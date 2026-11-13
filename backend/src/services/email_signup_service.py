"""Email + password sign-up (Issue #1734).

A person without a Google or GitHub account creates an account with an email
address and a password, under the same sign-up controls as the providers.
Verify first: no account and no credential exists before the emailed link is
followed.

1. **Request** (``POST /auth/signup/email/request``): the route answers 202
   with the same body for every address and only counts its rate limits; the
   work below runs after the response (:func:`process_signup_request`), so
   neither the answer nor its timing tells whether an address has an account.

   - The sign-up controls are evaluated for a NEW account at this address,
     without consuming anything (``SignupGateService.email_signup_admissible``).
     Not admissible: nothing is sent to anyone.
   - The address already has an account: at most one "you already have an
     account" notice a day, without a token, and only to an address that
     account verified.
   - Otherwise a pending request (hashed token, hashed beta invite, locale,
     time zone) replaces any older one for the address and the link is sent.
     A beta invite admits at most a few requests a day.

2. **Verify** (``/signup/verify``): the mailbox holder chooses the password.
   ``POST /auth/signup/email/complete`` checks the policy and the terms
   first (a refusal does not spend the link), then, in ONE transaction
   under a per-address advisory lock: deletes the request row (single use),
   refuses an address that has an account by now (409), re-runs the
   sign-up controls consuming (a beta invite is redeemed here; 403 when
   they refuse), creates the account, records the terms acceptance and the
   other consents, writes the audit row and commits. Nothing in between
   commits, so a failure leaves the invite unredeemed and the link usable.

The password is chosen only on the verified side: a request carries no
password, so nobody can start a sign-up for someone else's address with a
password they know. The return path (an invitation page, a device approval)
travels with the browser that started the sign-up, never with the request.

Raw tokens, links and passwords are never logged; addresses only as keyed
digests.
"""

from __future__ import annotations

import asyncio
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from auth.password import hash_password
from auth.password_policy import PasswordPolicyError, validate_password_policy
from auth.roles import (
    Role,
    _is_email_unique_violation,
    claim_first_account,
    lock_address,
    stage_email_account,
)
from config.settings import get_settings
from db.base import _get_session_factory
from db.redis import increment_counter, set_if_absent
from models.auth import AuditLog, EmailSignupRequest, User
from services.announcement_consent_service import (
    SignupAnnouncementConsent,
    new_consent_row,
)
from services.email_service import (
    EMAIL_SEND_TIMEOUT_SECONDS,
    EmailService,
    get_email_service,
    redact_recipient,
)
from services.signup_gate_service import (
    EMAIL_SIGNUP_ACTOR,
    EmailSignupAdmission,
    SignupGateService,
)
from services.terms_service import TermsService, current_terms_version
from utils.datetime import utcnow
from utils.exceptions import (
    PasswordLinkInvalidError,
    RedisError,
    SignupAccountExistsError,
    SignupNotAllowedError,
    ValidationError,
)
from utils.hashing import hmac_sha256_hex, sha256_hex
from utils.logger import get_logger
from utils.user_prefs import is_valid_locale, is_valid_timezone

logger = get_logger(__name__)

SIGNED_UP_ACTION = "user.signed_up"

# At most this many admitted requests per beta invite per day (silent).
_INVITE_REQUESTS_PER_DAY = 5
_INVITE_WINDOW_SECONDS = 24 * 60 * 60
# At most one "you already have an account" notice per address per day.
_NOTICE_WINDOW_SECONDS = 24 * 60 * 60

_EMAIL_TIMEOUT_SECONDS = EMAIL_SEND_TIMEOUT_SECONDS

# One mailbox, ASCII only: a local part of printable atoms and dots, one "@",
# and a domain of LDH labels with at least one dot. No whitespace, controls,
# quotes, commas, semicolons, angle brackets or parentheses can match, so the
# string is a single recipient and never a header or a list.
_LOCAL_RE = re.compile(r"[A-Za-z0-9!#$%&*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&*+/=?^_`{|}~-]+)*")
_LABEL_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
_MAX_ADDRESS_LEN = 254
_MAX_LOCAL_LEN = 64


