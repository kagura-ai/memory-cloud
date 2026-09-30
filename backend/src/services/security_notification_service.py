"""Security-change notification emails (Issue #1752).

The account owner is emailed whenever a security-sensitive change happens on
their account: a password set, changed, reset or removed; a sign-in provider
unlinked; an OAuth / MCP client authorized for the first time; an API key
created or regenerated; an OAuth client secret regenerated. The notices are
mandatory (no opt-out).

Routes call :func:`schedule_security_notification` after the change has
committed. It adds :func:`notify_security_event` to the request's
``BackgroundTasks``, which runs after the response on its own DB session. A
failure anywhere (DB, Redis, email provider) is logged and swallowed: a notice
never changes the API response and never rolls the change back.

Coalescing: at most one email per (user, event) per window
(``security_notification_window_seconds``, default 10 minutes). The first
occurrence is sent at once and opens a window, recorded as a member of a Redis
sorted set scored by the window's end. Later occurrences in the window are
buffered in a Redis list; when the window closes, the every-minute job
:func:`flush_due_security_notifications` claims it with ``ZREM`` (only the
caller whose ``ZREM`` removed the member sends, so several API processes can
run the job) and sends one digest listing them. When Redis is unavailable the
occurrence is sent at once: the failure mode is an extra email, never silence.

Recipient: the account's email when it is known to be deliverable — verified
by the password flow (``email_verified_at``) or taken from a sign-in provider
that verified it (a ``user_oauth_providers`` row; accounts are created only
from provider-verified addresses and addresses cannot be changed). Local CLI
accounts (``@local``) are never emailed.

Content safety: client names are set by whoever registers a client (Dynamic
Client Registration is public), user agents are client-supplied and key names
are user-chosen, so each is stripped of control characters, collapsed, capped
and defanged (no clickable URL survives) before it reaches the email. The body
never carries a secret, token, key value or prefix, or an action link.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, cast

from fastapi import BackgroundTasks, Request
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from config.settings import get_settings
from db.base import _get_session_factory
from db.redis import get_redis_client
from models.auth import (
    OAuth2AuthorizationCode,
    OAuth2Client,
    OAuth2DeviceCode,
    OAuth2Token,
    User,
    UserOAuthProvider,
)
from services.email_service import EmailService, get_email_service
from utils.datetime import utcnow
from utils.logger import get_logger

logger = get_logger(__name__)

# Same bound as the password emails: a stuck provider must not hold the
# background task (or the flush job) open indefinitely.
_EMAIL_TIMEOUT_SECONDS = 10.0

# Redis layout. The sorted set holds one member per open window,
# ``"<user_id>|<event>"``, scored by the window's end (epoch seconds); the list
# holds the occurrences buffered in that window as JSON.
_DUE_KEY = "security_notify:due"
_BUFFER_KEY = "security_notify:buffer:{user_id}:{event}"
_MEMBER_SEPARATOR = "|"
# Windows claimed per job run; the rest wait for the next minute.
_FLUSH_BATCH = 200
# Occurrences listed in one digest; the count of the rest is still reported.
_DIGEST_MAX_OCCURRENCES = 20

# Length caps for untrusted display strings.
_NAME_MAX_CHARS = 80
_USER_AGENT_MAX_CHARS = 200
_IP_MAX_CHARS = 64


class SecurityEvent(StrEnum):
    """The security-sensitive changes that email the account owner."""

    PASSWORD_SET = "password_set"
    PASSWORD_CHANGED = "password_changed"
    PASSWORD_RESET = "password_reset"
    OAUTH_CLIENT_AUTHORIZED = "oauth_client_authorized"
    API_KEY_CREATED = "api_key_created"
    API_KEY_REGENERATED = "api_key_regenerated"
    OAUTH_SECRET_REGENERATED = "oauth_secret_regenerated"
    SIGN_IN_METHOD_REMOVED = "sign_in_method_removed"


# (subject, one-line description of one occurrence)
_EVENT_TEXT: dict[SecurityEvent, tuple[str, str]] = {
    SecurityEvent.PASSWORD_SET: (
        "A password was added to your Kagura account",
        "A password was added to your account.",
    ),
    SecurityEvent.PASSWORD_CHANGED: (
        "Your Kagura password was changed",
        "Your password was changed.",
    ),
    SecurityEvent.PASSWORD_RESET: (
        "Your Kagura password was reset",
        "Your password was reset with a link sent to this address.",
    ),
    SecurityEvent.OAUTH_CLIENT_AUTHORIZED: (
        "A new app was authorized to access your Kagura account",
        "A new app was authorized to access your account.",
    ),
    SecurityEvent.API_KEY_CREATED: (
        "A new API key was created for your Kagura account",
        "A new API key was created.",
    ),
    SecurityEvent.API_KEY_REGENERATED: (
        "An API key of your Kagura account was regenerated",
        "An API key was regenerated (the old key stopped working).",
    ),
    SecurityEvent.OAUTH_SECRET_REGENERATED: (
        "An OAuth client secret of your Kagura account was regenerated",
        "An OAuth client secret was regenerated (the old secret stopped working).",
    ),
    SecurityEvent.SIGN_IN_METHOD_REMOVED: (
        "A sign-in method was removed from your Kagura account",
        "A sign-in method was removed from your account.",
    ),
}


# ---------------------------------------------------------------------------
# Sanitizing untrusted display strings
# ---------------------------------------------------------------------------

# C0/C1 controls (newlines included), zero-width and bidi-override characters,
# line/paragraph separators, BOM.
_UNSAFE_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2060-\u2069\ufeff]")
_SCHEME_RE = re.compile(r"://")
_WWW_RE = re.compile(r"(?i)\bwww\.")
# A host-looking token: labels joined by dots ending in an alphabetic TLD
# (``evil.example``, ``a.b.co``). Version numbers such as ``1.2.3`` do not match.
_HOST_RE = re.compile(r"(?i)\b(?:[a-z0-9-]+\.)+[a-z]{2,}\b")


def sanitize_display_text(value: object, max_chars: int) -> str | None:
    """Make an untrusted string safe to show in a plain-text email.

    Strips control, zero-width and bidi characters (so no header-like lines or
    reordered text can be injected), collapses whitespace, defangs URL-like
    text so mail clients do not turn it into a link (``://`` becomes
    ``[:]//``, a dot in a host name becomes ``[.]``), and caps the length.
    Applying it twice gives the same result.

    Args:
        value: The untrusted value (``None`` passes through).
        max_chars: Maximum length of the result, ellipsis included.

    Returns:
        The safe string, or ``None`` when nothing printable is left.
    """
    if value is None:
        return None
    text = _UNSAFE_CHARS_RE.sub(" ", str(value))
    text = " ".join(text.split())
    text = _SCHEME_RE.sub("[:]//", text)
    text = _WWW_RE.sub(lambda m: m.group(0)[:-1] + "[.]", text)
    text = _HOST_RE.sub(lambda m: m.group(0).replace(".", "[.]"), text)
    if not text:
        return None
    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + "…"
    return text


def sanitize_ip(value: object) -> str | None:
    """Return ``value`` when it is an IP address, else a sanitized short form."""
    if not value:
        return None
    try:
        return str(ipaddress.ip_address(str(value).strip()))
    except ValueError:
        return sanitize_display_text(value, _IP_MAX_CHARS)


# ---------------------------------------------------------------------------
# Occurrences and the email body
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SecurityOccurrence:
    """One occurrence of a security event, as listed in the email.

    Every field is display text that has already been sanitized. None of them
    may ever carry a secret, token, key value or link.

    Attributes:
        occurred_at: UTC time, ``YYYY-MM-DDTHH:MM:SS UTC``.
        ip: Client IP address of the request that made the change.
        user_agent: Client user agent (sanitized, capped).
        client_name: Name the OAuth client registered itself with.
        key_name: Name of the API key.
        sign_in_method: The removed sign-in method (``Password``, ``Google``...).
        actor: Who made the change when it was not the owner:
            ``"Display Name (email)"`` of the acting administrator.
    """

    occurred_at: str
    ip: str | None = None
    user_agent: str | None = None
    client_name: str | None = None
    key_name: str | None = None
    sign_in_method: str | None = None
    actor: str | None = None

    def to_json(self) -> str:
        """Serialize for the Redis buffer."""
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> SecurityOccurrence:
        """Deserialize from the Redis buffer, re-sanitizing every field.

        Raises:
            ValueError: The payload is not an occurrence.
        """
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get("occurred_at"), str):
            raise ValueError("not a security occurrence")
        known = {f.name for f in fields(cls)}
        values = {k: v for k, v in data.items() if k in known}
        return cls(
            occurred_at=sanitize_display_text(values["occurred_at"], 40) or "",
            ip=sanitize_ip(values.get("ip")),
            user_agent=sanitize_display_text(values.get("user_agent"), _USER_AGENT_MAX_CHARS),
            client_name=sanitize_display_text(values.get("client_name"), _NAME_MAX_CHARS),
            key_name=sanitize_display_text(values.get("key_name"), _NAME_MAX_CHARS),
            sign_in_method=sanitize_display_text(values.get("sign_in_method"), _NAME_MAX_CHARS),
            actor=sanitize_display_text(values.get("actor"), 2 * _NAME_MAX_CHARS),
        )


def format_occurred_at(when: datetime) -> str:
    """Format a naive-UTC (or aware) instant as ``YYYY-MM-DDTHH:MM:SS UTC``."""
    if when.tzinfo is not None:
        when = when.astimezone(UTC).replace(tzinfo=None)
    return when.strftime("%Y-%m-%dT%H:%M:%S") + " UTC"


def profile_url() -> str:
    """The profile page the "Wasn't you?" paragraph points at (no token)."""
    base_url = get_settings().frontend_url.strip().rstrip("/")
    return f"{base_url}/profile"


def render_security_notification(
    event: SecurityEvent | str,
    occurrences: list[SecurityOccurrence],
    *,
    digest: bool,
    window_minutes: int,
    profile_page_url: str,
) -> tuple[str, str]:
    """Build the subject and plain-text body of a security notice.

    Args:
        event: The event type.
        occurrences: What to list (at least one).
        digest: True for the trailing email that lists the occurrences
            buffered after the first one of a window.
        window_minutes: The coalescing window, for the digest wording.
        profile_page_url: ``<FRONTEND_URL>/profile``.

    Returns:
        ``(subject, text)``.
    """
    event = SecurityEvent(event)
    subject, description = _EVENT_TEXT[event]
    count = len(occurrences)
    listed = occurrences[:_DIGEST_MAX_OCCURRENCES]

    lines: list[str] = []
    if digest:
        subject = f"{subject} ({count} more {'time' if count == 1 else 'times'})"
        lines += [
            "This is a follow-up to a security notice we sent you a few minutes ago.",
            f"The same change happened {count} more {'time' if count == 1 else 'times'} "
            f"in the {window_minutes} minutes after it:",
        ]
    else:
        lines += [
            "A security-sensitive change was made to your Kagura Memory Cloud account.",
        ]
    lines.append("")

    for occurrence in listed:
        lines.append(f"  - {description}")
        lines.append(f"    When:        {occurrence.occurred_at}")
        lines.append(f"    IP address:  {occurrence.ip or 'unknown'}")
        lines.append(f"    Device:      {occurrence.user_agent or 'unknown'}")
        if occurrence.client_name:
            lines.append(f'    App:         "{occurrence.client_name}"')
            lines.append("                 (the name the app registered itself with)")
        if occurrence.key_name:
            lines.append(f'    Key name:    "{occurrence.key_name}"')
        if occurrence.sign_in_method:
            lines.append(f"    Removed:     {occurrence.sign_in_method}")
        if occurrence.actor:
            lines.append(f"    Done by:     {occurrence.actor}, an administrator of your")
            lines.append("                 workspace, on your account")
        lines.append("")
    if count > len(listed):
        lines += [f"  ... and {count - len(listed)} more.", ""]

    lines += [
        "Wasn't you?",
        "Sign in and review your sign-in methods on your profile page, and your",
        "keys and apps under Integrations > API Keys and OAuth Apps in your",
        "workspace. Remove anything you do not recognize:",
        "",
        f"  {profile_page_url}",
        "",
        "Type the address yourself or use a bookmark if you prefer. If you cannot",
        'sign in, use "Forgot password?" on the sign-in page to reset your',
        "password. We never ask for your password or keys by email.",
        "",
        "You receive this notice for every security-sensitive change to your",
        "account; it cannot be turned off.",
    ]
    return subject, "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Recipient
# ---------------------------------------------------------------------------


def _is_local_address(email: str) -> bool:
    return email.strip().lower().endswith("@local")


async def resolve_deliverable_address(db: AsyncSession, user_id: str) -> str | None:
    """Return the owner's address when a notice can be delivered to it.

    Deliverable means verified by the password flow (``email_verified_at``)
    or taken from a sign-in provider that verified it (a linked
    ``user_oauth_providers`` row). ``@local`` addresses never are.

    Args:
        db: Async session.
        user_id: The account's ``users.user_id``.

    Returns:
        The address, or ``None`` when nothing should be sent.
    """
    user = (await db.execute(select(User).where(User.user_id == user_id))).scalar_one_or_none()
    if user is None or not user.email or "@" not in user.email:
        return None
    if _is_local_address(user.email):
        return None
    if user.email_verified_at is not None:
        return user.email
    has_provider = await db.scalar(select(exists().where(UserOAuthProvider.user_id == user_id)))
    return user.email if has_provider else None


async def _actor_label(db: AsyncSession, actor_user_id: str) -> str | None:
    """``"Display Name (email)"`` of the acting administrator."""
    actor = (
        await db.execute(select(User).where(User.user_id == actor_user_id))
    ).scalar_one_or_none()
    if actor is None:
        return "another account"
    name = sanitize_display_text(actor.name, _NAME_MAX_CHARS)
    email = sanitize_display_text(actor.email, _NAME_MAX_CHARS)
    if name and email:
        return f"{name} ({email})"
    return name or email or "another account"


async def _client_name(db: AsyncSession, client_id: str) -> str | None:
    name = await db.scalar(
        select(OAuth2Client.client_name).where(OAuth2Client.client_id == client_id)
    )
    return sanitize_display_text(name, _NAME_MAX_CHARS)


# ---------------------------------------------------------------------------
# First authorization of an OAuth client
# ---------------------------------------------------------------------------


def is_first_client_authorization(
    session: Session,
    *,
    client_id: str,
    user_id: str,
    exclude_device_code_id: int | None = None,
) -> bool:
    """Whether ``user_id`` has never authorized ``client_id`` before.

    Called before the new grant is written. A user has authorized a client
    before when any of these exists for the pair:

    - an ``oauth_tokens`` row, revoked or not (rows are only deleted with the
      client or the account, so this remembers every past grant);
    - an ``oauth_authorization_codes`` row (consented, not yet exchanged);
    - another ``oauth_device_codes`` row with ``authorized_at`` set (approved,
      not yet exchanged).

    Args:
        session: Sync session (the OAuth routes run Authlib on one).
        client_id: The client's ``client_id``.
        user_id: The consenting user.
        exclude_device_code_id: The device code being approved right now.

    Returns:
        True when no earlier grant exists.
    """
    if (
        session.query(OAuth2Token.id)
        .filter(OAuth2Token.client_id == client_id, OAuth2Token.user_id == user_id)
        .first()
        is not None
    ):
        return False
    if (
        session.query(OAuth2AuthorizationCode.id)
        .filter(
            OAuth2AuthorizationCode.client_id == client_id,
            OAuth2AuthorizationCode.user_id == user_id,
        )
        .first()
        is not None
    ):
        return False
    device_query = session.query(OAuth2DeviceCode.id).filter(
        OAuth2DeviceCode.client_id == client_id,
        OAuth2DeviceCode.user_id == user_id,
        OAuth2DeviceCode.authorized_at.is_not(None),
    )
    if exclude_device_code_id is not None:
        device_query = device_query.filter(OAuth2DeviceCode.id != exclude_device_code_id)
    return device_query.first() is None


# ---------------------------------------------------------------------------
# Scheduling and delivery
# ---------------------------------------------------------------------------


def schedule_security_notification(
    background_tasks: BackgroundTasks,
    *,
    user_id: str,
    event: SecurityEvent,
    request: Request | Any,
    key_name: str | None = None,
    client_id: str | None = None,
    client_name: str | None = None,
    sign_in_method: str | None = None,
    actor_user_id: str | None = None,
) -> None:
    """Queue the notice for a change that has just committed.

    Call this only after the change's commit. The notice runs after the
    response; nothing here touches the DB, Redis or the email provider.

    Args:
        background_tasks: The request's ``BackgroundTasks``.
        user_id: The account owner (the recipient).
        event: What happened.
        request: The request that made the change (IP and user agent).
        key_name: API key name, for key events.
        client_id: OAuth client id; its registered name is looked up later.
        client_name: OAuth client name, when the caller already has it.
        sign_in_method: The removed sign-in method.
        actor_user_id: The acting user when it is not the owner (an admin
            acting on the owner's credentials).
    """
    client = getattr(request, "client", None)
    headers = getattr(request, "headers", None) or {}
    background_tasks.add_task(
        notify_security_event,
        user_id,
        event,
        ip=getattr(client, "host", None) if client else None,
        user_agent=headers.get("user-agent"),
        occurred_at=utcnow(),
        key_name=key_name,
        client_id=client_id,
        client_name=client_name,
        sign_in_method=sign_in_method,
        actor_user_id=actor_user_id if actor_user_id != user_id else None,
    )


async def notify_security_event(
    user_id: str,
    event: SecurityEvent | str,
    *,
    ip: str | None,
    user_agent: str | None,
    occurred_at: datetime | None = None,
    key_name: str | None = None,
    client_id: str | None = None,
    client_name: str | None = None,
    sign_in_method: str | None = None,
    actor_user_id: str | None = None,
    session_factory: Callable[[], AsyncSession] | None = None,
    email_service: EmailService | None = None,
) -> None:
    """Email the owner about one occurrence, or buffer it in an open window.

    Runs after the response; never raises.

    Args:
        user_id: The account owner.
        event: What happened.
        ip: Client IP of the request that made the change.
        user_agent: Client user agent (untrusted).
        occurred_at: When it happened (naive UTC); defaults to now.
        key_name: API key name (untrusted).
        client_id: OAuth client whose registered name is shown.
        client_name: OAuth client name (untrusted), when already known.
        sign_in_method: The removed sign-in method.
        actor_user_id: The acting administrator, when not the owner.
        session_factory: Override for tests; defaults to the app's factory.
        email_service: Override for tests; defaults to the configured one.
    """
    try:
        event = SecurityEvent(event)
        factory = session_factory or _get_session_factory()
        async with factory() as db:
            recipient = await resolve_deliverable_address(db, user_id)
            if recipient is None:
                logger.info(
                    "security_notification_skipped", user_id=user_id, security_event=event.value
                )
                return
            if client_id and not client_name:
                client_name = await _client_name(db, client_id)
            actor = await _actor_label(db, actor_user_id) if actor_user_id else None
        occurrence = SecurityOccurrence(
            occurred_at=format_occurred_at(occurred_at or utcnow()),
            ip=sanitize_ip(ip),
            user_agent=sanitize_display_text(user_agent, _USER_AGENT_MAX_CHARS),
            client_name=sanitize_display_text(client_name, _NAME_MAX_CHARS),
            key_name=sanitize_display_text(key_name, _NAME_MAX_CHARS),
            sign_in_method=sanitize_display_text(sign_in_method, _NAME_MAX_CHARS),
            actor=actor,
        )
        if await _buffer_if_window_open(user_id, event, occurrence):
            logger.info(
                "security_notification_buffered", user_id=user_id, security_event=event.value
            )
            return
        await _deliver(recipient, user_id, event, [occurrence], digest=False, email=email_service)
    except Exception as exc:
        # Type only: a driver error's text could echo the address.
        logger.error(
            "security_notification_failed",
            user_id=user_id,
            security_event=str(event),
            error_type=type(exc).__name__,
        )


def _member(user_id: str, event: SecurityEvent) -> str:
    return f"{user_id}{_MEMBER_SEPARATOR}{event.value}"


def _buffer_key(user_id: str, event: SecurityEvent | str) -> str:
    return _BUFFER_KEY.format(user_id=user_id, event=str(event))


def _window_seconds() -> int:
    return get_settings().security_notification_window_seconds


async def _buffer_if_window_open(
    user_id: str, event: SecurityEvent, occurrence: SecurityOccurrence
) -> bool:
    """Open a window, or buffer ``occurrence`` in the one already open.

    Returns:
        True when the occurrence was buffered (the digest will list it);
        False when it must be sent now — it opened the window, or Redis is
        unavailable (fail open to notifying).
    """
    window = _window_seconds()
    member = _member(user_id, event)
    buffer_key = _buffer_key(user_id, event)
    try:
        client = get_redis_client()
        opened = await client.zadd(_DUE_KEY, {member: _now_score() + window}, nx=True)
        if opened:
            return False
        payload = occurrence.to_json()
        pipe = client.pipeline(transaction=True)
        pipe.rpush(buffer_key, payload)
        # Orphaned buffers (a lost window) expire on their own.
        pipe.expire(buffer_key, 3 * window + 3600)
        await pipe.execute()
        # The window may have been claimed between the ZADD and the RPUSH; the
        # claim then missed this occurrence. Take it back and send it now.
        if await client.zscore(_DUE_KEY, member) is None:
            if await client.lrem(buffer_key, 1, payload):
                return False
        return True
    except Exception as exc:
        logger.warning(
            "security_notification_redis_unavailable",
            user_id=user_id,
            security_event=event.value,
            error_type=type(exc).__name__,
        )
        return False


def _now_score() -> float:
    """Current time as epoch seconds (the sorted set's score unit).

    ``utcnow()`` is naive UTC and ``timestamp()`` on a naive value assumes
    local time, so the zone is attached first.
    """
    return utcnow().replace(tzinfo=UTC).timestamp()


async def _deliver(
    recipient: str,
    user_id: str,
    event: SecurityEvent,
    occurrences: list[SecurityOccurrence],
    *,
    digest: bool,
    email: EmailService | None,
) -> bool:
    """Send one notice; log (never raise) on failure."""
    service = email or get_email_service()
    try:
        sent = await asyncio.wait_for(
            service.send_security_notification(
                to_email=recipient,
                event=event.value,
                occurrences=occurrences,
                digest=digest,
                window_minutes=max(1, _window_seconds() // 60),
                profile_page_url=profile_url(),
            ),
            timeout=_EMAIL_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.error(
            "security_notification_send_failed",
            user_id=user_id,
            security_event=event.value,
            error_type=type(exc).__name__,
        )
        return False
    if not sent:
        logger.error(
            "security_notification_send_failed",
            user_id=user_id,
            security_event=event.value,
            error_type="send_returned_false",
        )
        return False
    logger.info(
        "security_notification_sent",
        user_id=user_id,
        security_event=event.value,
        occurrences=len(occurrences),
        digest=digest,
    )
    return True


async def flush_due_security_notifications(
    *,
    now_score: float | None = None,
    session_factory: Callable[[], AsyncSession] | None = None,
    email_service: EmailService | None = None,
) -> int:
    """Close every due window and send its digest (the every-minute job).

    Each window is claimed with ``ZREM``; only the caller that removed the
    member reads and deletes the buffer and sends, so concurrent runs in
    several processes send each digest once. A window with nothing buffered
    sends nothing. Never raises.

    Args:
        now_score: Current epoch seconds (tests pin it).
        session_factory: Override for tests; defaults to the app's factory.
        email_service: Override for tests; defaults to the configured one.

    Returns:
        Number of digests sent.
    """
    try:
        client = get_redis_client()
        due = await client.zrangebyscore(
            _DUE_KEY,
            "-inf",
            now_score if now_score is not None else _now_score(),
            start=0,
            num=_FLUSH_BATCH,
        )
    except Exception as exc:
        logger.warning("security_notification_flush_unavailable", error_type=type(exc).__name__)
        return 0

    sent = 0
    for raw_member in due:
        member = cast(str, raw_member)
        user_id, _, event_value = member.rpartition(_MEMBER_SEPARATOR)
        try:
            if await client.zrem(_DUE_KEY, member) != 1:
                continue  # claimed by another process
            buffer_key = _buffer_key(user_id, event_value)
            pipe = client.pipeline(transaction=True)
            pipe.lrange(buffer_key, 0, -1)
            pipe.delete(buffer_key)
            raw_items, _ = await pipe.execute()
            if not raw_items:
                continue
            event = SecurityEvent(event_value)
            occurrences: list[SecurityOccurrence] = []
            for raw in raw_items:
                try:
                    occurrences.append(SecurityOccurrence.from_json(raw))
                except (ValueError, TypeError):
                    logger.warning(
                        "security_notification_bad_buffer_item", security_event=event_value
                    )
            if not occurrences:
                continue
            factory = session_factory or _get_session_factory()
            async with factory() as db:
                recipient = await resolve_deliverable_address(db, user_id)
            if recipient is None:
                logger.info(
                    "security_notification_skipped", user_id=user_id, security_event=event_value
                )
                continue
            if await _deliver(
                recipient, user_id, event, occurrences, digest=True, email=email_service
            ):
                sent += 1
        except Exception as exc:
            logger.error(
                "security_notification_flush_failed",
                user_id=user_id,
                security_event=event_value,
                error_type=type(exc).__name__,
            )
    return sent
