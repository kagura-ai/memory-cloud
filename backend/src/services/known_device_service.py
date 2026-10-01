"""New-device sign-in alerts (Issue #1769).

A browser sign-in (password, password + MFA, Google, GitHub) from a browser
the account has not signed in from before emails the owner through the
security-notice pipeline of #1752 (``NEW_DEVICE_SIGN_IN``).

The browser is identified by a long-lived device cookie, minted here on the
first sign-in and re-issued on every later one so its lifetime slides. Only
the keyed HMAC of the cookie value is stored (``user_known_devices``), so the
table identifies nothing without the server key. The user agent is not part
of the key — it is spoofable, so a match on it must not silence an alert —
and the IP address is neither a key nor stored here: both go into the notice
email, and sit in the notice pipeline's Redis buffer only while a notice is
coalesced or retried (see ``security_notification_service``).

Rules:

- the account has never had a known device (``users.known_devices_since`` is
  NULL — every account at upgrade time, and new accounts) → register the
  browser, set the marker, send nothing; this also covers the browser the
  account was created from;
- known browser → refresh ``last_seen``, send nothing;
- unknown browser once the marker is set → register it and alert. The
  browser is registered before the notice is attempted, so a delivery
  failure does not alert again on the next sign-in from it;
- the alert is mandatory, like every #1752 notice;
- a password reset deletes the account's known devices but keeps the marker,
  so the next sign-in from every browser — the attacker's included, even
  when it comes first — is a new device; account erasure deletes the rows
  with the account;
- rows not seen for ``known_device_retention_days`` are deleted daily (a
  browser away that long is reported again), and at most
  ``known_device_max_per_user`` are kept.

CLI / MCP sign-ins (device flow, token endpoint) carry no cookie and are out
of scope.
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime
from enum import StrEnum
from typing import Any

from fastapi import Request, Response
from sqlalchemy import Boolean, Delete, delete, literal_column, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from auth.session import browser_cookie_attrs
from config.settings import get_settings
from db.base import get_db
from models.auth import User, UserKnownDevice
from utils.datetime import utcnow
from utils.hashing import hmac_sha256_hex
from utils.logger import get_logger

logger = get_logger(__name__)

DEVICE_COOKIE_NAME = "kagura_device"
# One year, the ceiling of ``known_device_retention_days``: a browser's row is
# forgotten no later than its cookie expires, so a known row never meets a
# cookie-less request from the same browser.
DEVICE_COOKIE_MAX_AGE = 365 * 24 * 3600
# ``secrets.token_urlsafe(32)`` is 43 chars; accept a generous range so the
# format can grow, but reject anything that is not a URL-safe token.
_COOKIE_VALUE_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
# Domain separation: the audit HMAC key also hashes emails and queries.
_HASH_PREFIX = "known-device:"


class SignIn(StrEnum):
    """What :func:`record_sign_in` found for the browser."""

    KNOWN = "known"
    FIRST_DEVICE = "first_device"
    NEW_DEVICE = "new_device"


# ---------------------------------------------------------------------------
# Cookie
# ---------------------------------------------------------------------------


def new_device_cookie_value() -> str:
    """Mint a device cookie value (256 bits, URL-safe)."""
    return secrets.token_urlsafe(32)


def read_device_cookie(request: Request | Any) -> str | None:
    """The request's device cookie value, or None when absent or malformed."""
    cookies = getattr(request, "cookies", None) or {}
    value = cookies.get(DEVICE_COOKIE_NAME)
    if not isinstance(value, str) or not _COOKIE_VALUE_RE.match(value):
        return None
    return value


def set_device_cookie(response: Response, value: str) -> None:
    """(Re-)issue the device cookie with the session cookie's attributes."""
    response.set_cookie(
        key=DEVICE_COOKIE_NAME,
        value=value,
        max_age=DEVICE_COOKIE_MAX_AGE,
        **browser_cookie_attrs(),
    )


def device_hash(cookie_value: str) -> str:
    """Keyed hash of a device cookie value — the only form that is stored."""
    return hmac_sha256_hex(_HASH_PREFIX + cookie_value, get_settings().audit_hmac_key)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