def validate_signup_address(raw: str) -> str:
    """Return the normalized address, or raise ``ValueError`` for a bad shape.

    Shape only (never whether the address exists), so a 422 built on it
    reveals nothing. Leading and trailing blanks are dropped; the result is
    lower-case. ``@local`` (the CLI accounts' pseudo-domain) is refused.

    Raises:
        ValueError: Not exactly one ASCII mailbox of an accepted shape.
    """
    value = raw.strip()
    if not value or len(value) > _MAX_ADDRESS_LEN or not value.isascii():
        raise ValueError("Enter a valid email address")
    if value.count("@") != 1:
        raise ValueError("Enter a valid email address")
    local, domain = value.split("@")
    if len(local) > _MAX_LOCAL_LEN or not _LOCAL_RE.fullmatch(local):
        raise ValueError("Enter a valid email address")
    labels = domain.split(".")
    if len(labels) < 2 or not all(_LABEL_RE.fullmatch(label) for label in labels):
        raise ValueError("Enter a valid email address")
    if not labels[-1].isalpha() and not labels[-1].lower().startswith("xn--"):
        raise ValueError("Enter a valid email address")
    normalized = value.lower()
    if normalized.endswith("@local"):
        raise ValueError("Enter a valid email address")
    return normalized


def link_ttl() -> timedelta:
    """Lifetime of an email sign-up link (``EMAIL_SIGNUP_LINK_TTL_MINUTES``)."""
    return timedelta(minutes=get_settings().email_signup_link_ttl_minutes)


def _frontend_url(path: str) -> str:
    base_url = get_settings().frontend_url.strip().rstrip("/")
    return f"{base_url}{path}"


@dataclass(frozen=True)
class PendingSignupEmail:
    """A sign-up link to send after the request committed.

    Attributes:
        to_email: The normalized address. **Personal data** — do not log.
        verify_url: The link. **Sensitive** — do not log.
        expires_in_minutes: Link lifetime for the body.
        locale: Language of the mail.
    """

    to_email: str
    verify_url: str
    expires_in_minutes: int
    locale: str | None


@dataclass(frozen=True)
class AccountExistsNotice:
    """A "you already have an account" notice (no token).

    Attributes:
        to_email: The account's verified address. **Personal data**.
        locale: Language of the mail.
    """

    to_email: str
    locale: str | None


@dataclass(frozen=True)
class SignupConsents:
    """What the person agreed to on the completion page.

    Attributes:
        terms_version: The terms version the page showed and the person
            accepted, or ``None``.
        announcement: The announcement-email opt-in (#2761) as
            ``resolve_signup_consent`` resolved it, or ``None`` (not shown,
            invalid, stale wording, or the feature off): no row then.
    """

    terms_version: str | None
    announcement: SignupAnnouncementConsent | None = None


async def stage_signup_consents(
    db: AsyncSession, *, user_id: str, consents: SignupConsents
) -> None:
    """Stage the consent rows of a new account; never commits.

    Called inside the account-creating transaction, after the ``users`` row is
    flushed (the consent row references it) and the terms acceptance staged,
    so a refused or failed sign-up writes none. The terms acceptance is staged
    by the caller; the announcement-email opt-in (#2761) is one ``signup``
    history row here.
    """
    if consents.announcement is not None:
        db.add(
            new_consent_row(
                user_id=user_id,
                value=consents.announcement.value,
                source="signup",
                wording_version=consents.announcement.wording_version,
            )
        )


@dataclass(frozen=True)
class CreatedEmailAccount:
    """The account a completed email sign-up created.

    Attributes:
        user_id: ``email:<uuid4>``.
        email: The normalized address.
        name: Display name, or ``None``.
        role: The role it was given (``new_account_role``).
        admission: Which sign-up control admitted it.
    """

    user_id: str
    email: str
    name: str | None
    role: Role
    admission: EmailSignupAdmission


async def _address_has_account(db: AsyncSession, email: str) -> bool:
    """Any account whose address equals ``email`` ignoring case."""
    found = await db.execute(select(User.id).where(func.lower(User.email) == email).limit(1))
    return found.first() is not None


async def _over_daily_invite_limit(beta_invite_token_hash: str) -> bool:
    """Count one admitted request against the invite; True when over (fail closed)."""
    try:
        count = await increment_counter(
            f"email_signup_invite:{redact_recipient(beta_invite_token_hash)}",
            ttl=_INVITE_WINDOW_SECONDS,
        )
    except RedisError:
        logger.warning("email_signup_invite_limit_unavailable")
        return True
    return count > _INVITE_REQUESTS_PER_DAY


