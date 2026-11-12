"""Announcement-email consent: the settings toggle and the unsubscribe link (Issue #2761).

Two routers in one module:

``me_router`` (``/me/announcement-emails``) — session auth only (``SessionUser``),
like the rest of ``/me``: an API key or an OAuth bearer token cannot opt a
person in. The body is JSON, so a cross-site form post cannot reach the PUT,
and the session cookie is ``SameSite=Lax``.

- ``GET`` — the current answer and whether the deployment offers the emails.
- ``PUT`` — record a new answer (source ``settings``). Turning it ON needs
  ``ANNOUNCEMENT_EMAILS_ENABLED`` and the current wording version (409
  otherwise, the page reloads); turning it OFF always works. An unchanged
  answer writes nothing. Limited per user (429).

``public_router`` (``/announcement-emails``) — no authentication: the token is
the only authority, and a session cookie that rides along is ignored. Every
outcome (malformed, forged, unknown or erased account, already off, done)
answers the same ``200 {"status": "accepted"}``, so the answer never tells
whether an account exists. The token is checked first (CPU only); nothing
touches the database or Redis before that.

- ``POST /unsubscribe/{token}`` — the RFC 8058 one-click target named in
  ``List-Unsubscribe``. Never 429 (mailbox providers post from a few shared
  addresses; a refused unsubscribe is lost). A valid token whose write fails
  answers 503 so the provider retries; only the token's holder can see that.
- ``GET /unsubscribe/{token}`` — some clients open that URL in a browser: 303
  to the web page with the token in the URL fragment. Never changes anything
  (link scanners fetch GETs).
- ``POST /unsubscribe`` with ``{"token": ...}`` — what the web page posts, so the
  token is not in a request path that carries the session cookie. Invalid
  tokens are counted per client address and refused with 429 past the limit.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, StrictBool
from sqlalchemy.ext.asyncio import AsyncSession

from api.routes import auth as auth_module
from auth.announcement_unsubscribe import (
    UNSUBSCRIBE_PAGE_PATH,
    UnsubscribeClaim,
    read_unsubscribe_token,
)
from auth.dependencies import SessionUser
from config.settings import get_settings
from db.base import get_db
from db.redis import increment_counter
from models.api_base import TZAwareBaseModel
from services.announcement_consent_service import (
    apply_unsubscribe,
    current_announcement_consent,
    current_consent_wording_version,
    record_announcement_consent,
)
from utils.exceptions import RateLimitError, RedisError
from utils.logger import get_logger

logger = get_logger(__name__)

me_router = APIRouter(prefix="/me/announcement-emails", tags=["announcement-emails"])
public_router = APIRouter(prefix="/announcement-emails", tags=["announcement-emails"])

# Settings toggle: bounds the append-only history a script could grow.
_PREF_WINDOW_SECONDS = 60 * 60
_PREF_CHANGES_PER_USER = 20
# Unsubscribe page: invalid tokens per client address.
_UNSUB_WINDOW_SECONDS = 15 * 60
_UNSUB_INVALID_PER_IP = 30

# What may go into the redirect's fragment as is (gate1 R3).
_FRAGMENT_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{1,2048}")

_NOT_OFFERED = "Announcement emails are not offered on this deployment."
_STALE_WORDING = (
    "The announcement email wording has changed since this page was loaded. "
    "Reload to see the current wording."
)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class AnnouncementEmailsResponse(TZAwareBaseModel):
    """The signed-in user's announcement-email setting.

    Attributes:
        offered: Whether the deployment offers announcement emails
            (``ANNOUNCEMENT_EMAILS_ENABLED``). Turning the setting off works
            either way.
        announcement_emails: The current answer (false when never answered).
        answered: Whether the user ever answered.
        source: Where the current answer came from (``signup``, ``settings``,
            ``unsubscribe_link``), null when never answered.
        recorded_at: When the current answer was recorded.
        wording_version: The consent copy version to send back when turning it
            on; null while the deployment does not offer the emails.
    """

    offered: bool
    announcement_emails: bool
    answered: bool
    source: str | None = None
    recorded_at: datetime | None = None
    wording_version: str | None = None


class AnnouncementEmailsUpdateResponse(AnnouncementEmailsResponse):
    """``recorded`` is false when the answer was already the current one."""

    recorded: bool


class AnnouncementEmailsUpdateRequest(BaseModel):
    """A new answer. ``wording_version`` is required to turn it on."""

    announcement_emails: StrictBool
    wording_version: str | None = Field(None, max_length=64)


class UnsubscribeRequest(BaseModel):
    """The token from the unsubscribe page. An oversized or malformed value is
    accepted and ignored like any other invalid token."""

    token: str


class UnsubscribeAcceptedResponse(BaseModel):
    """Identical for every unsubscribe request."""

    status: Literal["accepted"] = "accepted"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _over_limit(key: str, limit: int, window: int) -> bool:
    """Count one request against ``key``; True when over ``limit``. Fails open."""
    try:
        count = await increment_counter(key, ttl=window)
    except RedisError as exc:
        logger.warning("announcement_emails_rate_limit_unavailable", error_type=type(exc).__name__)
        return False
    return count > limit


async def _state(db: AsyncSession, user_id: str) -> AnnouncementEmailsResponse:
    version = current_consent_wording_version()
    current = await current_announcement_consent(db, user_id)
    return AnnouncementEmailsResponse(
        offered=version is not None,
        announcement_emails=bool(current and current.value),
        answered=current is not None,
        source=current.source if current else None,
        recorded_at=current.recorded_at if current else None,
        wording_version=version,
    )


async def _unsubscribe(db: AsyncSession, claim: UnsubscribeClaim) -> None:
    """Apply a valid token. 503 when the write fails (the holder may retry)."""
    try:
        changed = await apply_unsubscribe(db, claim)
        await db.commit()
    except Exception as exc:
        await db.rollback()
        logger.warning("announcement_unsubscribe_failed", error_type=type(exc).__name__)
        raise HTTPException(
            status_code=503,
            detail="The request could not be completed. Please try again later.",
            headers={"Retry-After": "60"},
        ) from None
    if changed:
        logger.info("announcement_emails_unsubscribed", user_id=claim.user_id)


# ---------------------------------------------------------------------------
# Settings (session only)
# ---------------------------------------------------------------------------


@me_router.get("", response_model=AnnouncementEmailsResponse)
async def get_announcement_emails(
    user: SessionUser,
    db: AsyncSession = Depends(get_db),
) -> AnnouncementEmailsResponse:
    """The signed-in user's announcement-email setting."""
    return await _state(db, user["user_id"])


