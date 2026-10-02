"""Self-service password management for existing accounts (Issue #1678).

Existing accounts can sign in with a verified email and a password. This
service owns every change to ``users.password_hash`` made outside the admin
CLIs:

- **Reset** (public): a verified-email account with a password asks for a
  link; following it sets a new password.
- **Set up** (signed in): an account without a password asks for a link to
  its own address; following it sets the first password and, because it
  proves the mailbox, marks the email verified.
- **Change** / **remove** (signed in): both re-verify the current password;
  removing refuses when no OAuth provider would remain.

No method here creates a ``User``: password sign-in and recovery never create
accounts. Session revocation is the caller's (it needs the session manager
and the request's cookie): each write takes a ``revoke_sessions`` callback and
runs it after the write and BEFORE the commit, so a failed revocation rolls the
write back instead of leaving a new password beside the old sessions.

A **reset** is the compromise-recovery path, so it also revokes every OAuth2 /
MCP grant of the account in the same transaction (#1738): access and refresh
tokens, authorization codes not yet exchanged, and device codes. A change or
set-up (the caller proves the current password or the mailbox while signed
in) keeps them. API keys, OAuth client secrets, share keys and resource tokens
are integration credentials and are never revoked here; the reset page and
email tell the user to review them.

bcrypt (``hash_password`` / ``verify_password``) runs in a worker thread: it
is CPU-bound and would otherwise stall the event loop.

Raw tokens, reset URLs and passwords are never logged or placed in exceptions.
"""

from __future__ import annotations

import asyncio
import hmac
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.password import hash_password, verify_password
from auth.password_policy import PasswordPolicyError, validate_password_policy
from config.settings import get_settings
from db.base import _get_session_factory
from models.auth import (
    AuditLog,
    User,
    UserOAuthProvider,
)
from services.email_action_token_service import (
    PASSWORD_LINK_PURPOSES,
    EmailActionPurpose,
    EmailActionTokenService,
    token_ttl,
)
from services.email_service import (
    EMAIL_SEND_TIMEOUT_SECONDS,
    EmailService,
    get_email_service,
)
from services.known_device_service import known_devices_delete
from services.oauth_grant_revocation import revoke_oauth_grants
from utils.datetime import utcnow
from utils.exceptions import (
    ConflictError,
    CurrentPasswordMismatchError,
    EmailDispatchError,
    NotFoundException,
    PasswordLinkInvalidError,
    PasswordSetupNotAllowedError,
    ValidationError,
)
from utils.hashing import sha256_hex
from utils.logger import get_logger

logger = get_logger(__name__)

# Same bound as the erasure confirmation email: a stuck provider must not hold
# the request open indefinitely. No transaction is open during a send.
_EMAIL_TIMEOUT_SECONDS = EMAIL_SEND_TIMEOUT_SECONDS

# ``audit_logs.user_email`` carries an actor label, not the subject's mutable
# email: the subject is identified by ``user_id``, which erasure pseudonymizes.
_AUDIT_ACTOR_SELF = "self-service"
_AUDIT_ACTOR_LINK = "email-link"

# Every password change kills the outstanding password links, so an older
# email cannot undo (or redo) it.
_PASSWORD_LINK_PURPOSES = PASSWORD_LINK_PURPOSES


def credential_fingerprint(password_hash: str | None) -> str | None:
    """A stand-in for a stored password hash that says only whether it changed.

    A sign-in records the fingerprint of the hash it verified, and re-checks
    it with :func:`password_unchanged` once its session exists (#1809). It is
    kept beside a pending MFA step in Redis, so it is a digest — the bcrypt
    hash itself never leaves the database. Every new password gets a new
    bcrypt salt, so setting the same password again still changes it.
    """
    if password_hash is None:
        return None
    return sha256_hex(password_hash)


async def password_unchanged(db: AsyncSession, user_id: str, fingerprint: str | None) -> bool:
    """Is the account's committed password still the one a sign-in verified? (#1809)

    The race this closes: a password write (reset, set-up, change, removal)
    deletes the account's browser sessions BEFORE it commits, while holding
    the ``users`` row ``FOR UPDATE``. A sign-in that verified the old hash and
    writes its session after that sweep would otherwise keep a live session
    the new password never authorized.

    Call it AFTER the session is written. ``FOR SHARE`` conflicts with the
    writer's row lock, so a write in progress makes this wait for its commit
    and then read the new hash. Either the session was written before the
    sweep (and the sweep deleted it), or this read sees the change. The
    caller owns the transaction and must end it promptly to drop the lock.

    False when the hash changed or is gone, or the user no longer exists.
    """
    if fingerprint is None:
        return False
    current = await db.scalar(
        select(User.password_hash).where(User.user_id == user_id).with_for_update(read=True)
    )
    current_fingerprint = credential_fingerprint(current)
    return current_fingerprint is not None and hmac.compare_digest(current_fingerprint, fingerprint)


