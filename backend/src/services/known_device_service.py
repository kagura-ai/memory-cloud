"""New-device sign-in alerts (Issue #1769).

A browser sign-in (password, password + MFA, Google, GitHub) from a browser
the account has not signed in from before emails the owner through the
security-notice pipeline of #1752 (``NEW_DEVICE_SIGN_IN``).

The browser is identified by a long-lived device cookie, minted here on the
first sign-in and re-issued on every later one so its lifetime slides. Only
the keyed HMAC of the cookie value is stored (``user_known_devices``), so the
table identifies nothing without the server key. The user agent is not part
of the key — it is spoofable, so a match on it must not silence an alert —
and the IP address is neither a key nor stored: both appear in the notice
email only.

Rules:

- no known device yet (the account's first browser sign-in) → register the
  browser, send nothing; this also covers the browser the account was
  created from;
- known browser → refresh ``last_seen``, send nothing;
- unknown browser while others are known → register it and alert. The
  browser is registered before the notice is attempted, so a delivery
  failure does not alert again on the next sign-in from it;
- the alert is mandatory, like every #1752 notice;
- a password reset deletes the account's known devices (the next sign-in
  from every browser, the attacker's included, is a new device) and account
  erasure deletes them with the account;
- rows not seen for ``known_device_retention_days`` are deleted daily, and
  at most ``known_device_max_per_user`` are kept.

CLI / MCP sign-ins (device flow, token endpoint) carry no cookie and are out
of scope.
"""

from __future__ import annotations

import os
import re
import secrets
from datetime import datetime
from enum import StrEnum
from typing import Any, cast

from fastapi import Request, Response
from sqlalchemy import Delete, delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import get_settings
from db.base import get_db
from models.auth import UserKnownDevice
from services.security_notification_service import (
    SecurityEvent,
    spawn_security_notification,
)
from utils.datetime import utcnow
from utils.hashing import hmac_sha256_hex
from utils.logger import get_logger

logger = get_logger(__name__)

DEVICE_COOKIE_NAME = "kagura_device"
# One year: a browser that does not sign in for that long re-registers (and
# alerts) — the retention job will have forgotten it well before then anyway.
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
    """(Re-)issue the device cookie with the same attributes as the session cookie."""
    is_production = os.getenv("ENVIRONMENT", "development") == "production"
    response.set_cookie(
        key=DEVICE_COOKIE_NAME,
        value=value,
        path="/",
        httponly=True,
        secure=is_production,
        samesite="lax",
        max_age=DEVICE_COOKIE_MAX_AGE,
    )


def device_hash(cookie_value: str) -> str:
    """Keyed hash of a device cookie value — the only form that is stored."""
    return hmac_sha256_hex(_HASH_PREFIX + cookie_value, get_settings().audit_hmac_key)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


async def record_sign_in(db: AsyncSession, *, user_id: str, digest: str, now: datetime) -> SignIn:
    """Register the browser for the account, or refresh it, and say which.

    Caller owns commit/rollback.

    Args:
        db: Async session.
        user_id: The account that signed in.
        digest: :func:`device_hash` of the browser's device cookie.
        now: Naive UTC time of the sign-in.

    Returns:
        ``KNOWN`` when the browser was already registered (or another request
        registered it concurrently), ``FIRST_DEVICE`` when the account had no
        known device yet, ``NEW_DEVICE`` otherwise.
    """
    known = (
        await db.execute(
            select(UserKnownDevice).where(
                UserKnownDevice.user_id == user_id, UserKnownDevice.device_hash == digest
            )
        )
    ).scalar_one_or_none()
    if known is not None:
        known.last_seen = now
        return SignIn.KNOWN

    count = (
        await db.execute(
            select(func.count())
            .select_from(UserKnownDevice)
            .where(UserKnownDevice.user_id == user_id)
        )
    ).scalar_one()

    inserted = await db.execute(
        pg_insert(UserKnownDevice)
        .values(user_id=user_id, device_hash=digest, first_seen=now, last_seen=now)
        .on_conflict_do_nothing(constraint="user_known_devices_user_device_key")
    )
    if not cast(CursorResult[Any], inserted).rowcount:
        # Lost the race with a concurrent sign-in from the same browser.
        return SignIn.KNOWN

    cap = get_settings().known_device_max_per_user
    if count >= cap:
        # Keep the ``cap`` most recently seen rows (the new one included).
        keep = (
            select(UserKnownDevice.id)
            .where(UserKnownDevice.user_id == user_id)
            .order_by(UserKnownDevice.last_seen.desc(), UserKnownDevice.id)
            .limit(cap)
        )
        await db.execute(
            delete(UserKnownDevice).where(
                UserKnownDevice.user_id == user_id, UserKnownDevice.id.not_in(keep)
            )
        )

    return SignIn.FIRST_DEVICE if count == 0 else SignIn.NEW_DEVICE


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
            client = getattr(request, "client", None)
            headers = getattr(request, "headers", None) or {}
            spawn_security_notification(
                user_id=user_id,
                event=SecurityEvent.NEW_DEVICE_SIGN_IN,
                ip=getattr(client, "host", None) if client else None,
                user_agent=headers.get("user-agent"),
                sign_in_method=sign_in_method,
            )
        logger.info("sign_in_device_recorded", user_id=user_id, outcome=outcome.value)
    except Exception as exc:
        logger.error("sign_in_device_record_failed", user_id=user_id, error_type=type(exc).__name__)
