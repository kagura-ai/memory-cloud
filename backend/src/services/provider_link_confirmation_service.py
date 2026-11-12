"""Connect a provider to an email sign-up account after an email confirmation (Issue #2786).

A GitHub sign-in, or a Google sign-in Google is not authoritative for, whose
address belongs to an account created by email sign-up ends at
``email_in_use`` (``RoleManager.ensure_user`` raises ``ProviderLinkConfirmable``
once every other refusal passed). When the sign-in was started by this
browser's ``/login`` (``auth.oauth_browser``), the callback answers exactly as
before and starts :func:`process_link_offer` after the response:

1. **Offer** (detached task, never raises): the account is read again and must
   still be eligible with the same account stamp; a deliverable (verified)
   address is needed; at most 3 mails per account and 3 per provider identity
   a day (fail closed). A single-use token is minted, only its digest is
   stored, as the key of a Redis record (15 minutes) that holds the account,
   the provider identity, the digest of the browser nonce, the account stamp
   and the return path. A newer offer from the same identity to the same
   account replaces the older one. The link ``/link/confirm?token=`` is
   mailed to the account.
2. **Preview** (``POST /auth/link-confirm/preview``): which provider account
   would be connected, and whether this is the browser that started the
   sign-in. Consumes nothing.
3. **Complete** (``POST /auth/link-confirm/complete``): needs the token AND
   the nonce cookie of that browser; another browser gets 403 and the record
   stays. Then the record is taken atomically (single use) and, under the
   account's row lock, every condition is checked again (eligibility, account
   stamp, address, no provider of that kind, the identity linked nowhere and
   not the id of an account of its own) before the link row is written.
4. **Cancel** ("This wasn't me", ``POST /auth/link-confirm/cancel``):
   discards the record and sets ``users.provider_auto_link_blocked_at``: no
   further offers and no automatic Google link for the account. Connecting a
   provider from Profile still works.

Raw tokens, nonces and links are never logged; addresses and provider
identities only as keyed digests.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import re
import secrets
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

import auth.oauth_browser as oauth_browser
from auth.roles import Role, email_account_link_refusal, stage_address_link
from auth.roles import account_stamp as compute_account_stamp
from config.settings import get_settings
from models.auth import AuditLog, User, UserOAuthProvider
from services.email_service import EMAIL_SEND_TIMEOUT_SECONDS, EmailService, get_email_service
from utils.datetime import utcnow
from utils.exceptions import PasswordLinkInvalidError, ProviderLinkWrongBrowserError
from utils.hashing import hmac_sha256_hex, sha256_hex
from utils.logger import get_logger

logger = get_logger(__name__)

LINK_CONFIRM_TTL = timedelta(minutes=15)

SENT_ACTION = "oauth_provider_link_confirmation_sent"
CONFIRMED_ACTION = "oauth_provider_link_confirmed"
DECLINED_ACTION = "oauth_provider_link_confirmation_declined"
# audit_logs.user_email of these rows: an actor label, never the subject's address.
LINK_CONFIRMATION_ACTOR = "link-confirmation"

PROVIDER_LABELS: dict[str, str] = {"google": "Google", "github": "GitHub"}

_SENDS_PER_DAY = 3
_SEND_WINDOW_SECONDS = 24 * 60 * 60

_RECORD_KEY = "link_confirm:{digest}"
_POINTER_KEY = "link_confirm_identity:{account}:{provider}:{identity}"
_SENT_ACCOUNT_KEY = "link_confirm_sent_account:{account}"
_SENT_IDENTITY_KEY = "link_confirm_sent_identity:{identity}"

_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")
_GITHUB_LOGIN_RE = re.compile(r"[A-Za-z0-9-]{1,39}")
_AVATAR_HOSTS = {"github": "avatars.githubusercontent.com", "google": "lh3.googleusercontent.com"}
_AVATAR_MAX = 512
_NAME_MAX = 80
_ADDRESS_MAX = 254
# A display name loses format characters, private use, surrogates and
# unassigned code points; controls and line / paragraph separators become spaces.
_NAME_DROPPED = frozenset({"Cf", "Co", "Cs", "Cn"})
_NAME_SPACED = frozenset({"Cc", "Zl", "Zp"})

_spawned: set[asyncio.Task[None]] = set()


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PendingProviderLink:
    """A pending link, stored as JSON under the token's digest for 15 minutes.

    No plaintext address: ``email_hmac`` is the keyed digest of the account's
    normalized address when the link was offered.
    """

    account_user_id: str
    email_hmac: str
    provider: Literal["google", "github"]
    oauth_sub: str
    identity_hmac: str
    provider_login: str
    provider_name: str | None
    provider_avatar_url: str | None
    browser_hash: str
    account_stamp: str
    return_to: str | None
    created_at: str
    expires_at: str

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: object) -> PendingProviderLink | None:
        """Parse a stored record; None for anything that is not one."""
        if not isinstance(raw, str):
            return None
        try:
            values = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(values, dict):
            return None
        required = (
            "account_user_id",
            "email_hmac",
            "provider",
            "oauth_sub",
            "identity_hmac",
            "provider_login",
            "browser_hash",
            "account_stamp",
            "created_at",
            "expires_at",
        )
        if not all(isinstance(values.get(k), str) and values.get(k) for k in required):
            return None
        if values["provider"] not in PROVIDER_LABELS:
            return None
        for optional in ("provider_name", "provider_avatar_url", "return_to"):
            if values.get(optional) is not None and not isinstance(values.get(optional), str):
                return None
        try:
            datetime.fromisoformat(values["expires_at"])
        except ValueError:
            return None
        return cls(
            account_user_id=values["account_user_id"],
            email_hmac=values["email_hmac"],
            provider=values["provider"],
            oauth_sub=values["oauth_sub"],
            identity_hmac=values["identity_hmac"],
            provider_login=values["provider_login"],
            provider_name=values.get("provider_name"),
            provider_avatar_url=values.get("provider_avatar_url"),
            browser_hash=values["browser_hash"],
            account_stamp=values["account_stamp"],
            return_to=values.get("return_to"),
            created_at=values["created_at"],
            expires_at=values["expires_at"],
        )


@dataclass(frozen=True)
class ProviderLinkPreview:
    """What the confirmation page shows. ``same_browser`` decides the button."""

    provider: Literal["google", "github"]
    provider_login: str
    provider_name: str | None
    provider_avatar_url: str | None
    expires_at: datetime
    same_browser: bool


@dataclass(frozen=True)
class ConfirmedProviderLink:
    """The account a confirmed link connected the identity to (committed)."""

    user_id: str
    email: str
    name: str | None
    picture: str | None
    role: Role
    provider: Literal["google", "github"]
    oauth_sub: str
    provider_login: str
    return_to: str | None
    # The account stamp verified under the row lock when the link was made;
    # the session opened afterwards is kept only while it still matches.
    account_stamp: str


async def account_stamp_unchanged(db: AsyncSession, user_id: str, stamp: str) -> bool:
    """Is the account still in the state the link was made in? (#1809 pattern)

    The session a completed link opens is written after the link's row lock
    is gone. A password reset, an MFA, role or address change, or a removed
    provider signs the account out BEFORE it commits, under the row lock; a
    session written after that sweep would outlive it. Call this AFTER the
    session is written: ``FOR SHARE`` waits for such a write to commit and
    then reads its result, so either the sweep deleted the session or this
    read sees the change. The caller owns the transaction and ends it at
    once to drop the lock.

    False when the stamp differs or the account no longer exists.
    """
    user = (
        await db.execute(
            select(User)
            .where(User.user_id == user_id)
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if user is None:
        return False
    return hmac.compare_digest(compute_account_stamp(user), stamp)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _key() -> str:
    return get_settings().audit_hmac_key


def identity_hmac(provider: str, oauth_sub: str) -> str:
    """Keyed digest of ``provider:sub``: correlates attempts without the identity."""
    return hmac_sha256_hex(f"{provider}:{oauth_sub}", _key())


def _address_hmac(address: str) -> str:
    return hmac_sha256_hex(address.strip().lower(), _key())


def provider_login(provider: str, user_info: Mapping[str, Any]) -> str | None:
    """The provider account's identifier as the page and the mail show it.

    GitHub: the login, only when it is in GitHub's alphabet. Google: the
    identity's address (one ASCII mailbox). Anything else: None (no offer).
    """
    if provider == "github":
        login = user_info.get("login")
        if isinstance(login, str) and _GITHUB_LOGIN_RE.fullmatch(login):
            return login
        return None
    if provider == "google":
        address = user_info.get("email")
        if (
            isinstance(address, str)
            and address.isascii()
            and address.count("@") == 1
            and len(address) <= _ADDRESS_MAX
            and not any(ch.isspace() or ord(ch) < 32 for ch in address)
        ):
            return address
    return None


def display_name(value: object) -> str | None:
    """A provider display name for the page: controls removed, at most 80 characters."""
    if not isinstance(value, str):
        return None
    kept: list[str] = []
    for ch in value:
        category = unicodedata.category(ch)
        if category in _NAME_SPACED:
            kept.append(" ")
        elif category not in _NAME_DROPPED:
            kept.append(ch)
    text = " ".join("".join(kept).split())
    if not text:
        return None
    return text if len(text) <= _NAME_MAX else text[: _NAME_MAX - 1].rstrip() + "…"


def avatar_url(provider: str, value: object) -> str | None:
    """The avatar URL only when it is https on the provider's own avatar host."""
    if not isinstance(value, str) or len(value) > _AVATAR_MAX:
        return None
    try:
        parsed = urlparse(value)
    except ValueError:
        return None
    if parsed.scheme != "https" or parsed.hostname != _AVATAR_HOSTS.get(provider):
        return None
    if parsed.username or parsed.password or parsed.port:
        return None
    return value


def _well_formed_token(raw_token: str | None) -> bool:
    return bool(raw_token) and _TOKEN_RE.fullmatch(raw_token or "") is not None


def _record_key(raw_token: str) -> str:
    return _RECORD_KEY.format(digest=sha256_hex(raw_token))


def _pointer_key(account_user_id: str, provider: str, identity: str) -> str:
    return _POINTER_KEY.format(
        account=hmac_sha256_hex(account_user_id, _key()), provider=provider, identity=identity
    )


def _confirm_url(raw_token: str) -> str:
    base_url = get_settings().frontend_url.strip().rstrip("/")
    return f"{base_url}/link/confirm?token={raw_token}"


def _redis_client(redis: Any | None) -> Any:
    if redis is not None:
        return redis
    from db.redis import get_redis_client

    return get_redis_client()


async def _count(redis: Any, key: str) -> int | None:
    """INCR with a day's TTL; None when Redis cannot count."""
    try:
        count = int(await redis.incr(key))
        if count == 1 or int(await redis.ttl(key)) < 0:
            await redis.expire(key, _SEND_WINDOW_SECONDS)
        return count
    except Exception as exc:
        logger.warning("provider_link_confirm_limit_unavailable", error_type=type(exc).__name__)
        return None


async def _within_send_limits(redis: Any, account_user_id: str, identity: str) -> bool:
    """Count one mail against the account and the identity; False when over (fail closed)."""
    per_account = await _count(
        redis, _SENT_ACCOUNT_KEY.format(account=hmac_sha256_hex(account_user_id, _key()))
    )
    per_identity = await _count(redis, _SENT_IDENTITY_KEY.format(identity=identity))
    if per_account is None or per_identity is None:
        return False
    return per_account <= _SENDS_PER_DAY and per_identity <= _SENDS_PER_DAY


def _audit(
    *,
    user_id: str,
    action: str,
    provider: str,
    identity: str,
    ip_address: str | None,
    user_agent: str | None,
) -> AuditLog:
    return AuditLog(
        user_email=LINK_CONFIRMATION_ACTOR,
        user_id=user_id,
        action=action,
        resource=f"oauth_provider:{provider}",
        new_value_hash=hmac_sha256_hex(provider, _key()),
        user_metadata={"identity_hmac": identity},
        ip_address=ip_address,
        user_agent=user_agent,
    )


# ---------------------------------------------------------------------------
# Offer (after the callback's response)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _OfferMail:
    """The confirmation mail of a recorded offer.

    ``to_email`` is **personal data** and ``confirm_url`` carries the token:
    neither is ever logged.
    """

    to_email: str
    confirm_url: str
    provider_label: str
    provider_login: str
    locale: str | None


async def process_link_offer(
    *,
    account_user_id: str,
    account_stamp: str,
    provider: str,
    user_info: Mapping[str, Any],
    browser_hash: str,
    return_to: str | None,
    ip_address: str | None,
    user_agent: str | None,
    session_factory: Callable[[], AsyncSession] | None = None,
    email_service: EmailService | None = None,
    redis: Any | None = None,
) -> None:
    """Record a pending link and mail its confirmation; never raises.

    Args:
        account_user_id: The email sign-up account ``ensure_user`` found.
        account_stamp: :func:`auth.roles.account_stamp` of it at that moment.
        provider: ``google`` or ``github``.
        user_info: The provider's user info (``sub``, ``email``, ``login``,
            ``name``, ``picture``). **Personal data** — never logged.
        browser_hash: Digest of the browser nonce the sign-in was bound to.
        return_to: The flow's validated return path, or None.
        ip_address: Client address of the callback.
        user_agent: Client user agent of the callback.
        session_factory: Override for tests; defaults to the app's factory.
        email_service: Override for tests; defaults to the configured one.
        redis: Override for tests; defaults to the async client.
    """
    stamp_at_offer = account_stamp
    mail: _OfferMail | None = None
    try:
        if not get_settings().enable_provider_link_confirmation:
            return
        if provider not in PROVIDER_LABELS:
            return
        oauth_sub = user_info.get("sub")
        identity_address = user_info.get("email")
        login = provider_login(provider, user_info)
        if not isinstance(oauth_sub, str) or not oauth_sub or not isinstance(identity_address, str):
            return
        if login is None:
            logger.info(
                "provider_link_confirmation_skipped", reason="provider_login", provider=provider
            )
            return
        identity = identity_hmac(provider, oauth_sub)
        client = _redis_client(redis)
        if session_factory is None:
            from db.base import _get_session_factory

            session_factory = _get_session_factory()

        async with session_factory() as db:
            user = (
                await db.execute(select(User).where(User.user_id == account_user_id))
            ).scalar_one_or_none()
            if user is None:
                return
            refusal = email_account_link_refusal(user, address=identity_address)
            if refusal is None and not hmac.compare_digest(
                compute_account_stamp(user), stamp_at_offer
            ):
                refusal = "account_changed"
            if refusal is not None:
                logger.info(
                    "provider_link_confirmation_skipped",
                    reason=refusal,
                    provider=provider,
                    identity_hmac=identity,
                )
                return
            from services.security_notification_service import resolve_deliverable_address

            address = await resolve_deliverable_address(db, account_user_id)
            if address is None:
                logger.info("provider_link_confirmation_skipped", reason="no_deliverable_address")
                return
            if not await _within_send_limits(client, account_user_id, identity):
                logger.info(
                    "provider_link_confirmation_throttled",
                    provider=provider,
                    identity_hmac=identity,
                )
                return

            raw_token = secrets.token_urlsafe(32)
            now = utcnow()
            record = PendingProviderLink(
                account_user_id=account_user_id,
                email_hmac=_address_hmac(user.email),
                provider=provider,  # type: ignore[arg-type]
                oauth_sub=oauth_sub,
                identity_hmac=identity,
                provider_login=login,
                provider_name=display_name(user_info.get("name")),
                provider_avatar_url=avatar_url(provider, user_info.get("picture")),
                browser_hash=browser_hash,
                account_stamp=stamp_at_offer,
                return_to=return_to,
                created_at=now.isoformat(),
                expires_at=(now + LINK_CONFIRM_TTL).isoformat(),
            )
            ttl = int(LINK_CONFIRM_TTL.total_seconds())
            pointer = _pointer_key(account_user_id, provider, identity)
            previous = await client.get(pointer)
            if isinstance(previous, str) and previous:
                await client.delete(_RECORD_KEY.format(digest=previous))
            await client.set(_record_key(raw_token), record.to_json(), ex=ttl)
            await client.set(pointer, sha256_hex(raw_token), ex=ttl)
            db.add(
                _audit(
                    user_id=account_user_id,
                    action=SENT_ACTION,
                    provider=provider,
                    identity=identity,
                    ip_address=ip_address,
                    user_agent=user_agent,
                )
            )
            await db.commit()
            mail = _OfferMail(
                to_email=address,
                confirm_url=_confirm_url(raw_token),
                provider_label=PROVIDER_LABELS[provider],
                provider_login=login,
                locale=user.locale,
            )
        logger.info("provider_link_confirmation_offered", provider=provider, identity_hmac=identity)
    except Exception as exc:
        # Type only: a driver error's text could echo an address.
        logger.error("provider_link_confirmation_offer_failed", error_type=type(exc).__name__)
        return
    if mail is None:
        return

    service = email_service or get_email_service()
    try:
        sent = await asyncio.wait_for(
            service.send_provider_link_confirmation(
                to_email=mail.to_email,
                confirm_url=mail.confirm_url,
                provider_label=mail.provider_label,
                provider_login=mail.provider_login,
                expires_in_minutes=int(LINK_CONFIRM_TTL.total_seconds() // 60),
                locale=mail.locale,
            ),
            timeout=EMAIL_SEND_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.error("provider_link_confirmation_email_failed", error_type=type(exc).__name__)
        return
    if not sent:
        logger.error("provider_link_confirmation_email_failed", error_type="send_returned_false")


def spawn_link_offer(**kwargs: Any) -> None:
    """Start :func:`process_link_offer` as a detached task; never raises.

    Detached (not a ``BackgroundTask`` on the redirect), so the callback's
    request ends at the same moment whether or not an offer follows.
    """
    try:
        task = asyncio.get_running_loop().create_task(process_link_offer(**kwargs))
        _spawned.add(task)
        task.add_done_callback(_spawned.discard)
    except Exception as exc:
        logger.error("provider_link_confirmation_schedule_failed", error_type=type(exc).__name__)


# ---------------------------------------------------------------------------
# Preview / complete / cancel (the link holder)
# ---------------------------------------------------------------------------


class ProviderLinkConfirmationService:
    """Preview, complete and cancel a pending provider link."""

    def __init__(self, db: AsyncSession, redis: Any | None = None) -> None:
        """Bind the service to a session.

        Args:
            db: The async session.
            redis: Override for tests; defaults to the async client.
        """
        self.db = db
        self._redis = redis

    @property
    def redis(self) -> Any:
        if self._redis is None:
            self._redis = _redis_client(None)
        return self._redis

    async def _read(self, raw_token: str) -> PendingProviderLink:
        if not _well_formed_token(raw_token):
            raise PasswordLinkInvalidError()
        record = PendingProviderLink.from_json(await self.redis.get(_record_key(raw_token)))
        if record is None:
            raise PasswordLinkInvalidError()
        return record

    async def preview(self, *, raw_token: str, browser_nonce: str | None) -> ProviderLinkPreview:
        """The provider account a live link connects; consumes nothing.

        Raises:
            PasswordLinkInvalidError: Unknown, expired or used link (400).
        """
        record = await self._read(raw_token)
        return ProviderLinkPreview(
            provider=record.provider,
            provider_login=record.provider_login,
            provider_name=record.provider_name,
            provider_avatar_url=record.provider_avatar_url,
            expires_at=datetime.fromisoformat(record.expires_at),
            same_browser=oauth_browser.matches(browser_nonce, record.browser_hash),
        )

    async def complete(
        self,
        *,
        raw_token: str,
        browser_nonce: str | None,
        ip_address: str | None,
        user_agent: str | None,
    ) -> ConfirmedProviderLink:
        """Connect the pending identity to the account.

        Raises:
            ProviderLinkWrongBrowserError: This browser did not start the
                sign-in; the pending link is kept (403).
            PasswordLinkInvalidError: Unknown, expired or used link, or the
                account cannot take the link any more; the link is spent (400).
        """
        record = await self._read(raw_token)
        if not oauth_browser.matches(browser_nonce, record.browser_hash):
            logger.info(
                "provider_link_confirmation_wrong_browser",
                provider=record.provider,
                identity_hmac=record.identity_hmac,
            )
            raise ProviderLinkWrongBrowserError()
        taken = PendingProviderLink.from_json(await self.redis.getdel(_record_key(raw_token)))
        if taken is None or taken != record:
            raise PasswordLinkInvalidError()
        await self.redis.delete(
            _pointer_key(record.account_user_id, record.provider, record.identity_hmac)
        )
        try:
            confirmed = await self._link(record, ip_address=ip_address, user_agent=user_agent)
        except PasswordLinkInvalidError:
            raise
        except BaseException:
            await self.db.rollback()
            raise

        from services.security_notification_service import (
            PROVIDER_SIGN_IN_LABELS,
            SecurityEvent,
            spawn_security_notification,
        )

        spawn_security_notification(
            user_id=confirmed.user_id,
            event=SecurityEvent.SIGN_IN_METHOD_LINK_CONFIRMED,
            ip=ip_address,
            user_agent=user_agent,
            sign_in_method=(
                f"{PROVIDER_SIGN_IN_LABELS[record.provider]} ({record.provider_login})"
            ),
        )
        return confirmed

    async def _refuse(self, record: PendingProviderLink, reason: str) -> PasswordLinkInvalidError:
        await self.db.rollback()
        logger.info(
            "provider_link_confirmation_refused",
            reason=reason,
            provider=record.provider,
            identity_hmac=record.identity_hmac,
        )
        return PasswordLinkInvalidError()

    async def _link(
        self,
        record: PendingProviderLink,
        *,
        ip_address: str | None,
        user_agent: str | None,
    ) -> ConfirmedProviderLink:
        db = self.db
        user = (
            await db.execute(
                select(User)
                .where(User.user_id == record.account_user_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if user is None:
            raise await self._refuse(record, "account_gone")
        if not hmac.compare_digest(_address_hmac(user.email), record.email_hmac):
            raise await self._refuse(record, "address_changed")
        refusal = email_account_link_refusal(user, address=user.email)
        if refusal is not None:
            raise await self._refuse(record, refusal)
        if not hmac.compare_digest(compute_account_stamp(user), record.account_stamp):
            raise await self._refuse(record, "account_changed")
        same_kind = (
            await db.execute(
                select(UserOAuthProvider.id)
                .where(
                    UserOAuthProvider.user_id == user.user_id,
                    UserOAuthProvider.provider == record.provider,
                )
                .limit(1)
            )
        ).first()
        if same_kind is not None:
            raise await self._refuse(record, "provider_already_linked")
        elsewhere = (
            await db.execute(
                select(UserOAuthProvider.id)
                .where(
                    UserOAuthProvider.provider == record.provider,
                    UserOAuthProvider.oauth_sub == record.oauth_sub,
                )
                .limit(1)
            )
        ).first()
        if elsewhere is not None:
            raise await self._refuse(record, "identity_linked_elsewhere")
        own_account = (
            await db.execute(select(User.id).where(User.user_id == record.oauth_sub).limit(1))
        ).first()
        if own_account is not None:
            raise await self._refuse(record, "identity_has_an_account")

        stage_address_link(
            db,
            owner_id=user.user_id,
            provider=record.provider,
            oauth_sub=record.oauth_sub,
            action=CONFIRMED_ACTION,
            actor_email=LINK_CONFIRMATION_ACTOR,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        user.last_login_at = utcnow()
        confirmed = ConfirmedProviderLink(
            user_id=user.user_id,
            email=user.email,
            name=user.name,
            picture=user.picture,
            role=Role(user.role),
            provider=record.provider,
            oauth_sub=record.oauth_sub,
            provider_login=record.provider_login,
            return_to=record.return_to,
            account_stamp=record.account_stamp,
        )
        try:
            await db.commit()
        except IntegrityError:
            # The identity was linked meanwhile (UNIQUE provider + sub, or
            # user + provider): nothing was connected.
            raise await self._refuse(record, "integrity") from None
        logger.info(
            "provider_link_confirmed",
            user_id=confirmed.user_id,
            provider=record.provider,
            identity_hmac=record.identity_hmac,
        )
        return confirmed

    async def cancel(
        self, *, raw_token: str, ip_address: str | None, user_agent: str | None
    ) -> None:
        """ "This wasn't me": discard the pending link and block further offers.

        Idempotent and silent: an unknown, expired or used link changes
        nothing. With a live one the record is deleted and the account gets
        ``provider_auto_link_blocked_at`` (kept when already set), so the
        same sign-in mails nobody again and Google no longer links
        automatically. Connecting a provider from Profile still works.

        The record is taken first (a completion racing this ends one way
        only); when the database step fails it is put back for the rest of
        its lifetime, so a retry of the same link still sets the marker.
        """
        if not _well_formed_token(raw_token):
            return
        record = PendingProviderLink.from_json(await self.redis.getdel(_record_key(raw_token)))
        if record is None:
            return
        try:
            user = (
                await self.db.execute(
                    select(User)
                    .where(User.user_id == record.account_user_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if user is None or not hmac.compare_digest(
                _address_hmac(user.email), record.email_hmac
            ):
                await self.db.rollback()
                await self._drop_pointer(record)
                return
            if user.provider_auto_link_blocked_at is None:
                user.provider_auto_link_blocked_at = utcnow()
            self.db.add(
                _audit(
                    user_id=user.user_id,
                    action=DECLINED_ACTION,
                    provider=record.provider,
                    identity=record.identity_hmac,
                    ip_address=ip_address,
                    user_agent=user_agent,
                )
            )
            await self.db.commit()
        except BaseException:
            await self.db.rollback()
            await self._put_back(raw_token, record)
            raise
        await self._drop_pointer(record)
        logger.info(
            "provider_link_confirmation_declined",
            provider=record.provider,
            identity_hmac=record.identity_hmac,
        )

    async def _drop_pointer(self, record: PendingProviderLink) -> None:
        await self.redis.delete(
            _pointer_key(record.account_user_id, record.provider, record.identity_hmac)
        )

    async def _put_back(self, raw_token: str, record: PendingProviderLink) -> None:
        """Restore a taken record for the rest of its lifetime. Never raises."""
        try:
            remaining = int((datetime.fromisoformat(record.expires_at) - utcnow()).total_seconds())
            if remaining > 0:
                await self.redis.set(
                    _record_key(raw_token), record.to_json(), ex=remaining, nx=True
                )
        except Exception as exc:  # noqa: BLE001 - the caller re-raises the real error
            logger.warning(
                "provider_link_confirmation_restore_failed",
                provider=record.provider,
                error_type=type(exc).__name__,
            )