def normalize_email(email: str) -> str:
    """Return the comparison form of an email address (``lower(trim())``)."""
    return email.strip().lower()


def is_local_address(email: str) -> bool:
    """Return whether ``email`` is a local CLI account address (``@local``)."""
    return normalize_email(email).endswith("@local")


async def find_password_user_by_email(db: AsyncSession, email: str) -> User | None:
    """Find the one account that may sign in (or reset) with ``email``.

    The account must have a verified email and a password; ``@local``
    addresses never match. Accounts whose emails differ only by case make the
    lookup ambiguous, and it fails closed.

    Args:
        db: The async session.
        email: The address as typed.

    Returns:
        The user, or ``None`` when zero or several accounts match.
    """
    normalized = normalize_email(email)
    if not normalized or "@" not in normalized or is_local_address(normalized):
        return None
    rows = (
        (
            await db.execute(
                select(User)
                .where(
                    func.lower(User.email) == normalized,
                    User.email_verified_at.is_not(None),
                    User.password_hash.is_not(None),
                )
                .limit(2)
            )
        )
        .scalars()
        .all()
    )
    if len(rows) != 1:
        if rows:
            logger.warning("password_email_lookup_collision", matches=len(rows))
        return None
    return rows[0]


def _frontend_link(path: str, raw_token: str) -> str:
    """Build the front-end URL a password email points at."""
    base_url = get_settings().frontend_url.strip().rstrip("/")
    return f"{base_url}{path}?token={raw_token}"


# Revokes the account's browser sessions; raises when it could not.
SessionRevoker = Callable[[str], None]


async def _validated_hash(new_password: str) -> str:
    """Validate ``new_password`` against the policy and hash it (off the loop).

    Raises:
        ValidationError: 422 with the policy message (never the password).
    """
    try:
        validate_password_policy(new_password)
    except PasswordPolicyError as exc:
        raise ValidationError(str(exc), field="new_password") from None
    return await asyncio.to_thread(hash_password, new_password)


@dataclass(frozen=True)
class PendingResetEmail:
    """A reset email to send after the response (see ``request_reset``).

    Attributes:
        to_email: The account's address. **Personal data** — do not log.
        reset_url: The link. **Sensitive** — do not log.
        expires_in_minutes: Link lifetime for the body.
    """

    to_email: str
    reset_url: str
    expires_in_minutes: int


