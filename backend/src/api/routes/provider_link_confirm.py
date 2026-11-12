"""Confirm a provider link by email (Issue #2786).

Public: the emailed token and the browser nonce cookie set at
``/auth/{provider}/login`` are the credentials (``auth.oauth_browser``).
``ENABLE_PROVIDER_LINK_CONFIRMATION=false`` 404s every route (router-level
dependency, before body validation).

- ``POST /auth/link-confirm/preview`` — which provider account a live link
  connects, and whether this browser started the sign-in
  (``same_browser``). Consumes nothing (mail scanners may open the page).
- ``POST /auth/link-confirm/complete`` — connect the provider and sign this
  browser in; answers ``{"redirect_url": ...}`` with a session cookie. 403
  ``AUTH-310`` in another browser (the link stays usable in the right one),
  400 ``AUTH-301`` unknown / expired / used link or an account that cannot
  take the link any more, 503 when the link was made but the session could
  not be opened (sign in again).
- ``POST /auth/link-confirm/cancel`` — "This wasn't me": discards the link
  and stops further offers for the account. Always 204.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

import auth.oauth_browser as oauth_browser
from api.routes import auth as auth_module
from config.settings import get_settings
from db.base import get_db
from db.redis import get_redis_client, increment_counter
from models.api_base import TZAwareBaseModel
from services.known_device_service import note_browser_sign_in
from services.provider_link_confirmation_service import (
    PROVIDER_LABELS,
    ProviderLinkConfirmationService,
)
from utils.exceptions import RateLimitError, RedisError
from utils.logger import get_logger

logger = get_logger(__name__)

# preview + complete + cancel per client address (fixed window).
_WINDOW_SECONDS = 15 * 60
_ATTEMPTS_PER_IP = 20


def _require_enabled() -> None:
    """404 the whole surface when the confirmation is off for this deployment."""
    if not get_settings().enable_provider_link_confirmation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")


router = APIRouter(
    prefix="/auth/link-confirm",
    tags=["authentication"],
    dependencies=[Depends(_require_enabled)],
)


class ProviderLinkTokenBody(BaseModel):
    """The token from the emailed ``/link/confirm?token=`` link."""

    token: str = Field(..., min_length=1, max_length=128)


class ProviderLinkPreviewResponse(TZAwareBaseModel):
    """The provider account a live link connects.

    ``provider_login`` is the GitHub login or the Google address; show it as
    the primary identifier. ``provider_name`` is free text chosen at the
    provider (secondary). ``provider_avatar_url`` is only ever an https URL on
    the provider's avatar host. ``same_browser`` is true only in the browser
    that started the sign-in: only there can the link be completed.
    """

    provider: Literal["google", "github"]
    provider_login: str
    provider_name: str | None
    provider_avatar_url: str | None
    expires_at: datetime
    same_browser: bool


class ProviderLinkCompleteResponse(BaseModel):
    """Where the browser goes next (the session cookie is set)."""

    redirect_url: str


async def _attempt(request: Request) -> None:
    """Count one attempt for the client address; 429 over the limit (fails open)."""
    ip = auth_module._login_client_ip(request)
    try:
        count = await increment_counter(f"link_confirm_ip:{ip}", ttl=_WINDOW_SECONDS)
    except RedisError as exc:
        logger.warning(
            "provider_link_confirm_rate_limit_unavailable", error_type=type(exc).__name__
        )
        return
    if count > _ATTEMPTS_PER_IP:
        raise RateLimitError("Too many attempts. Please try again later.")


def _browser_nonce(request: Request) -> str | None:
    return oauth_browser.presented_nonce(request.cookies)


@router.post("/preview", response_model=ProviderLinkPreviewResponse)
async def preview_provider_link(
    body: ProviderLinkTokenBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> ProviderLinkPreviewResponse:
    """The provider account of a live link. 400 when the link is not live."""
    await _attempt(request)
    preview = await ProviderLinkConfirmationService(db).preview(
        raw_token=body.token, browser_nonce=_browser_nonce(request)
    )
    return ProviderLinkPreviewResponse(
        provider=preview.provider,
        provider_login=preview.provider_login,
        provider_name=preview.provider_name,
        provider_avatar_url=preview.provider_avatar_url,
        expires_at=preview.expires_at,
        same_browser=preview.same_browser,
    )


@router.post("/complete", response_model=ProviderLinkCompleteResponse)
async def complete_provider_link(
    body: ProviderLinkTokenBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Connect the provider account to the email account and sign this browser in."""
    await _attempt(request)
    ip = auth_module._login_client_ip(request)
    nonce = _browser_nonce(request)
    confirmed = await ProviderLinkConfirmationService(db).complete(
        raw_token=body.token,
        browser_nonce=nonce,
        ip_address=ip,
        user_agent=request.headers.get("user-agent"),
    )
    # The link is committed and the owner is being told. Open the session the
    # way a password sign-in does: write it, then sign the account out
    # everywhere else under the per-account sweep lock. It proves nothing
    # for identity linking (no fresh proof of the account was given).
    unavailable = HTTPException(
        status_code=503,
        detail=(
            f"{PROVIDER_LABELS[confirmed.provider]} was connected to your account. Sign in again."
        ),
    )
    session_manager = auth_module._session_manager
    if session_manager is None:
        raise unavailable
    try:
        session_id = session_manager.create_session(
            {
                "sub": confirmed.user_id,
                "user_id": confirmed.user_id,
                "email": confirmed.email,
                "name": confirmed.name,
                "picture": confirmed.picture,
                "role": confirmed.role.value,
            },
            proven_at=None,
        )
        completed = await auth_module._complete_password_sign_in(
            confirmed.user_id, confirmed.email, session_id
        )
    except Exception as exc:
        # SweepLeaseLostError / TimeoutError from the sweep lock, or Redis.
        logger.error(
            "provider_link_confirm_session_failed",
            user_id=confirmed.user_id,
            error_type=type(exc).__name__,
        )
        raise unavailable from None
    if not completed:
        logger.warning("provider_link_confirm_session_swept", user_id=confirmed.user_id)
        raise unavailable

    response = Response(
        content=ProviderLinkCompleteResponse(
            redirect_url=auth_module._safe_redirect_url(confirmed.return_to)
        ).model_dump_json(),
        media_type="application/json",
    )
    auth_module._set_session_cookie(response, session_id)
    # The nonce has done its job: the cookie and its server-side marker go.
    attrs = oauth_browser.cookie_attrs()
    response.delete_cookie(
        oauth_browser.cookie_name(),
        path="/",
        secure=bool(attrs["secure"]),
        httponly=True,
        samesite="lax",
    )
    if nonce is not None:
        try:
            await get_redis_client().delete(
                oauth_browser.marker_key(oauth_browser.nonce_digest(nonce))
            )
        except Exception as exc:
            logger.warning(
                "provider_link_confirm_marker_delete_failed", error_type=type(exc).__name__
            )
    await note_browser_sign_in(
        request,
        response,
        user_id=confirmed.user_id,
        sign_in_method=PROVIDER_LABELS[confirmed.provider],
    )
    return response


@router.post("/cancel", status_code=204)
async def cancel_provider_link(
    body: ProviderLinkTokenBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """ "This wasn't me": discard a pending link and stop further offers. Always 204."""
    await _attempt(request)
    await ProviderLinkConfirmationService(db).cancel(
        raw_token=body.token,
        ip_address=auth_module._login_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    return Response(status_code=204)