async def record_sign_in(db: AsyncSession, *, user_id: str, digest: str, now: datetime) -> SignIn:
    """Register the browser for the account, or refresh it, and say which.

    Locks the ``users`` row ``FOR UPDATE`` so two sign-ins of one account
    serialize here (two unknown browsers cannot both pass as the first), then
    upserts the device row: one statement, so a concurrent retention DELETE
    of a stale row cannot strand an ORM UPDATE. Caller owns commit/rollback.

    Args:
        db: Async session.
        user_id: The account that signed in.
        digest: :func:`device_hash` of the browser's device cookie.
        now: Naive UTC time of the sign-in.

    Returns:
        ``KNOWN`` when the browser was already registered, ``FIRST_DEVICE``
        when the account had never had a known device, ``NEW_DEVICE``
        otherwise.
    """
    user = (
        await db.execute(select(User).where(User.user_id == user_id).with_for_update())
    ).scalar_one()
    first = user.known_devices_since is None
    if first:
        user.known_devices_since = now

    # ``xmax = 0`` is true for a row this statement inserted, false for one it
    # updated (PostgreSQL sets xmax on the old version of an updated row).
    upsert = (
        pg_insert(UserKnownDevice)
        .values(user_id=user_id, device_hash=digest, first_seen=now, last_seen=now)
        .on_conflict_do_update(
            constraint="user_known_devices_user_device_key", set_={"last_seen": now}
        )
        .returning(UserKnownDevice.id, literal_column("(xmax = 0)", Boolean))
    )
    inserted_id, inserted = (await db.execute(upsert)).one()
    if not inserted:
        return SignIn.KNOWN
    if first:
        return SignIn.FIRST_DEVICE

    # Keep the new row plus the ``cap - 1`` most recently seen others, so the
    # account never holds more than ``cap`` rows even when the new row's
    # ``now`` is older than a concurrent sign-in's (it waited on the lock).
    cap = get_settings().known_device_max_per_user
    keep = (
        select(UserKnownDevice.id)
        .where(UserKnownDevice.user_id == user_id, UserKnownDevice.id != inserted_id)
        .order_by(UserKnownDevice.last_seen.desc(), UserKnownDevice.id)
        .limit(cap - 1)
    )
    await db.execute(
        delete(UserKnownDevice).where(
            UserKnownDevice.user_id == user_id,
            UserKnownDevice.id != inserted_id,
            UserKnownDevice.id.not_in(keep),
        )
    )
    return SignIn.NEW_DEVICE


def known_devices_delete(user_id: str) -> Delete:
    """DELETE of every known device of an account (password reset, erasure)."""
    return delete(UserKnownDevice).where(UserKnownDevice.user_id == user_id)


def stale_devices_delete(cutoff: datetime) -> Delete:
    """DELETE of the rows not seen since ``cutoff`` (the retention job)."""
    return delete(UserKnownDevice).where(UserKnownDevice.last_seen < cutoff)


# ---------------------------------------------------------------------------
# At a sign-in route
# ---------------------------------------------------------------------------


async def note_browser_sign_in(
    request: Request | Any,
    response: Response,
    *,
    user_id: str,
    sign_in_method: str,
) -> None:
    """Record the browser that just signed in and alert the owner if it is new.

    Call it after the session cookie is set, on every browser sign-in path.
    Never raises: a failure here is logged and the sign-in stands. The device
    cookie is (re-)issued even then, so the next sign-in can recognize the
    browser.

    Args:
        request: The sign-in request (device cookie, IP, user agent).
        response: The response that carries the session cookie.
        user_id: The account that signed in.
        sign_in_method: ``Password`` / ``Google`` / ``GitHub``, for the email.
    """
    # Imported here: the notice module imports the password service, which
    # imports this module for ``known_devices_delete``.
    from services import security_notification_service as notices

    cookie = read_device_cookie(request) or new_device_cookie_value()
    set_device_cookie(response, cookie)
    try:
        outcome = SignIn.KNOWN
        async for db in get_db():
            try:
                outcome = await record_sign_in(
                    db, user_id=user_id, digest=device_hash(cookie), now=utcnow()
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
            break
        if outcome == SignIn.NEW_DEVICE:
            notices.spawn_security_notification(
                user_id=user_id,
                event=notices.SecurityEvent.NEW_DEVICE_SIGN_IN,
                request=request,
                sign_in_method=sign_in_method,
            )
        logger.info("sign_in_device_recorded", user_id=user_id, outcome=outcome.value)
    except Exception as exc:
        logger.error("sign_in_device_record_failed", user_id=user_id, error_type=type(exc).__name__)
