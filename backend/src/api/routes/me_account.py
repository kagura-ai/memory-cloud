"""Self-service account erasure endpoints (Issue #360) and multi-provider
account linking (Issue #517).

Routes that let an authenticated user request, confirm, cancel, and
inspect their own GDPR-Art.17 / APPI account-deletion flow. Admin force-
erase lives in `admin.py` and goes through the same service.

The module also hosts the account-linking sub-API introduced in #517:
``link-provider`` initiates an OAuth round-trip to bind a new IdP identity,
``unlink-provider`` removes an existing linked provider, and ``providers``
lists all providers currently linked to the session user.

Auth model: every endpoint uses `SessionUser` (browser session only, no
API keys) — a leaked API key must never be enough to trigger account
self-deletion. This mirrors the discipline already used by `/users/me`
and the billing checkout endpoints.
"""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from api.routes import auth as auth_module
from api.routes.me_oauth import (
    INTENT_KEY,
    STATE_TTL,
    USER_KEY,
    _build_authorization_url,
)
from auth.dependencies import SessionUser
from db.base import get_db
from models.api_base import TZAwareBaseModel
from services.account_erasure_service import AccountErasureService
from services.account_linking_service import AccountLinkingService
from utils.datetime import to_utc_iso
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/me/account", tags=["account-erasure"])


# ---------------------------------------------------------------------------
# Schemas — kept inline (small + endpoint-specific). Mirrors the lightweight
# schema discipline used by routes/users.py.
# ---------------------------------------------------------------------------


class ErasureRequestCreateResponse(TZAwareBaseModel):
    """Returned after creating a self-service erasure request.

    The confirmation token is delivered through one of two channels
    depending on the user's auth method (Issue #469):

    - **Password-auth users**: ``confirm_token`` is populated in this
      response. The user re-enters their password alongside this token at
      ``POST /me/account/erasure-confirm`` (the password is the second
      factor — the response token is the first).
    - **OAuth users**: ``confirm_token`` is ``None`` here. The token is
      delivered via email to the user's account address as a one-time
      confirm link. Email is the canonical second factor for OAuth, just
      as the password re-prompt is for password-auth users — keeping the
      raw token out of the response body removes a redundant copy that
      would otherwise widen the disclosure surface (proxy access logs,
      browser devtools, frontend error-reporters) once email actually
      delivers it.

    The raw token is stored in Redis under ``erasure_token:{token}`` with
    a 1-hour TTL and is single-use regardless of delivery channel. The
    Redis key maps token → ``request_id`` only — it is NOT session-bound.
    Confirmation additionally requires the authenticated session user to
    match the erasure request's ``user_id`` (enforced by
    ``confirm_self_service``), so a leaked token alone is insufficient
    without the matching session cookie.

    The frontend SHOULD treat this token as sensitive and not log it.
    """

    request_id: UUID
    status: str
    requested_at: datetime
    confirm_token: str | None = Field(
        default=None,
        description=(
            "One-time confirmation token, valid for 1 hour. **Populated only "
            "for password-auth users** — they re-enter their password "
            "alongside this token at POST /me/account/erasure-confirm. **For "
            "OAuth users this is null** and the token is delivered via email."
        ),
    )


class ErasureConfirmRequest(BaseModel):
    """Payload for POST /me/account/erasure-confirm."""

    token: str
    password: str | None = Field(
        default=None,
        description="Required only for password-auth users. OAuth users omit.",
    )


class ErasureRequestStateResponse(TZAwareBaseModel):
    """Read-only view of an erasure request's lifecycle state."""

    request_id: UUID
    status: str
    is_self_service: bool
    requested_at: datetime
    confirmed_at: datetime | None = None
    scheduled_for: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    cancelled_at: datetime | None = None
    failure_reason: str | None = None