class PasswordAccountService:
    """Reset, set up, change and remove a user's password."""

    def __init__(self, db: AsyncSession, email_service: EmailService | None = None) -> None:
        """Bind the service to a session.

        Args:
            db: The request's async session.
            email_service: Override for tests; defaults to the configured one.
        """
        self.db = db
        self.email_service = email_service or get_email_service()

    # ------------------------------------------------------------------
    # Reset (public)
    # ------------------------------------------------------------------

    async def request_reset(
        self,
        *,
        email: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> PendingResetEmail | None:
        """Issue a reset link when ``email`` names an eligible account.

        Runs after the response (see ``process_reset_request``): the lookup,
        the token, the audit row and the commit all differ between a hit and
        a miss, so none of them may happen on the request path. A failed send
        strands nothing: the link just expires, and a new request invalidates
        it.

        Args:
            email: The address as typed.
            ip_address: Client IP for the audit row.
            user_agent: Client user agent for the audit row.

        Returns:
            The email to send, or ``None`` when no eligible account matched.
        """
        user = await find_password_user_by_email(self.db, email)
        if user is None:
            return None
        issued = await EmailActionTokenService(self.db).issue(
            user_id=user.user_id, email=user.email, purpose="reset_password"
        )
        self._audit(
            user.user_id, _AUDIT_ACTOR_SELF, "password_reset_requested", ip_address, user_agent
        )
        await self.db.commit()
        return PendingResetEmail(
            to_email=user.email,
            reset_url=_frontend_link("/password/reset", issued.raw_token),
            expires_in_minutes=int(token_ttl("reset_password").total_seconds() // 60),
        )

    async def send_reset_email(self, pending: PendingResetEmail) -> None:
        """Send a reset email; never raises (runs after the response).

        Args:
            pending: What ``request_reset`` returned.
        """
        try:
            sent = await asyncio.wait_for(
                self.email_service.send_password_reset(
                    to_email=pending.to_email,
                    reset_url=pending.reset_url,
                    expires_in_minutes=pending.expires_in_minutes,
                ),
                timeout=_EMAIL_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            logger.error("password_reset_email_dispatch_failed", **_error_fields(exc))
            return
        if not sent:
            logger.error("password_reset_email_dispatch_failed", error_type="send_returned_false")

    async def complete_reset(
        self,
        *,
        raw_token: str,
        new_password: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
        revoke_sessions: SessionRevoker | None = None,
    ) -> str:
        """Set a new password from a reset link.

        The policy is checked before the token is consumed, so a rejected
        password does not burn the link.

        Args:
            raw_token: The token from the link.
            new_password: The new password.
            ip_address: Client IP for the audit row.
            user_agent: Client user agent for the audit row.
            revoke_sessions: Signs the account out; run before the commit.
                Every OAuth2 / MCP grant of the account is revoked in the same
                transaction (#1738), so it rolls back with the password.

        Returns:
            The user_id whose password was reset.

        Raises:
            ValidationError: The password breaks the policy (422).
            PasswordLinkInvalidError: The link is unknown, expired, used, was
                sent to an address the account no longer has, or the account
                no longer has a password to reset (400).
        """
        password_hash = await _validated_hash(new_password)
        user = await self._consume_for_user(raw_token, "reset_password")
        user.password_hash = password_hash
        await self._invalidate_password_links(user.user_id)
        tokens_revoked = await self._revoke_oauth_grants(user.user_id)
        # #1769: forget every known browser, so the next sign-in from each —
        # the attacker's included — is a new device and emails the owner.
        await self.db.execute(known_devices_delete(user.user_id))
        self._audit(user.user_id, _AUDIT_ACTOR_LINK, "password_reset", ip_address, user_agent)
        await self._revoke_then_commit(user.user_id, revoke_sessions)
        logger.info(
            "password_reset_completed", user_id=user.user_id, oauth_tokens_revoked=tokens_revoked
        )
        return user.user_id

    # ------------------------------------------------------------------
    # Set up (signed in, then link)
    # ------------------------------------------------------------------

    async def request_setup(
        self,
        *,
        user_id: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        """Email a set-a-password link to a signed-in user without a password.

        The token (and the audit row recording the request) is committed
        BEFORE the email is sent. The send runs in a worker thread that a
        timeout cannot cancel, so a send reported as failed may still deliver
        its email; its link must then work. A failed send raises (503) and the
        user can retry; the stranded token is harmless, because the next
        request invalidates it and it expires anyway.

        Args:
            user_id: The signed-in user.
            ip_address: Client IP for the audit row.
            user_agent: Client user agent for the audit row.

        Raises:
            NotFoundException: The user row is gone (404).
            ConflictError: The account already has a password (409).
            PasswordSetupNotAllowedError: A local CLI account (400).
            EmailDispatchError: The email could not be sent (503).
        """
        user = await self._load_user(user_id)
        if user.password_hash is not None:
            raise ConflictError("This account already has a password")
        if is_local_address(user.email):
            raise PasswordSetupNotAllowedError()

        issued = await EmailActionTokenService(self.db).issue(
            user_id=user.user_id, email=user.email, purpose="set_password"
        )
        self._audit(
            user.user_id, _AUDIT_ACTOR_SELF, "password_setup_requested", ip_address, user_agent
        )
        to_email = user.email
        await self.db.commit()
        try:
            sent = await asyncio.wait_for(
                self.email_service.send_password_setup(
                    to_email=to_email,
                    setup_url=_frontend_link("/password/setup", issued.raw_token),
                    expires_in_minutes=int(token_ttl("set_password").total_seconds() // 60),
                ),
                timeout=_EMAIL_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            logger.error(
                "password_setup_email_dispatch_failed", user_id=user_id, **_error_fields(exc)
            )
            raise EmailDispatchError() from None
        if not sent:
            logger.error(
                "password_setup_email_dispatch_failed",
                user_id=user_id,
                error_type="send_returned_false",
            )
            raise EmailDispatchError()

    async def complete_setup(
        self,
        *,
        raw_token: str,
        new_password: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
        revoke_sessions: SessionRevoker | None = None,
    ) -> str:
        """Set the first password from a set-up link and verify the email.

        Args:
            raw_token: The token from the link.
            new_password: The new password.
            ip_address: Client IP for the audit row.
            user_agent: Client user agent for the audit row.
            revoke_sessions: Signs the account's other sessions out; run
                before the commit.

        Returns:
            The user_id whose password was set.

        Raises:
            ValidationError: The password breaks the policy (422).
            PasswordLinkInvalidError: The link is unknown, expired, used, was
                sent to an address the account no longer has, or the account
                has a password by now (400).
        """
        password_hash = await _validated_hash(new_password)
        user = await self._consume_for_user(raw_token, "set_password")
        user.password_hash = password_hash
        if user.email_verified_at is None:
            user.email_verified_at = utcnow()
        await self._invalidate_password_links(user.user_id)
        self._audit(user.user_id, _AUDIT_ACTOR_LINK, "password_set", ip_address, user_agent)
        await self._revoke_then_commit(user.user_id, revoke_sessions)
        logger.info("password_setup_completed", user_id=user.user_id)
        return user.user_id

    # ------------------------------------------------------------------
    # Change / remove (signed in)
    # ------------------------------------------------------------------

    async def change(
        self,
        *,
        user_id: str,
        current_password: str,
        new_password: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
        revoke_sessions: SessionRevoker | None = None,
    ) -> None:
        """Replace the password after re-verifying the current one.

        ``revoke_sessions`` signs the other sessions out before the commit.

        Raises:
            NotFoundException: The user row is gone (404).
            ConflictError: The account has no password to change (409).
            CurrentPasswordMismatchError: ``current_password`` is wrong (403).
            ValidationError: The new password breaks the policy (422).
        """
        # Same lock as remove(): a removal running at the same time waits, then
        # sees this change (and refuses), instead of the two overwriting each
        # other.
        user = await self._load_user(user_id, for_update=True)
        await self._verify_current(user, current_password)
        user.password_hash = await _validated_hash(new_password)
        await self._invalidate_password_links(user.user_id)
        self._audit(user.user_id, _AUDIT_ACTOR_SELF, "password_changed", ip_address, user_agent)
        await self._revoke_then_commit(user.user_id, revoke_sessions)
        logger.info("password_changed", user_id=user_id)

    async def remove(
        self,
        *,
        user_id: str,
        current_password: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
        revoke_sessions: SessionRevoker | None = None,
    ) -> None:
        """Remove the password, never the last sign-in method.

        ``revoke_sessions`` signs the other sessions out before the commit.

        Raises:
            NotFoundException: The user row is gone (404).
            ConflictError: No password to remove, or no OAuth provider would
                remain (409).
            CurrentPasswordMismatchError: ``current_password`` is wrong (403).
        """
        # Lock the user row before counting: an unlink running at the same
        # time locks it too, so one of the two sees the other's commit and
        # refuses instead of both removing a method.
        user = await self._load_user(user_id, for_update=True)
        await self._verify_current(user, current_password)
        linked = await self.db.scalar(
            select(func.count())
            .select_from(UserOAuthProvider)
            .where(UserOAuthProvider.user_id == user_id)
        )
        if not linked:
            raise ConflictError("Cannot remove the only remaining sign-in method")
        user.password_hash = None
        await self._invalidate_password_links(user.user_id)
        self._audit(user.user_id, _AUDIT_ACTOR_SELF, "password_removed", ip_address, user_agent)
        await self._revoke_then_commit(user.user_id, revoke_sessions)
        logger.info("password_removed", user_id=user_id)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _load_user(self, user_id: str, *, for_update: bool = False) -> User:
        stmt = select(User).where(User.user_id == user_id)
        if for_update:
            # ``populate_existing``: a row already in the identity map must be
            # refreshed from the locked read, not served stale.
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        user = (await self.db.execute(stmt)).scalar_one_or_none()
        if user is None:
            raise NotFoundException("User", resource_id=user_id)
        return user

    @staticmethod
    async def _verify_current(user: User, current_password: str) -> None:
        if user.password_hash is None:
            raise ConflictError("This account has no password")
        if not await asyncio.to_thread(verify_password, current_password, user.password_hash):
            raise CurrentPasswordMismatchError()

    async def _revoke_then_commit(
        self, user_id: str, revoke_sessions: SessionRevoker | None
    ) -> None:
        """Revoke the sessions, then commit the password write.

        Revoking first means a failure leaves nothing committed: the write
        (and a consumed link) is rolled back and the caller can retry.
        """
        if revoke_sessions is not None:
            try:
                # The revoker is synchronous Redis work (a scan of the session
                # keys); run it off the event loop so the loop — and the grant
                # writers waiting on this transaction's user lock in worker
                # threads (#1770) — keep moving while it runs.
                await asyncio.to_thread(revoke_sessions, user_id)
            except BaseException:
                await self.db.rollback()
                raise
        await self.db.commit()

    async def _consume_for_user(self, raw_token: str, purpose: EmailActionPurpose) -> User:
        consumed = await EmailActionTokenService(self.db).consume(
            raw_token=raw_token, purpose=purpose
        )
        if consumed is None:
            await self.db.rollback()
            raise PasswordLinkInvalidError()
        # ``consume`` locked the user row; re-read it under that lock (fresh,
        # not from the identity map), so a removal or change committed in the
        # meantime is what the checks below see.
        user = (
            await self.db.execute(
                select(User)
                .where(User.user_id == consumed.user_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        # A link sent to an address the account no longer has proves nothing
        # about the current mailbox. A reset link needs a password to reset;
        # a set-up link is for an account that has none (either may have
        # changed since the link was sent).
        if (
            user is None
            or normalize_email(user.email) != normalize_email(consumed.email)
            or (user.password_hash is None) != (purpose == "set_password")
        ):
            await self.db.commit()  # keep the link burned
            raise PasswordLinkInvalidError()
        return user

    async def _revoke_oauth_grants(self, user_id: str) -> int:
        """Revoke every OAuth2 / MCP grant of the account (#1738).

        Runs inside the reset's transaction, while ``_consume_for_user`` holds
        the ``users`` row lock; the shared revoker re-takes it (a no-op) and
        owns the statement order that closes the races with in-flight grants
        (``services/oauth_grant_revocation.py``, #1770). Bearer checks read
        the token row on every request, so nothing else needs invalidating.

        Returns:
            The number of tokens revoked.
        """
        return (await revoke_oauth_grants(self.db, user_id)).tokens

    async def _invalidate_password_links(self, user_id: str) -> None:
        await EmailActionTokenService(self.db).invalidate(
            user_id=user_id, purposes=_PASSWORD_LINK_PURPOSES
        )

    def _audit(
        self,
        user_id: str,
        actor: str,
        action: str,
        ip_address: str | None,
        user_agent: str | None,
    ) -> None:
        self.db.add(
            AuditLog(
                user_email=actor,
                user_id=user_id,
                action=action,
                resource="user:password",
                ip_address=ip_address,
                user_agent=user_agent,
            )
        )


async def process_reset_request(
    *,
    email: str,
    ip_address: str | None = None,
    user_agent: str | None = None,
    session_factory: Callable[[], AsyncSession] | None = None,
    email_service: EmailService | None = None,
) -> None:
    """Do a reset request's work after the response; never raises.

    The route answers every reset request with the same 202 before any of
    this runs, so neither the answer nor its timing reveals whether an
    account matched. The request's session is closed by then, so this opens
    its own; it is closed before the email is sent.

    Args:
        email: The normalized address. **Personal data** — never logged.
        ip_address: Client IP for the audit row.
        user_agent: Client user agent for the audit row.
        session_factory: Override for tests; defaults to the app's factory.
        email_service: Override for tests; defaults to the configured one.
    """
    try:
        factory = session_factory or _get_session_factory()
        async with factory() as db:
            service = PasswordAccountService(db, email_service=email_service)
            pending = await service.request_reset(
                email=email, ip_address=ip_address, user_agent=user_agent
            )
    except Exception as exc:
        # Type only: a driver error's text could echo the address.
        logger.error("password_reset_request_failed", error_type=type(exc).__name__)
        return
    if pending is not None:
        await service.send_reset_email(pending)


def _error_fields(exc: BaseException) -> dict[str, Any]:
    """Log-safe description of a send failure: type and status code only.

    ``str(exc)`` is never logged — an SDK error can echo the request body,
    which carries the link.
    """
    fields: dict[str, Any] = {"error_type": type(exc).__name__}
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if isinstance(status, int):
        fields["status_code"] = status
    return fields