@me_router.put("", response_model=AnnouncementEmailsUpdateResponse)
async def update_announcement_emails(
    payload: AnnouncementEmailsUpdateRequest,
    user: SessionUser,
    db: AsyncSession = Depends(get_db),
) -> AnnouncementEmailsUpdateResponse:
    """Turn announcement emails on or off for the signed-in user.

    Raises:
        HTTPException(409): Turning it on while the deployment does not offer
            them, or with a wording version other than the current one.
        RateLimitError(429): Too many changes in an hour.
        NotFoundException(404): The account is gone.
    """
    user_id = user["user_id"]
    value = payload.announcement_emails
    version = current_consent_wording_version()
    if value:
        if version is None:
            raise HTTPException(status_code=409, detail=_NOT_OFFERED)
        if payload.wording_version != version:
            raise HTTPException(status_code=409, detail=_STALE_WORDING)

    if await _over_limit(
        f"announce_pref_user:{user_id}", _PREF_CHANGES_PER_USER, _PREF_WINDOW_SECONDS
    ):
        raise RateLimitError(
            "Too many changes to this setting. Please try again later.",
            retry_after=_PREF_WINDOW_SECONDS,
        )

    result = await record_announcement_consent(
        db,
        user_id=user_id,
        value=value,
        source="settings",
        wording_version=version if value else None,
    )
    await db.commit()
    if result.recorded:
        logger.info("announcement_emails_setting_changed", user_id=user_id, value=value)
    state = result.state
    return AnnouncementEmailsUpdateResponse(
        offered=version is not None,
        announcement_emails=state.value,
        answered=True,
        source=state.source,
        recorded_at=state.recorded_at,
        wording_version=version,
        recorded=result.recorded,
    )


# ---------------------------------------------------------------------------
# Unsubscribe link (no authentication; the token is the authority)
# ---------------------------------------------------------------------------


@public_router.post("/unsubscribe/{token}", response_model=UnsubscribeAcceptedResponse)
async def one_click_unsubscribe(
    token: str,
    db: AsyncSession = Depends(get_db),
) -> UnsubscribeAcceptedResponse:
    """RFC 8058 one-click unsubscribe. The same 200 for every token.

    The request body (``List-Unsubscribe=One-Click``) is not read. Never 429.
    """
    claim = read_unsubscribe_token(token)
    if claim is not None:
        await _unsubscribe(db, claim)
    return UnsubscribeAcceptedResponse()


@public_router.get("/unsubscribe/{token}", status_code=303, response_class=RedirectResponse)
async def open_unsubscribe_link(token: str) -> RedirectResponse:
    """Send a browser to the unsubscribe page; changes nothing.

    The token goes into the URL fragment (never sent to a server) only when it
    has the token alphabet; anything else lands on the page without one.
    """
    target = get_settings().frontend_url.rstrip("/") + UNSUBSCRIBE_PAGE_PATH
    if _FRAGMENT_TOKEN_RE.fullmatch(token):
        target = f"{target}#{token}"
    return RedirectResponse(
        target,
        status_code=303,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@public_router.post("/unsubscribe", response_model=UnsubscribeAcceptedResponse)
async def unsubscribe_from_page(
    payload: UnsubscribeRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> UnsubscribeAcceptedResponse:
    """What the unsubscribe page posts. The same 200 for every token.

    Raises:
        RateLimitError(429): Too many invalid tokens from this client address.
            A valid token is never refused.
    """
    claim = read_unsubscribe_token(payload.token)
    if claim is None:
        ip = auth_module._login_client_ip(request)
        if await _over_limit(
            f"announce_unsub_bad_ip:{ip}", _UNSUB_INVALID_PER_IP, _UNSUB_WINDOW_SECONDS
        ):
            raise RateLimitError(
                "Too many requests. Please try again later.",
                retry_after=_UNSUB_WINDOW_SECONDS,
            )
        return UnsubscribeAcceptedResponse()
    await _unsubscribe(db, claim)
    return UnsubscribeAcceptedResponse()