def _state_response(request) -> ErasureRequestStateResponse:
    """Project an ErasureRequest ORM row to the public response shape.

    The deleted_data_summary, ip_address, and user_agent fields are
    intentionally NOT exposed to the user — they are admin-only audit
    artifacts.
    """
    return ErasureRequestStateResponse(
        request_id=request.id,
        status=request.status,
        is_self_service=request.is_self_service,
        requested_at=request.requested_at,
        confirmed_at=request.confirmed_at,
        scheduled_for=request.scheduled_for,
        started_at=request.started_at,
        completed_at=request.completed_at,
        cancelled_at=request.cancelled_at,
        failure_reason=request.failure_reason,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/erasure-request",
    response_model=ErasureRequestCreateResponse,
    status_code=201,
)
async def create_erasure_request(
    request: Request,
    user: SessionUser,
    db: AsyncSession = Depends(get_db),
) -> ErasureRequestCreateResponse:
    """Create a pending erasure request and issue a one-time token.

    Returns 201 with the new request's state. The ``confirm_token`` field
    in the response is populated for password-auth users and ``null`` for
    OAuth users (Issue #469): OAuth users receive the token via email
    instead, keeping the raw secret out of the response surface.

    Other status codes:
        - 403: user is the protected initial admin
        - 409: an active erasure request already exists for this user
        - 503: OAuth user but the confirmation email failed to dispatch
          (mapped from EmailDispatchError); the pending row is rolled back
          so the user can retry once the email backend recovers.

    The receipt notification (separate from the OAuth confirmation email)
    is dispatched post-commit and is fire-and-forget — it tells the user
    the request was received and is meant to be obvious to the human even
    if they don't click any confirm link.
    """
    service = AccountErasureService(db)
    record, response_token = await service.request_self_service_erasure(
        user_id=user["user_id"],
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    return ErasureRequestCreateResponse(
        request_id=record.id,
        status=record.status,
        requested_at=record.requested_at,
        confirm_token=response_token,
    )


@router.post("/erasure-confirm", response_model=ErasureRequestStateResponse)
async def confirm_erasure_request(
    body: ErasureConfirmRequest,
    user: SessionUser,
    db: AsyncSession = Depends(get_db),
) -> ErasureRequestStateResponse:
    """Confirm a pending request and start the 7-day cooling-off period.

    Password-auth users must re-supply their password as a second factor.
    OAuth users rely on the email-link click + active session cookie.
    Returns 400 on invalid/expired token, 403 on password mismatch.
    """
    service = AccountErasureService(db)
    record = await service.confirm_self_service(
        user_id=user["user_id"],
        token=body.token,
        password=body.password,
    )
    return _state_response(record)


@router.delete("/erasure-request", response_model=ErasureRequestStateResponse)
async def cancel_erasure_request(
    user: SessionUser,
    db: AsyncSession = Depends(get_db),
) -> ErasureRequestStateResponse:
    """Cancel a cooling_off request before it executes.

    Returns 404 if there is no active request to cancel.
    """
    service = AccountErasureService(db)
    record = await service.cancel_self_service(user_id=user["user_id"])
    return _state_response(record)


@router.get("/erasure-request", response_model=ErasureRequestStateResponse | None)
async def get_active_erasure_request(
    user: SessionUser,
    db: AsyncSession = Depends(get_db),
) -> ErasureRequestStateResponse | None:
    """Return the user's active erasure request, or null if none.

    "Active" = pending OR cooling_off OR in_progress. Terminal states
    (complete / failed / cancelled) are not surfaced here — that's an
    admin-side audit concern, not user-facing UX.
    """
    service = AccountErasureService(db)
    record = await service.get_active_request_for_user(user["user_id"])
    if record is None:
        return None
    return _state_response(record)


# ---------------------------------------------------------------------------
# Multi-provider OAuth account linking (Issue #517)
# ---------------------------------------------------------------------------


class LinkProviderRequest(BaseModel):
    """Body for POST /me/account/link-provider."""

    provider: Literal["google", "github"]


class LinkProviderResponse(BaseModel):
    """Frontend redirects ``window.location`` to ``authorization_url``.

    Mirrors ``me_oauth.RefreshOAuthResponse``: the OAuth round-trip itself
    is the fresh re-auth, so no password is ever prompted (edge case 1 — the
    locked re-auth contract for OAuth-only users)."""

    authorization_url: str
    state: str


class UnlinkProviderRequest(BaseModel):
    """Body for POST /me/account/unlink-provider."""

    provider: Literal["google", "github"]


class UnlinkProviderResponse(BaseModel):
    """Returned by POST /me/account/unlink-provider on success."""

    status: str


class LinkedProvider(BaseModel):
    """One linked OAuth identity, as surfaced to the profile UI.

    ``linked_at`` / ``last_used_at`` are pre-serialized to ISO 8601 strings
    with an explicit ``Z`` suffix via ``to_utc_iso`` (the source columns are
    naive UTC ``TIMESTAMP WITHOUT TIME ZONE``), so JS clients don't reparse
    them as local time."""

    provider: str
    linked_at: str | None = None
    last_used_at: str | None = None


class ProvidersListResponse(BaseModel):
    """All OAuth providers currently linked to the session user."""

    providers: list[LinkedProvider]


@router.post("/link-provider", response_model=LinkProviderResponse, tags=["account-linking"])
async def link_provider(
    body: LinkProviderRequest,
    user: SessionUser,
) -> LinkProviderResponse:
    """Initiate a link-mode OAuth round-trip for the current user.

    The OAuth round-trip *is* the fresh re-auth — there is no password
    prompt, which is the locked re-auth contract for OAuth-only users
    (edge case 1). The existing ``/auth/{provider}/callback`` reads
    ``oauth2_state_intent:{state}`` == ``"link"`` and binds the returned
    identity to ``oauth2_state_user:{state}`` via ``AccountLinkingService``.

    Returns:
        JSON with ``authorization_url`` (frontend redirects to it) and the
        CSRF ``state`` token.

    Raises:
        HTTPException(500): auth managers not initialised, OAuth2 manager
            missing, or a required env var (GOOGLE_REDIRECT_URI /
            GITHUB_CLIENT_ID) is missing.
    """
    if not auth_module._session_manager:
        raise HTTPException(status_code=500, detail="Auth managers not initialized")

    user_id = user["user_id"]
    provider = body.provider

    # Resolve config + compose the URL BEFORE writing any Redis state so a
    # missing env var 500s without orphaning four state keys for 5 minutes
    # (shares me_oauth._build_authorization_url with refresh_oauth).
    state = secrets.token_urlsafe(32)
    authorization_url = _build_authorization_url(provider, state)

    redis = auth_module._session_manager._redis
    redis.setex(f"oauth2_state:{state}", STATE_TTL, "pending")
    redis.setex(INTENT_KEY.format(state=state), STATE_TTL, "link")
    # Pin the originating user so the callback binds the returned identity
    # to THIS account (and rejects state replayed under a different session).
    redis.setex(USER_KEY.format(state=state), STATE_TTL, user_id)
    redis.setex(f"oauth2_return_to:{state}", STATE_TTL, "/profile?linked=1")

    logger.info("link_provider_initiated", user_id=user_id, provider=provider)

    return LinkProviderResponse(authorization_url=authorization_url, state=state)


@router.post("/unlink-provider", response_model=UnlinkProviderResponse, tags=["account-linking"])
async def unlink_provider(
    body: UnlinkProviderRequest,
    request: Request,
    user: SessionUser,
    db: AsyncSession = Depends(get_db),
) -> UnlinkProviderResponse:
    """Remove a linked OAuth provider from the current account.

    Returns ``{"status": "ok"}`` on success. The service guards the
    invariants and raises, which the global ``memory_cloud_exception_handler``
    maps to HTTP:

        - 404 (NotFoundException): the provider is not linked to this account.
        - 409 (ConflictError): removing it would leave zero sign-in methods.
    """
    service = AccountLinkingService(db)
    await service.unlink(
        user_id=user["user_id"],
        provider=body.provider,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    return UnlinkProviderResponse(status="ok")


@router.get("/providers", response_model=ProvidersListResponse, tags=["account-linking"])
async def list_providers(
    user: SessionUser,
    db: AsyncSession = Depends(get_db),
) -> ProvidersListResponse:
    """List the OAuth providers currently linked to the session user."""
    service = AccountLinkingService(db)
    rows = await service.list_providers(user["user_id"])
    return ProvidersListResponse(
        providers=[
            LinkedProvider(
                provider=row.provider,
                linked_at=to_utc_iso(row.linked_at),
                last_used_at=to_utc_iso(row.last_used_at),
            )
            for row in rows
        ]
    )