async def _claim_notice_slot(email: str) -> bool:
    """True for the first notice to ``email`` in a day (fail closed)."""
    try:
        return await set_if_absent(
            f"email_signup_notice:{redact_recipient(email)}", "1", _NOTICE_WINDOW_SECONDS
        )
    except RedisError:
        logger.warning("email_signup_notice_limit_unavailable")
        return False


class EmailSignupService:
    """Request, preview and complete an email + password sign-up."""

    def __init__(self, db: AsyncSession, email_service: EmailService | None = None) -> None:
        """Bind the service to a session.

        Args:
            db: The async session.
            email_service: Override for tests; defaults to the configured one.
        """
        self.db = db
        self._email_service = email_service

    @property
    def email_service(self) -> EmailService:
        if self._email_service is None:
            self._email_service = get_email_service()
        return self._email_service

    # ------------------------------------------------------------------
    # Request (after the response)
    # ------------------------------------------------------------------

    async def request(
        self,
        *,
        email: str,
        beta_invite_token_hash: str | None,
        locale: str | None,
        timezone: str | None,
    ) -> PendingSignupEmail | AccountExistsNotice | None:
        """Decide what a sign-up request sends, and store a pending request.

        Args:
            email: The normalized address (``validate_signup_address``).
            beta_invite_token_hash: SHA-256 of the beta invite carried, or None.
            locale: UI language of the page; dropped when unsupported.
            timezone: Browser time zone; dropped when not a valid IANA name.

        Returns:
            The link to send, the notice to send, or ``None`` (send nothing).
        """
        email = email.strip().lower()
        email_hmac = hmac_sha256_hex(email, get_settings().audit_hmac_key)
        locale = locale if is_valid_locale(locale) else None
        timezone = timezone if is_valid_timezone(timezone) else None
        gate = SignupGateService(self.db)
        admission = await gate.email_signup_admissible(
            email=email, beta_invite_token_hash=beta_invite_token_hash
        )
        if admission is None:
            # Registration closed for this address: nothing goes to anyone,
            # not even a notice to an existing account.
            logger.info("email_signup_request_not_admissible", email_hmac=email_hmac)
            await self.db.rollback()
            return None

        existing = (
            (
                await self.db.execute(
                    select(User.email, User.email_verified_at, User.locale)
                    .where(func.lower(User.email) == email)
                    .limit(2)
                )
            )
            .tuples()
            .all()
        )
        if existing:
            await self.db.rollback()
            if len(existing) != 1 or existing[0][1] is None:
                logger.info("email_signup_request_existing_unverified", email_hmac=email_hmac)
                return None
            if not await _claim_notice_slot(email):
                logger.info("email_signup_notice_throttled", email_hmac=email_hmac)
                return None
            logger.info("email_signup_request_existing_account", email_hmac=email_hmac)
            return AccountExistsNotice(to_email=existing[0][0], locale=locale or existing[0][2])

        if admission == "beta_invite" and beta_invite_token_hash:
            if await _over_daily_invite_limit(beta_invite_token_hash):
                logger.info("email_signup_invite_throttled", email_hmac=email_hmac)
                await self.db.rollback()
                return None

        raw_token = secrets.token_urlsafe(32)
        now = utcnow()
        expires_at = now + link_ttl()
        await lock_address(self.db, email)
        # Under the lock: an account created meanwhile (by a completion or an
        # OAuth sign-up, which takes the same lock) gets no new link.
        if await _address_has_account(self.db, email):
            await self.db.rollback()
            return None
        await self.db.execute(delete(EmailSignupRequest).where(EmailSignupRequest.email == email))
        row = EmailSignupRequest(
            email=email,
            token_hash=sha256_hex(raw_token),
            beta_invite_token_hash=beta_invite_token_hash,
            locale=locale,
            timezone=timezone,
            expires_at=expires_at,
        )
        self.db.add(row)
        await self.db.flush()
        request_id = row.id
        await self.db.commit()
        logger.info(
            "email_signup_requested",
            request_id=str(request_id),
            admission=admission,
            email_hmac=email_hmac,
        )
        return PendingSignupEmail(
            to_email=email,
            verify_url=f"{_frontend_url('/signup/verify')}?token={raw_token}",
            expires_in_minutes=int(link_ttl().total_seconds() // 60),
            locale=locale,
        )

    async def send(self, pending: PendingSignupEmail | AccountExistsNotice) -> None:
        """Send what :meth:`request` returned; never raises (after the response)."""
        try:
            if isinstance(pending, PendingSignupEmail):
                coro = self.email_service.send_signup_verification(
                    to_email=pending.to_email,
                    verify_url=pending.verify_url,
                    expires_in_minutes=pending.expires_in_minutes,
                    locale=pending.locale,
                )
            else:
                coro = self.email_service.send_signup_account_exists(
                    to_email=pending.to_email,
                    sign_in_url=_frontend_url("/login"),
                    reset_url=_frontend_url("/password/forgot"),
                    locale=pending.locale,
                )
            sent = await asyncio.wait_for(coro, timeout=_EMAIL_TIMEOUT_SECONDS)
        except Exception as exc:
            logger.error("email_signup_email_dispatch_failed", error_type=type(exc).__name__)
            return
        if not sent:
            logger.error("email_signup_email_dispatch_failed", error_type="send_returned_false")

    # ------------------------------------------------------------------
    # Preview / complete (the link holder)
    # ------------------------------------------------------------------

    async def _live_request(self, raw_token: str) -> EmailSignupRequest:
        row = (
            await self.db.execute(
                select(EmailSignupRequest).where(
                    EmailSignupRequest.token_hash == sha256_hex(raw_token),
                    EmailSignupRequest.expires_at > utcnow(),
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise PasswordLinkInvalidError()
        return row

    async def preview(self, raw_token: str) -> tuple[str, datetime]:
        """The address and expiry of a live link; consumes nothing.

        Raises:
            PasswordLinkInvalidError: Unknown, expired or used (400).
        """
        row = await self._live_request(raw_token)
        email, expires_at = row.email, row.expires_at
        await self.db.rollback()
        return email, expires_at

    async def complete(
        self,
        *,
        raw_token: str,
        new_password: str,
        name: str | None,
        consents: SignupConsents,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> CreatedEmailAccount:
        """Create the account from a sign-up link.

        Raises:
            ValidationError: The password breaks the policy
                (``field="new_password"``) or the current terms were not
                accepted (``field="accepted_terms"``); the link stays usable (422).
            PasswordLinkInvalidError: Unknown, expired or used link (400).
            SignupAccountExistsError: The address has an account by now; the
                link is spent (409).
            SignupNotAllowedError: The sign-up controls refuse it now; the
                link is spent (403).
        """
        # 1. Cheap refusals first: none of them spends the link.
        try:
            validate_password_policy(new_password)
        except PasswordPolicyError as exc:
            raise ValidationError(str(exc), field="new_password") from None
        current_terms = current_terms_version()
        if current_terms is not None and consents.terms_version != current_terms:
            raise ValidationError(
                "Accept the current terms of service to create the account",
                field="accepted_terms",
            )
        # 2. The link must exist before the server spends bcrypt on it.
        token_hash = sha256_hex(raw_token)
        pending = await self._live_request(raw_token)
        email = pending.email
        password_hash = await asyncio.to_thread(hash_password, new_password)
        # 3. The gate's config may self-heal with a commit; read it before the
        #    transaction that must not commit until the very end.
        gate = SignupGateService(self.db)
        config = await gate.get_config()
        enabled, mode = config.enabled, config.mode
        await self.db.rollback()

        try:
            return await self._create(
                gate=gate,
                gate_enabled=enabled,
                gate_mode=mode,
                token_hash=token_hash,
                email=email,
                password_hash=password_hash,
                name=name,
                consents=consents,
                current_terms=current_terms,
                ip_address=ip_address,
                user_agent=user_agent,
            )
        except (SignupAccountExistsError, SignupNotAllowedError, PasswordLinkInvalidError):
            raise
        except BaseException:
            await self.db.rollback()
            raise

    async def _create(
        self,
        *,
        gate: SignupGateService,
        gate_enabled: bool,
        gate_mode: str,
        token_hash: str,
        email: str,
        password_hash: str,
        name: str | None,
        consents: SignupConsents,
        current_terms: str | None,
        ip_address: str | None,
        user_agent: str | None,
    ) -> CreatedEmailAccount:
        email_hmac = hmac_sha256_hex(email, get_settings().audit_hmac_key)
        # #2788: while no account exists, take the first-account lock BEFORE
        # the address lock, in every SYSTEM_ADMIN_MODE. The gate's first-user
        # admission below and the insert share this transaction, so this
        # serialises email completions with each other and with every
        # session-mode creator, and keeps the lock order of the OAuth path
        # (first-account, then address, then rows). The role is decided later
        # by new_account_role, which re-takes the same lock at once. (Only if
        # the last account were erased between this count and that decision
        # would it take the lock after the address lock; not reachable in
        # practice.)
        await claim_first_account(self.db, serialize=True)
        await lock_address(self.db, email)
        consumed = cast(
            CursorResult[Any],
            await self.db.execute(
                delete(EmailSignupRequest)
                .where(
                    EmailSignupRequest.token_hash == token_hash,
                    EmailSignupRequest.expires_at > utcnow(),
                )
                .returning(
                    EmailSignupRequest.id,
                    EmailSignupRequest.beta_invite_token_hash,
                    EmailSignupRequest.locale,
                    EmailSignupRequest.timezone,
                )
            ),
        ).first()
        if consumed is None:
            # A concurrent completion of the same link won.
            await self.db.rollback()
            raise PasswordLinkInvalidError()
        request_id = consumed[0]
        beta_invite_token_hash: str | None = consumed[1]
        locale: str | None = consumed[2]
        timezone: str | None = consumed[3]

        if await _address_has_account(self.db, email):
            await self.db.commit()  # the link stays spent
            logger.info(
                "email_signup_refused_account_exists",
                request_id=str(request_id),
                email_hmac=email_hmac,
            )
            raise SignupAccountExistsError()

        user_id = f"email:{uuid4()}"
        admission = await gate.admit_email_signup(
            gate_enabled=gate_enabled,
            gate_mode=gate_mode,
            email=email,
            new_user_id=user_id,
            beta_invite_token_hash=beta_invite_token_hash,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        if admission is None:
            await self.db.commit()  # the link stays spent; the gate staged nothing
            logger.info("email_signup_blocked", request_id=str(request_id), email_hmac=email_hmac)
            raise SignupNotAllowedError()

        role = await stage_email_account(
            self.db,
            user_id=user_id,
            email=email,
            name=name,
            password_hash=password_hash,
            locale=locale,
            timezone=timezone,
        )
        # The acceptance and consent rows reference users.user_id.
        await self.db.flush()
        if current_terms is not None:
            # Checked above: the person accepted exactly the current version.
            TermsService(self.db).stage_initial_acceptance(
                user_id=user_id,
                user_email=EMAIL_SIGNUP_ACTOR,
                version=current_terms,
                source="join" if admission == "beta_invite" else "login",
                ip_address=ip_address,
                user_agent=user_agent,
            )
        await stage_signup_consents(self.db, user_id=user_id, consents=consents)
        self.db.add(
            AuditLog(
                user_email=EMAIL_SIGNUP_ACTOR,
                user_id=user_id,
                action=SIGNED_UP_ACTION,
                resource=f"user:{user_id}",
                new_value_hash=email_hmac,
                user_metadata={"method": "email", "admission": admission, "role": role.value},
                ip_address=ip_address,
                user_agent=user_agent,
            )
        )
        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            if not _is_email_unique_violation(exc):
                raise
            # Exact-case twin inserted outside the lock: spend the link anyway.
            await self.db.execute(
                delete(EmailSignupRequest).where(EmailSignupRequest.token_hash == token_hash)
            )
            await self.db.commit()
            raise SignupAccountExistsError() from None
        logger.info(
            "email_signup_completed",
            request_id=str(request_id),
            user_id=user_id,
            admission=admission,
            role=role.value,
        )
        return CreatedEmailAccount(
            user_id=user_id, email=email, name=name, role=role, admission=admission
        )


async def process_signup_request(
    *,
    email: str,
    beta_invite_token_hash: str | None,
    locale: str | None,
    timezone: str | None,
    session_factory: Callable[[], AsyncSession] | None = None,
    email_service: EmailService | None = None,
) -> None:
    """Do a sign-up request's work after the response; never raises.

    Args:
        email: The normalized address. **Personal data** — never logged.
        beta_invite_token_hash: SHA-256 of the beta invite, or None.
        locale: UI language of the page.
        timezone: Browser time zone.
        session_factory: Override for tests; defaults to the app's factory.
        email_service: Override for tests; defaults to the configured one.
    """
    try:
        factory = session_factory or _get_session_factory()
        async with factory() as db:
            service = EmailSignupService(db, email_service=email_service)
            pending = await service.request(
                email=email,
                beta_invite_token_hash=beta_invite_token_hash,
                locale=locale,
                timezone=timezone,
            )
    except Exception as exc:
        # Type only: a driver error's text could echo the address.
        logger.error("email_signup_request_failed", error_type=type(exc).__name__)
        return
    if pending is not None:
        await service.send(pending)
