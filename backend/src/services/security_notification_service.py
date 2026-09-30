"""Security-change notification emails (Issue #1752).

The account owner is emailed whenever a security-sensitive change happens on
their account: a password set, changed, reset or removed; a sign-in provider
linked or unlinked; an OAuth / MCP client authorized (by consent: the first
time; by device-flow approval: every time); an API key created or regenerated;
an OAuth client created or its secret regenerated. The notices are mandatory
(no opt-out).

Routes call :func:`schedule_security_notification` after the change has
committed. It adds :func:`notify_security_event` to the request's
``BackgroundTasks``, which runs after the response on its own DB session. A
failure anywhere (DB, Redis, email provider) is logged and swallowed: a notice
never changes the API response and never rolls the change back.

Coalescing: at most one email per (user, event) per window
(``security_notification_window_seconds``, default 10 minutes). The first
occurrence is sent at once and opens a window: it gets a random window id,
stored as the (user, event)'s open-window pointer and in a member of a Redis
sorted set scored by the window's end. Every other Redis key of the window
(buffer, counter, claim, attempts) carries that id, so a window's flush only
ever touches its own occurrences, never those of a newer window of the same
(user, event). Later occurrences in the window are buffered in a Redis list
(the first ``_DIGEST_MAX_OCCURRENCES`` are kept, a counter keeps the total).
Recording an occurrence is one optimistic Redis transaction watching only
that (user, event)'s pointer, which carries the window id and deadline: it is
buffered only while the pointer names a window whose deadline has not passed;
otherwise a new window is opened, pointer and due entry together. When the window closes, the
every-minute job :func:`flush_due_security_notifications` claims it in one
transaction too (due entry removed, pointer released if still its own, buffer
moved to a claim key) and sends one digest. The claim key is deleted once the
send is done; a definite failure (the provider refused or raised, the
recipient lookup failed, Redis failed mid-flush) re-queues the window a
bounded number of times. A timed-out send is uncertain (the provider call may still complete in
its thread), so it counts as sent: never a duplicate. When the first,
immediate send of a window definitely fails, the window is closed again, so
the next occurrence is sent at once rather than as a follow-up to a notice
that never arrived; occurrences buffered while that send was in flight are
sent right away as a notice of their own. A flush run is bounded by
``_FLUSH_BATCH`` windows and ``_FLUSH_TIME_BUDGET_SECONDS``; the rest wait for
the next run. When Redis is unavailable the occurrence is sent at once: the
failure mode is an extra email, never silence.

Pending state (pointers, buffers, counters, claims) is kept for
``_STATE_RETENTION_SECONDS`` (7 days) past its window's deadline, so a stalled
scheduler does not lose committed repeats; a due entry older than that is
dropped with a
``security_notification_window_expired`` warning, and a claimed window whose
details expired still gets a digest that reports how many occurrences could
not be listed.

Known limitation: delivery is at most once. When a process crashes after a
window was claimed and before its digest was sent, that digest is lost.

Recipient: the address on the account at send time, when it is verified
(``email_verified_at``): by the password flow (following an emailed link) or
by an OAuth sign-in whose IdP attested the address as verified
(``auth.roles.RoleManager.ensure_user``; a sign-in also syncs a verified
provider-side email change onto the account). A linked provider alone is not
proof. Local CLI accounts (``@local``) are never emailed.

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
import time
import unicodedata
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, cast

from fastapi import BackgroundTasks, Request
from redis.exceptions import WatchError
from sqlalchemy import select
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
)
from services.email_service import EmailService, get_email_service
from utils.datetime import utcnow
from utils.logger import get_logger

logger = get_logger(__name__)

# Same bound as the password emails: a stuck provider must not hold the
# background task (or the flush job) open indefinitely.
_EMAIL_TIMEOUT_SECONDS = 10.0

# Redis layout. ``_OPEN_KEY`` names the (user, event)'s open window as
# ``"<window id>:<deadline>"``. The sorted set holds one member per window, ``"<user_id>|<event>|<id>"``,
# scored by the window's end (or retry time; epoch seconds). Every other key is
# per window id.
_OPEN_KEY = "security_notify:open:{user_id}:{event}"
_DUE_KEY = "security_notify:due"
_BUFFER_KEY = "security_notify:buffer:{user_id}:{event}:{window_id}"
# Total occurrences buffered in the window (the list itself is capped).
_COUNT_KEY = "security_notify:count:{user_id}:{event}:{window_id}"
# Where a flush holds a claimed window's buffer and count until its send is done.
_CLAIM_BUFFER_KEY = "security_notify:claim:buffer:{user_id}:{event}:{window_id}"
_CLAIM_COUNT_KEY = "security_notify:claim:count:{user_id}:{event}:{window_id}"
# Definite failures of the window's digest so far.
_ATTEMPTS_KEY = "security_notify:attempts:{user_id}:{event}:{window_id}"
# A digest whose send definitely failed is tried this many times, then dropped.
_DIGEST_MAX_ATTEMPTS = 3
_DIGEST_RETRY_DELAY_SECONDS = 60
_MEMBER_SEPARATOR = "|"
# Windows claimed per job run, and the time after which a run stops claiming
# (sends are sequential with a 10 s timeout each); the rest wait for the next
# minute.
_FLUSH_BATCH = 50
_FLUSH_TIME_BUDGET_SECONDS = 45.0
# How long pending notice state is kept: the TTL of pointers, buffers,
# counters and claims, and the age after which a due entry the job never got
# to is dropped (with a ``security_notification_window_expired`` warning).
# Long, so a stalled scheduler does not silently lose committed repeats.
_STATE_RETENTION_SECONDS = 7 * 24 * 60 * 60
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
    SIGN_IN_METHOD_ADDED = "sign_in_method_added"
    OAUTH_CLIENT_CREATED = "oauth_client_created"


# How a linked / unlinked sign-in provider is named in a notice.
PROVIDER_SIGN_IN_LABELS = {"google": "Google sign-in", "github": "GitHub sign-in"}

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
    # Neutral wording: device-flow approvals notify every time, not only for
    # an app the account never authorized before.
    SecurityEvent.OAUTH_CLIENT_AUTHORIZED: (
        "An app was authorized to access your Kagura account",
        "An app was authorized to access your account.",
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
    SecurityEvent.SIGN_IN_METHOD_ADDED: (
        "A sign-in method was added to your Kagura account",
        "A new sign-in method was linked to your account.",
    ),
    SecurityEvent.OAUTH_CLIENT_CREATED: (
        "A new OAuth app was registered on your Kagura account",
        "A new OAuth app (with a client secret) was registered.",
    ),
}


# ---------------------------------------------------------------------------
# Sanitizing untrusted display strings
# ---------------------------------------------------------------------------

# Unicode categories removed outright: format characters (zero-width, bidi
# controls, soft hyphen, tag characters, U+061C, U+180E), private use,
# surrogates and unassigned code points. Removing (not spacing) them means a
# hidden character cannot split a host name to dodge the defang below.
_REMOVED_CATEGORIES = frozenset({"Cf", "Co", "Cs", "Cn"})
# Categories turned into a space: control characters (newlines included) and
# line / paragraph separators.
_SPACED_CATEGORIES = frozenset({"Cc", "Zl", "Zp"})
# Removed although they are combining marks: the combining grapheme joiner.
_REMOVED_CHARS = frozenset({"\u034f"})
# Dots NFKC does not fold but IDNA treats as a label separator.
_DOT_LIKE = str.maketrans({"\u3002": "."})
_SCHEME_RE = re.compile(r"://")
_WWW_RE = re.compile(r"(?i)\bwww\.")
# A host-looking token: labels of word characters (any script) joined by dots,
# ending in a letters-only or punycode TLD (``evil.example``, ``pаypal.com``,
# ``例え.jp``, ``evil.рф``, ``a.xn--p1ai``). Version numbers such as ``1.2.3``
# do not match.
_HOST_RE = re.compile(r"(?:[\w-]+\.)+(?:[^\W\d_]{2,}|xn--[\w-]+)\b")
# A dotted-quad IPv4 address (``1.2.3.4/login``).
_IPV4_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")


def _strip_unsafe_chars(text: str) -> str:
    out: list[str] = []
    for ch in text:
        category = unicodedata.category(ch)
        if category in _REMOVED_CATEGORIES or ch in _REMOVED_CHARS:
            continue
        out.append(" " if category in _SPACED_CATEGORIES else ch)
    return "".join(out)


def sanitize_display_text(value: object, max_chars: int) -> str | None:
    """Make an untrusted string safe to show in a plain-text email.

    Folds compatibility characters (NFKC: fullwidth letters, dots, colons and
    slashes become ASCII), removes format / private-use / unassigned characters
    and turns controls and line separators into spaces (so no header-like lines
    or reordered text can be injected), collapses whitespace, defangs URL-like
    text so mail clients do not turn it into a link (``://`` becomes
    ``[:]//``, a dot in a host name or an IPv4 address becomes ``[.]``, in any
    script), and caps the length. Applying it twice gives the same result.

    Args:
        value: The untrusted value (``None`` passes through).
        max_chars: Maximum length of the result, ellipsis included.

    Returns:
        The safe string, or ``None`` when nothing printable is left.
    """
    if value is None:
        return None
    text = unicodedata.normalize("NFKC", str(value)).translate(_DOT_LIKE)
    text = _strip_unsafe_chars(text)
    text = " ".join(text.split())
    # Cut before the patterns run: _HOST_RE rescans suffixes of long dotted
    # runs, so an unbounded user agent would cost quadratic time. Defanging
    # only lengthens the text, so nothing past the cut could be shown anyway.
    truncated = len(text) > max_chars
    text = text[:max_chars]
    text = _SCHEME_RE.sub("[:]//", text)
    text = _WWW_RE.sub(lambda m: m.group(0)[:-1] + "[.]", text)
    text = _HOST_RE.sub(lambda m: m.group(0).replace(".", "[.]"), text)
    text = _IPV4_RE.sub(lambda m: m.group(0).replace(".", "[.]"), text)
    if not text:
        return None
    if truncated or len(text) > max_chars:
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
    total: int | None = None,
) -> tuple[str, str]:
    """Build the subject and plain-text body of a security notice.

    Args:
        event: The event type.
        occurrences: What to list (at least one).
        digest: True for the trailing email that lists the occurrences
            buffered after the first one of a window.
        window_minutes: The coalescing window, for the digest wording.
        profile_page_url: ``<FRONTEND_URL>/profile``.
        total: How many occurrences the digest covers when more happened
            than were kept in ``occurrences``; defaults to their number.

    Returns:
        ``(subject, text)``.
    """
    event = SecurityEvent(event)
    subject, description = _EVENT_TEXT[event]
    count = max(total or 0, len(occurrences))
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
            label = "Added:  " if event is SecurityEvent.SIGN_IN_METHOD_ADDED else "Removed:"
            lines.append(f"    {label}     {occurrence.sign_in_method}")
        if occurrence.actor:
            lines.append(f"    Done by:     {occurrence.actor}, an administrator of your")
            lines.append("                 workspace, on your account")
        lines.append("")
    if not listed:
        lines += [
            f"  {count} {'occurrence' if count == 1 else 'occurrences'} of this change "
            "could not be listed: their details",
            "  expired before this email was sent.",
            "",
        ]
    elif count > len(listed):
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

    Deliverable means verified: ``email_verified_at`` is set by the password
    flow (following an emailed link) or by an OAuth sign-in whose IdP attested
    the address as verified (``RoleManager.ensure_user``). A linked provider
    alone is not proof — Google can return an unverified address. Accounts
    created by OAuth since the #1678 back-fill that have not signed in again
    are verified on their next OAuth sign-in. ``@local`` addresses never are.

    Args:
        db: Async session.
        user_id: The account's ``users.user_id``.

    Returns:
        The address, or ``None`` when nothing should be sent.
    """
    user = (await db.execute(select(User).where(User.user_id == user_id))).scalar_one_or_none()
    if user is None or not user.email or "@" not in user.email:
        return None
    if _is_local_address(user.email) or user.email_verified_at is None:
        return None
    return user.email


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
) -> bool:
    """Whether ``user_id`` has never authorized ``client_id`` before.

    Used for authorization-code consent (device-flow approvals notify every
    time). Called before the new grant is written. A user has authorized a
    client before when any of these exists for the pair:

    - an ``oauth_tokens`` row, revoked or not (rows are only deleted with the
      client or the account, so this remembers every past grant);
    - an ``oauth_authorization_codes`` row (consented, not yet exchanged);
    - an ``oauth_device_codes`` row with ``authorized_at`` set (approved,
      not yet exchanged).

    The check is not serialized with the grant write: two consents racing for
    the same new client can both count as first. That costs at most one extra
    notice (the second is coalesced into the window's digest), never a missed
    one.

    Args:
        session: Sync session (the OAuth routes run Authlib on one).
        client_id: The client's ``client_id``.
        user_id: The consenting user.

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
    return (
        session.query(OAuth2DeviceCode.id)
        .filter(
            OAuth2DeviceCode.client_id == client_id,
            OAuth2DeviceCode.user_id == user_id,
            OAuth2DeviceCode.authorized_at.is_not(None),
        )
        .first()
        is None
    )


# ---------------------------------------------------------------------------
# Scheduling and delivery
# ---------------------------------------------------------------------------


def schedule_security_notification(
    background_tasks: BackgroundTasks,
    *,
    user_id: str,
    event: SecurityEvent,
    request: Request | Any = None,
    ip: str | None = None,
    user_agent: str | None = None,
    key_name: str | None = None,
    client_id: str | None = None,
    client_name: str | None = None,
    sign_in_method: str | None = None,
    actor_user_id: str | None = None,
) -> None:
    """Queue the notice for a change that has just committed.

    Call this only after the change's commit. The notice runs after the
    response; nothing here touches the DB, Redis or the email provider, and
    nothing here raises — a failure to queue is logged and the change stands.

    Args:
        background_tasks: The request's ``BackgroundTasks``.
        user_id: The account owner (the recipient).
        event: What happened.
        request: The request that made the change (IP and user agent).
        ip: Client IP, when there is no request to read it from.
        user_agent: Client user agent, when there is no request.
        key_name: API key name, for key events.
        client_id: OAuth client id; its registered name is looked up later.
        client_name: OAuth client name, when the caller already has it.
        sign_in_method: The added or removed sign-in method.
        actor_user_id: The acting user when it is not the owner (an admin
            acting on the owner's credentials).
    """
    try:
        background_tasks.add_task(
            notify_security_event,
            user_id,
            event,
            **_notice_kwargs(
                user_id,
                request=request,
                ip=ip,
                user_agent=user_agent,
                key_name=key_name,
                client_id=client_id,
                client_name=client_name,
                sign_in_method=sign_in_method,
                actor_user_id=actor_user_id,
            ),
        )
    except Exception as exc:
        logger.error(
            "security_notification_schedule_failed",
            user_id=user_id,
            security_event=str(event),
            error_type=type(exc).__name__,
        )


# Strong references to spawned notices: the event loop keeps tasks only weakly.
_spawned: set[asyncio.Task[None]] = set()


def spawn_security_notification(
    *,
    user_id: str,
    event: SecurityEvent,
    ip: str | None = None,
    user_agent: str | None = None,
    key_name: str | None = None,
    client_id: str | None = None,
    client_name: str | None = None,
    sign_in_method: str | None = None,
    actor_user_id: str | None = None,
) -> None:
    """Start the notice for a committed change made outside an HTTP route.

    For callers without ``BackgroundTasks`` (MCP tools). Same arguments and
    the same never-raise contract as :func:`schedule_security_notification`.
    """
    try:
        task = asyncio.get_running_loop().create_task(
            notify_security_event(
                user_id,
                event,
                **_notice_kwargs(
                    user_id,
                    request=None,
                    ip=ip,
                    user_agent=user_agent,
                    key_name=key_name,
                    client_id=client_id,
                    client_name=client_name,
                    sign_in_method=sign_in_method,
                    actor_user_id=actor_user_id,
                ),
            )
        )
        _spawned.add(task)
        task.add_done_callback(_spawned.discard)
    except Exception as exc:
        logger.error(
            "security_notification_schedule_failed",
            user_id=user_id,
            security_event=str(event),
            error_type=type(exc).__name__,
        )


def _notice_kwargs(
    user_id: str,
    *,
    request: Request | Any,
    ip: str | None,
    user_agent: str | None,
    key_name: str | None,
    client_id: str | None,
    client_name: str | None,
    sign_in_method: str | None,
    actor_user_id: str | None,
) -> dict[str, Any]:
    """Keyword arguments of :func:`notify_security_event` for one occurrence."""
    if request is not None:
        client = getattr(request, "client", None)
        headers = getattr(request, "headers", None) or {}
        ip = ip or (getattr(client, "host", None) if client else None)
        user_agent = user_agent or headers.get("user-agent")
    return {
        "ip": ip,
        "user_agent": user_agent,
        "occurred_at": utcnow(),
        "key_name": key_name,
        "client_id": client_id,
        "client_name": client_name,
        "sign_in_method": sign_in_method,
        "actor_user_id": actor_user_id if actor_user_id != user_id else None,
    }


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
        outcome, window_id = await _buffer_if_window_open(user_id, event, occurrence)
        if outcome == _BUFFERED:
            logger.info(
                "security_notification_buffered", user_id=user_id, security_event=event.value
            )
            return
        result = await _deliver(
            recipient, user_id, event, [occurrence], digest=False, email=email_service
        )
        if result == _FAILED and outcome == _OPENED and window_id is not None:
            # The notice that opened the window never went out: close it, so
            # the next occurrence is sent at once and not as a "follow-up".
            # Occurrences buffered while that send was in flight are sent now
            # as a notice of their own (fail open to notifying). A timed-out
            # send may still arrive, so it keeps the window.
            buffered, total = await _close_window(user_id, event, window_id)
            if buffered:
                await _deliver(
                    recipient,
                    user_id,
                    event,
                    buffered,
                    digest=False,
                    email=email_service,
                    total=total,
                )
    except Exception as exc:
        # Type only: a driver error's text could echo the address.
        logger.error(
            "security_notification_failed",
            user_id=user_id,
            security_event=str(event),
            error_type=type(exc).__name__,
        )


def _member(user_id: str, event: SecurityEvent | str, window_id: str) -> str:
    return _MEMBER_SEPARATOR.join((user_id, str(event), window_id))


def _parse_member(member: str) -> tuple[str, str, str]:
    """``(user_id, event, window_id)``; the user id may itself contain ``|``."""
    user_id, event_value, window_id = member.rsplit(_MEMBER_SEPARATOR, 2)
    return user_id, event_value, window_id


def _open_key(user_id: str, event: SecurityEvent | str) -> str:
    return _OPEN_KEY.format(user_id=user_id, event=str(event))


def _key(template: str, user_id: str, event: SecurityEvent | str, window_id: str) -> str:
    return template.format(user_id=user_id, event=str(event), window_id=window_id)


def _window_seconds() -> int:
    return get_settings().security_notification_window_seconds


# Outcomes of _buffer_if_window_open.
_OPENED = "opened"  # send now; this occurrence opened the window
_BUFFERED = "buffered"  # the window's digest will list it
_SEND_NOW = "send_now"  # send now; no window is ours (Redis down, lost race)

# Outcomes of _deliver.
_SENT = "sent"
_FAILED = "failed"  # definite: the provider refused or raised
_UNCERTAIN = "uncertain"  # timed out; the call may still complete in its thread


# Optimistic transactions (WATCH / MULTI / EXEC) retried this many times on a
# conflict before giving up (a record then sends at once; a claim waits for
# the next run).
_TX_RETRIES = 5


async def _buffer_if_window_open(
    user_id: str, event: SecurityEvent, occurrence: SecurityOccurrence
) -> tuple[str, str | None]:
    """Buffer ``occurrence`` in the open window, or open a new one — atomically.

    One optimistic transaction watches only the (user, event)'s open-window
    pointer, whose value is ``"<window id>:<deadline>"``. The occurrence is
    buffered only when the pointer names a window whose deadline has not
    passed; otherwise — no pointer, an unreadable one, or an expired window —
    a new window is opened (pointer and due entry written together). Every
    transition that ends a window (claim, close, expiry) deletes the pointer
    in its own MULTI when it names that window, so this WATCH fires on it. An
    expired window keeps its own due entry and buffer, so its digest still
    goes out on its own.

    Returns:
        ``(outcome, window_id)``: ``_OPENED`` when this occurrence opened the
        window (send it now); ``_BUFFERED`` when the digest will list it;
        ``_SEND_NOW`` (no window id) when it must be sent now without a window
        — Redis is unavailable or the transaction kept conflicting (fail open
        to notifying).
    """
    open_key = _open_key(user_id, event)
    try:
        client = get_redis_client()
        ttl = _buffer_ttl()
        payload = occurrence.to_json()
        for _ in range(_TX_RETRIES):
            try:
                async with client.pipeline(transaction=True) as pipe:
                    # Only this (user, event)'s pointer is watched: other
                    # users' windows never force a retry. Claim / close /
                    # expiry rewrite the pointer inside their own MULTI when it
                    # names their window, which fires this WATCH.
                    await pipe.watch(open_key)
                    parsed = _parse_pointer(await pipe.get(open_key))
                    now = _now_score()
                    pipe.multi()
                    if parsed is not None and parsed[1] > now:
                        window_id = parsed[0]
                        buffer_key = _key(_BUFFER_KEY, user_id, event, window_id)
                        count_key = _key(_COUNT_KEY, user_id, event, window_id)
                        pipe.rpush(buffer_key, payload)
                        # Keep the first occurrences only; the counter keeps the
                        # total, so a burst cannot grow the list without bound.
                        pipe.ltrim(buffer_key, 0, _DIGEST_MAX_OCCURRENCES - 1)
                        pipe.incr(count_key)
                        pipe.expire(buffer_key, ttl)
                        pipe.expire(count_key, ttl)
                        await pipe.execute()
                        return _BUFFERED, window_id
                    new_id = uuid.uuid4().hex
                    deadline = now + _window_seconds()
                    pipe.set(open_key, _pointer_value(new_id, deadline), ex=ttl)
                    pipe.zadd(_DUE_KEY, {_member(user_id, event, new_id): deadline})
                    await pipe.execute()
                    return _OPENED, new_id
            except WatchError:
                continue
        logger.warning(
            "security_notification_record_contended", user_id=user_id, security_event=event.value
        )
        return _SEND_NOW, None
    except Exception as exc:
        logger.warning(
            "security_notification_redis_unavailable",
            user_id=user_id,
            security_event=event.value,
            error_type=type(exc).__name__,
        )
        return _SEND_NOW, None


async def _claim_window(
    client: Any, user_id: str, event_value: str, window_id: str, *, now: float
) -> bool:
    """Claim a due window for its flush — one optimistic transaction.

    Removes the due entry, releases the open-window pointer if it still names
    this window, and moves the buffer and counter under the claim keys
    (renamed, or appended to a claim left by a failed earlier attempt). The
    buffer is watched, so no occurrence can slip in between the read and the
    move.

    Only a window whose due score is still ``<= now`` is claimed: a window
    re-queued with a later retry time since this run listed it is left alone.

    Returns:
        True when this caller claimed it; False when the due entry is gone
        (another process claimed it), not due yet (re-queued meanwhile), or
        the transaction kept conflicting (the entry stays for the next run).
    """
    member = _member(user_id, event_value, window_id)
    open_key = _open_key(user_id, event_value)
    buffer_key = _key(_BUFFER_KEY, user_id, event_value, window_id)
    count_key = _key(_COUNT_KEY, user_id, event_value, window_id)
    claim_buffer = _key(_CLAIM_BUFFER_KEY, user_id, event_value, window_id)
    claim_count = _key(_CLAIM_COUNT_KEY, user_id, event_value, window_id)
    ttl = _buffer_ttl()
    for _ in range(_TX_RETRIES):
        try:
            async with client.pipeline(transaction=True) as pipe:
                await pipe.watch(_DUE_KEY, open_key, buffer_key, count_key, claim_buffer)
                score = await pipe.zscore(_DUE_KEY, member)
                if score is None or score > now:
                    return False
                pointer = _parse_pointer(await pipe.get(open_key))
                items = await pipe.lrange(buffer_key, 0, -1)
                total = max(int(await pipe.get(count_key) or 0), len(items))
                pipe.multi()
                pipe.zrem(_DUE_KEY, member)
                if pointer is not None and pointer[0] == window_id:
                    pipe.delete(open_key)
                if items:
                    pipe.rpush(claim_buffer, *items)
                    pipe.ltrim(claim_buffer, 0, _DIGEST_MAX_OCCURRENCES - 1)
                    pipe.expire(claim_buffer, ttl)
                if total:
                    # Carried even without items (they may have expired), so
                    # the digest can still say how many there were.
                    pipe.incrby(claim_count, total)
                    pipe.expire(claim_count, ttl)
                pipe.delete(buffer_key, count_key)
                await pipe.execute()
                return True
        except WatchError:
            continue
    logger.warning(
        "security_notification_claim_contended", user_id=user_id, security_event=event_value
    )
    return False


async def _close_window(
    user_id: str, event: SecurityEvent, window_id: str
) -> tuple[list[SecurityOccurrence], int]:
    """Close a window whose opening notice failed — one transaction; never raises.

    Removes the due entry, releases the pointer if it still names this
    window, and reads and deletes the buffer and its counter together, so a
    transient failure cannot strand the pointer. The occurrences buffered
    meanwhile are returned so the caller sends them as a notice of their own
    rather than as a follow-up to an email the owner never got.

    Returns:
        The buffered occurrences and their total (``[], 0`` when none or on
        a Redis failure).
    """
    member = _member(user_id, event, window_id)
    open_key = _open_key(user_id, event)
    buffer_key = _key(_BUFFER_KEY, user_id, event, window_id)
    count_key = _key(_COUNT_KEY, user_id, event, window_id)
    try:
        client = get_redis_client()
        for _ in range(_TX_RETRIES):
            try:
                async with client.pipeline(transaction=True) as pipe:
                    await pipe.watch(open_key, buffer_key, count_key)
                    pointer = _parse_pointer(await pipe.get(open_key))
                    raw_items = await pipe.lrange(buffer_key, 0, -1)
                    raw_total = await pipe.get(count_key)
                    pipe.multi()
                    pipe.zrem(_DUE_KEY, member)
                    if pointer is not None and pointer[0] == window_id:
                        pipe.delete(open_key)
                    pipe.delete(buffer_key, count_key)
                    await pipe.execute()
                break
            except WatchError:
                continue
        else:
            raise RuntimeError("close_window_contended")
        logger.info(
            "security_notification_window_closed", user_id=user_id, security_event=event.value
        )
        occurrences: list[SecurityOccurrence] = []
        for raw in raw_items or []:
            try:
                occurrences.append(SecurityOccurrence.from_json(raw))
            except (ValueError, TypeError):
                logger.warning("security_notification_bad_buffer_item", security_event=event.value)
        return occurrences, max(int(raw_total or 0), len(occurrences))
    except Exception as exc:
        logger.warning(
            "security_notification_window_close_failed",
            user_id=user_id,
            security_event=event.value,
            error_type=type(exc).__name__,
        )
        return [], 0


async def purge_user_notification_state(user_id: str) -> int:
    """Delete every security-notice key of ``user_id`` (account erasure).

    Removes the user's due entries, open-window pointers and every per-window
    buffer, counter, claim and attempts key. Keys are matched exactly (the
    window id is the last segment), so another user whose id shares a prefix
    is never touched. Never raises.

    Args:
        user_id: The erased account's ``users.user_id``.

    Returns:
        Number of Redis entries removed (due members plus keys).
    """
    removed = 0
    try:
        client = get_redis_client()
        prefix = f"{user_id}{_MEMBER_SEPARATOR}"
        members: list[str] = []
        async for raw_member, _score in client.zscan_iter(_DUE_KEY):
            member = cast(str, raw_member)
            if not member.startswith(prefix):
                continue
            try:
                if _parse_member(member)[0] == user_id:
                    members.append(member)
            except ValueError:
                continue
        if members:
            removed += int(await client.zrem(_DUE_KEY, *members))

        keys: set[str] = set()
        for event in SecurityEvent:
            keys.add(_open_key(user_id, event))
            for template in (
                _BUFFER_KEY,
                _COUNT_KEY,
                _CLAIM_BUFFER_KEY,
                _CLAIM_COUNT_KEY,
                _ATTEMPTS_KEY,
            ):
                exact_prefix = _key(template, user_id, event, "")
                async for raw_key in client.scan_iter(match=_glob_escape(exact_prefix) + "*"):
                    key = cast(str, raw_key)
                    window_id = key[len(exact_prefix) :]
                    if key.startswith(exact_prefix) and _WINDOW_ID_RE.fullmatch(window_id):
                        keys.add(key)
        if keys:
            removed += int(await client.delete(*keys))
    except Exception as exc:
        logger.warning(
            "security_notification_purge_failed", user_id=user_id, error_type=type(exc).__name__
        )
    return removed


_WINDOW_ID_RE = re.compile(r"[0-9a-f]{32}")


def _glob_escape(text: str) -> str:
    """Escape Redis glob metacharacters so ``text`` matches literally."""
    return re.sub(r"([*?\[\]\\])", r"\\\1", text)


def _buffer_ttl() -> int:
    """TTL of pointers, buffers, counters and claims.

    A window's keys are written up to one window before its deadline, and a
    window counts as stale only ``_STATE_RETENTION_SECONDS`` after that
    deadline. The TTL covers both plus an hour of retry backoff, so a
    pending repeat never expires before the stale sweep reports it.
    """
    return _STATE_RETENTION_SECONDS + _window_seconds() + 60 * 60


def _pointer_value(window_id: str, deadline: float) -> str:
    """The open-window pointer: ``"<window id>:<deadline epoch seconds>"``."""
    return f"{window_id}:{deadline!r}"


def _parse_pointer(raw: object) -> tuple[str, float] | None:
    """``(window id, deadline)`` from a pointer, or None when absent or unreadable."""
    if not isinstance(raw, str):
        return None
    window_id, _, deadline = raw.partition(":")
    if not _WINDOW_ID_RE.fullmatch(window_id):
        return None
    try:
        return window_id, float(deadline)
    except ValueError:
        return None


def _now_score() -> float:
    """Current time as epoch seconds (the sorted set's score unit).

    ``utcnow()`` is naive UTC and ``timestamp()`` on a naive value assumes
    local time, so the zone is attached first.
    """
    return utcnow().replace(tzinfo=UTC).timestamp()


def _monotonic() -> float:
    """Clock for the flush time budget (patched in tests)."""
    return time.monotonic()


async def _deliver(
    recipient: str,
    user_id: str,
    event: SecurityEvent,
    occurrences: list[SecurityOccurrence],
    *,
    digest: bool,
    email: EmailService | None,
    total: int | None = None,
) -> str:
    """Send one notice; log (never raise) on failure.

    Returns:
        ``_SENT``; ``_FAILED`` when the provider returned False or raised;
        ``_UNCERTAIN`` when the send timed out — ``wait_for`` cancels the
        coroutine but not a provider call running in a thread, so the email
        may still go out and must not be retried.
    """
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
                total=total if total is not None else len(occurrences),
            ),
            timeout=_EMAIL_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        logger.warning(
            "security_notification_send_uncertain",
            user_id=user_id,
            security_event=event.value,
            digest=digest,
        )
        return _UNCERTAIN
    except Exception as exc:
        logger.error(
            "security_notification_send_failed",
            user_id=user_id,
            security_event=event.value,
            error_type=type(exc).__name__,
        )
        return _FAILED
    if not sent:
        logger.error(
            "security_notification_send_failed",
            user_id=user_id,
            security_event=event.value,
            error_type="send_returned_false",
        )
        return _FAILED
    logger.info(
        "security_notification_sent",
        user_id=user_id,
        security_event=event.value,
        occurrences=len(occurrences),
        digest=digest,
    )
    return _SENT


async def flush_due_security_notifications(
    *,
    now_score: float | None = None,
    session_factory: Callable[[], AsyncSession] | None = None,
    email_service: EmailService | None = None,
) -> int:
    """Close every due window and send its digest (the every-minute job).

    Each window is claimed in one optimistic transaction (:func:`_claim_window`:
    the due entry removed, the pointer released, the buffer moved to the claim
    keys); only the caller whose transaction removed the entry goes on, so
    concurrent runs in several processes send each digest once. The claim keys
    are deleted once the digest is done (best effort: a Redis error then only
    leaves them to expire). A definite
    failure — the send, the recipient lookup, or Redis after the claim —
    re-queues the window ``_DIGEST_RETRY_DELAY_SECONDS`` × attempts later;
    after ``_DIGEST_MAX_ATTEMPTS`` of them the digest is logged and dropped.
    A timed-out send counts as sent. A window with nothing buffered sends
    nothing. A run claims at most ``_FLUSH_BATCH`` windows and stops claiming
    after ``_FLUSH_TIME_BUDGET_SECONDS``. Never raises.

    Args:
        now_score: Current epoch seconds (tests pin it).
        session_factory: Override for tests; defaults to the app's factory.
        email_service: Override for tests; defaults to the configured one.

    Returns:
        Number of digests sent.
    """
    now = now_score if now_score is not None else _now_score()
    try:
        client = get_redis_client()
        await _expire_stale_windows(client, now=now)
        due = await client.zrangebyscore(_DUE_KEY, "-inf", now, start=0, num=_FLUSH_BATCH)
    except Exception as exc:
        logger.warning("security_notification_flush_unavailable", error_type=type(exc).__name__)
        return 0

    started = _monotonic()
    sent = 0
    for index, raw_member in enumerate(due):
        if _monotonic() - started > _FLUSH_TIME_BUDGET_SECONDS:
            logger.warning(
                "security_notification_flush_budget_exhausted", deferred=len(due) - index
            )
            break
        member = cast(str, raw_member)
        try:
            user_id, event_value, window_id = _parse_member(member)
        except ValueError:
            await client.zrem(_DUE_KEY, member)
            continue
        try:
            if not await _claim_window(client, user_id, event_value, window_id, now=now):
                continue  # claimed by another process (or contended: next run)
        except Exception as exc:
            logger.error(
                "security_notification_flush_failed",
                user_id=user_id,
                security_event=event_value,
                error_type=type(exc).__name__,
            )
            continue
        try:
            if await _flush_window(
                client,
                user_id,
                event_value,
                window_id,
                now=now,
                session_factory=session_factory,
                email_service=email_service,
            ):
                sent += 1
        except Exception as exc:
            # Claimed but not finished (Redis failed mid-flush): without a due
            # entry nothing would ever retry it, so re-queue it.
            logger.error(
                "security_notification_flush_failed",
                user_id=user_id,
                security_event=event_value,
                error_type=type(exc).__name__,
            )
            try:
                await _retry_window(client, user_id, event_value, window_id, now=now)
            except Exception as retry_exc:
                logger.error(
                    "security_notification_requeue_failed",
                    user_id=user_id,
                    security_event=event_value,
                    error_type=type(retry_exc).__name__,
                )
    return sent


async def _expire_stale_windows(client: Any, *, now: float) -> None:
    """Drop due entries older than ``_STATE_RETENTION_SECONDS``, loudly.

    Their buffers have expired by then; the entry would otherwise sit in the
    due set forever. Each goes with its pointer (when it still names it) and
    its keys; a warning records the loss.

    The due set is watched too and the entry's score re-read inside the
    transaction: a replica that claimed the window after the range read (its
    claim removes the entry and holds the buffer in the claim keys) or
    re-queued it must keep those keys. A conflict leaves the entry to the
    next run.
    """
    cutoff = now - _STATE_RETENTION_SECONDS
    stale = await client.zrangebyscore(_DUE_KEY, "-inf", f"({cutoff}", start=0, num=_FLUSH_BATCH)
    for raw_member in stale:
        member = cast(str, raw_member)
        try:
            user_id, event_value, window_id = _parse_member(member)
        except ValueError:
            await client.zrem(_DUE_KEY, member)
            continue
        open_key = _open_key(user_id, event_value)
        try:
            async with client.pipeline(transaction=True) as pipe:
                await pipe.watch(open_key, _DUE_KEY)
                pointer = _parse_pointer(await pipe.get(open_key))
                score = await pipe.zscore(_DUE_KEY, member)
                if score is None or float(score) >= cutoff:
                    await pipe.unwatch()
                    continue  # claimed or re-queued since the range read
                pipe.multi()
                pipe.zrem(_DUE_KEY, member)
                if pointer is not None and pointer[0] == window_id:
                    pipe.delete(open_key)
                pipe.delete(
                    *(
                        _key(template, user_id, event_value, window_id)
                        for template in (
                            _BUFFER_KEY,
                            _COUNT_KEY,
                            _CLAIM_BUFFER_KEY,
                            _CLAIM_COUNT_KEY,
                            _ATTEMPTS_KEY,
                        )
                    )
                )
                await pipe.execute()
        except WatchError:
            continue  # the next run tries again
        logger.warning(
            "security_notification_window_expired",
            user_id=user_id,
            security_event=event_value,
        )


async def _flush_window(
    client: Any,
    user_id: str,
    event_value: str,
    window_id: str,
    *,
    now: float,
    session_factory: Callable[[], AsyncSession] | None,
    email_service: EmailService | None,
) -> bool:
    """Send the digest of one claimed window; True when it was sent."""
    claim_buffer = _key(_CLAIM_BUFFER_KEY, user_id, event_value, window_id)
    claim_count = _key(_CLAIM_COUNT_KEY, user_id, event_value, window_id)
    attempts_key = _key(_ATTEMPTS_KEY, user_id, event_value, window_id)

    raw_items = await client.lrange(claim_buffer, 0, -1)
    raw_total = await client.get(claim_count)
    if not raw_items and not int(raw_total or 0):
        await client.delete(claim_buffer, claim_count, attempts_key)
        return False

    occurrences: list[SecurityOccurrence] = []
    for raw in raw_items:
        try:
            occurrences.append(SecurityOccurrence.from_json(raw))
        except (ValueError, TypeError):
            logger.warning("security_notification_bad_buffer_item", security_event=event_value)
    # Occurrences whose details are gone (expired buffer, unreadable items)
    # are still reported by number in the digest, never dropped silently.
    total = max(int(raw_total or 0), len(raw_items))

    event = SecurityEvent(event_value)
    try:
        factory = session_factory or _get_session_factory()
        async with factory() as db:
            recipient = await resolve_deliverable_address(db, user_id)
    except Exception as exc:
        logger.error(
            "security_notification_flush_failed",
            user_id=user_id,
            security_event=event_value,
            error_type=type(exc).__name__,
        )
        await _retry_window(client, user_id, event_value, window_id, now=now, total=total)
        return False
    if recipient is None:
        logger.info("security_notification_skipped", user_id=user_id, security_event=event_value)
        await client.delete(claim_buffer, claim_count, attempts_key)
        return False

    result = await _deliver(
        recipient, user_id, event, occurrences, digest=True, email=email_service, total=total
    )
    if result == _FAILED:
        await _retry_window(client, user_id, event_value, window_id, now=now, total=total)
        return False
    # Sent, or uncertain (a timed-out send may still arrive): never resend.
    # The cleanup is best effort — a Redis error here must not re-queue a
    # digest that went out; the claim keys then expire with their TTL.
    try:
        await client.delete(claim_buffer, claim_count, attempts_key)
    except Exception as exc:
        logger.warning(
            "security_notification_cleanup_failed",
            user_id=user_id,
            security_event=event_value,
            error_type=type(exc).__name__,
        )
    return result == _SENT


async def _retry_window(
    client: Any,
    user_id: str,
    event_value: str,
    window_id: str,
    *,
    now: float,
    total: int | None = None,
) -> None:
    """Re-queue a claimed window after a definite failure, or drop it at the bound.

    The claim (and any buffer) stays under the window's keys; the next flush
    of the re-added due entry picks them up.
    """
    claim_buffer = _key(_CLAIM_BUFFER_KEY, user_id, event_value, window_id)
    claim_count = _key(_CLAIM_COUNT_KEY, user_id, event_value, window_id)
    attempts_key = _key(_ATTEMPTS_KEY, user_id, event_value, window_id)
    ttl = _buffer_ttl()
    attempts = int(await client.incr(attempts_key))
    await client.expire(attempts_key, ttl)
    if attempts >= _DIGEST_MAX_ATTEMPTS:
        logger.error(
            "security_notification_digest_dropped",
            user_id=user_id,
            security_event=event_value,
            attempts=attempts,
            occurrences=total,
        )
        await client.delete(
            claim_buffer,
            claim_count,
            attempts_key,
            _key(_BUFFER_KEY, user_id, event_value, window_id),
            _key(_COUNT_KEY, user_id, event_value, window_id),
        )
        return
    await client.expire(claim_buffer, ttl)
    await client.expire(claim_count, ttl)
    await client.zadd(
        _DUE_KEY,
        {_member(user_id, event_value, window_id): now + _DIGEST_RETRY_DELAY_SECONDS * attempts},
        nx=True,
    )
    logger.warning(
        "security_notification_digest_requeued",
        user_id=user_id,
        security_event=event_value,
        attempts=attempts,
    )
