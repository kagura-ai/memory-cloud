"""Self-service account erasure endpoints (Issue #360) and multi-provider
account linking (Issue #517).

Routes that let an authenticated user request, confirm, cancel, and
inspect their own GDPR-Art.17 / APPI account-deletion flow. Admin force-
erase lives in `admin.py` and goes through the same service.

The module also hosts the account-linking sub-API introduced in #517:
``link-provider`` initiates an OAuth round-trip to bind a new IdP identity,
``unlink-provider`` removes an existing linked provider, and ``providers``
lists all providers currently linked to the session user.

Identity links (#1784) live here too: ``identity-links`` lists the accounts
counted as the same owner as the session user and the ones that can be
linked, and links or unlinks one. A link is proved by the browser session
holding both accounts, never by an email match.

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

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from api.routes import auth as auth_module
from api.routes.me_oauth import (
    INTENT_KEY,
    RETURN_TO_KEY,
    STATE_TTL,
    USER_KEY,
    _build_authorization_url,
)
from auth.dependencies import SessionUser
from db.base import get_db
from models.api_base import TZAwareBaseModel
from services.account_erasure_service import AccountErasureService
from services.account_linking_service import AccountLinkingService
from services.identity_link_service import IdentityLinkService
from services.security_notification_service import (
    PROVIDER_SIGN_IN_LABELS,
    SecurityEvent,
    schedule_security_notification,
)
from utils.datetime import to_utc_iso
from utils.exceptions import NotFoundException
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
    depending on whether the account has a password (Issue #469; since #1678
    an OAuth account that added a password counts as a password user):

    - **Users with a password**: ``confirm_token`` is populated in this
      response. The user re-enters their password alongside this token at
      ``POST /me/account/erasure-confirm`` (the password is the second
      factor — the response token is the first).
    - **Users without a password**: ``confirm_token`` is ``None`` here. The token is
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
            "for users with a password** — they re-enter their password "
            "alongside this token at POST /me/account/erasure-confirm. **For "
            "users without a password this is null** and the token is "
            "delivered via email."
        ),
    )


class ErasureConfirmRequest(BaseModel):
    """Payload for POST /me/account/erasure-confirm."""

    token: str
    password: str | None = Field(
        default=None,
        description="Required only for users with a password. Others omit.",
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
    redis.setex(RETURN_TO_KEY.format(state=state), STATE_TTL, "/profile?linked=1")

    logger.info("link_provider_initiated", user_id=user_id, provider=provider)

    return LinkProviderResponse(authorization_url=authorization_url, state=state)


@router.post("/unlink-provider", response_model=UnlinkProviderResponse, tags=["account-linking"])
async def unlink_provider(
    body: UnlinkProviderRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    user: SessionUser,
    db: AsyncSession = Depends(get_db),
) -> UnlinkProviderResponse:
    """Remove a linked OAuth provider from the current account.

    Returns ``{"status": "ok"}`` on success. The service guards the
    invariants and raises, which the global ``memory_cloud_exception_handler``
    maps to HTTP:

        - 404 (NotFoundException): the provider is not linked to this account.
        - 409 (ConflictError): removing it would leave zero sign-in methods.

    On success the owner is emailed a security notice (Issue #1752).
    """
    service = AccountLinkingService(db)
    await service.unlink(
        user_id=user["user_id"],
        provider=body.provider,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    schedule_security_notification(
        background_tasks,
        user_id=user["user_id"],
        event=SecurityEvent.SIGN_IN_METHOD_REMOVED,
        request=request,
        sign_in_method=PROVIDER_SIGN_IN_LABELS.get(body.provider, body.provider),
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


# ---------------------------------------------------------------------------
# Identity links (Issue #1784)
# ---------------------------------------------------------------------------


class IdentityLinkTarget(BaseModel):
    """Body for linking or unlinking an account."""

    user_id: str = Field(min_length=1, max_length=255)


class LinkedIdentityItem(BaseModel):
    """An account counted as the same owner as the session user.

    ``linked_at`` is an ISO 8601 string with an explicit ``Z`` (``to_utc_iso``).
    """

    user_id: str
    email: str | None = None
    name: str | None = None
    linked_at: str | None = None


class LinkableIdentityItem(BaseModel):
    """An account signed in on this browser session that is not linked yet."""

    user_id: str
    email: str | None = None
    name: str | None = None


class IdentityLinksResponse(BaseModel):
    """The session user's link set, and what this session could add to it."""

    linked: list[LinkedIdentityItem]
    linkable: list[LinkableIdentityItem]


class IdentityLinkStatusResponse(BaseModel):
    """Returned by the link and unlink endpoints on success."""

    status: str


def _session_id(request: Request) -> str:
    """The caller's session id, or 401 — ``SessionUser`` has already
    established a session, so a missing cookie is a contradiction."""
    session_id = request.cookies.get(auth_module.SESSION_COOKIE_NAME)
    if not auth_module._session_manager or not session_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return session_id


@router.get("/identity-links", response_model=IdentityLinksResponse, tags=["account-linking"])
async def list_identity_links(
    request: Request,
    user: SessionUser,
    db: AsyncSession = Depends(get_db),
) -> IdentityLinksResponse:
    """List the accounts linked to the session user, and the linkable ones.

    Linked accounts own the same private contexts and the memories in them
    (roles and workspace membership stay per account). Linkable accounts are
    the other accounts signed in on this browser session.
    """
    user_id = user["user_id"]
    linked = await IdentityLinkService(db).list_linked(user_id)
    linked_ids = {item.user_id for item in linked}
    session_id = _session_id(request)
    accounts = auth_module._session_manager.list_accounts(session_id)
    linkable = []
    for account in accounts:
        account_id = account.get("user_id") or account.get("sub")
        if account_id and account_id != user_id and account_id not in linked_ids:
            linkable.append(
                LinkableIdentityItem(
                    user_id=account_id, email=account.get("email"), name=account.get("name")
                )
            )
    return IdentityLinksResponse(
        linked=[
            LinkedIdentityItem(
                user_id=item.user_id,
                email=item.email,
                name=item.name,
                linked_at=to_utc_iso(item.linked_at),
            )
            for item in linked
        ],
        linkable=linkable,
    )


@router.post("/identity-links", response_model=IdentityLinkStatusResponse, tags=["account-linking"])
async def link_identity(
    body: IdentityLinkTarget,
    request: Request,
    background_tasks: BackgroundTasks,
    user: SessionUser,
    db: AsyncSession = Depends(get_db),
) -> IdentityLinkStatusResponse:
    """Count another account as the same owner as the session user.

    The proof is this browser session: the target must be one of the accounts
    signed in on it, which each entered through its own sign-in. Nothing is
    ever linked by an email match (#481). An id that is not in the session
    answers 404 whether or not such an account exists.

    Raises (via the global handler): 400 for the caller's own id, 404 for an
    account not signed in here, 409 when the set would exceed its size cap.
    """
    user_id = user["user_id"]
    session_id = _session_id(request)
    if body.user_id != user_id and not auth_module._session_manager.session_holds_user(
        session_id, body.user_id
    ):
        raise NotFoundException("Account")
    created = await IdentityLinkService(db).link(
        user_id,
        body.user_id,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    # A repeat of an existing link changed nothing: no second notice.
    for account in (user_id, body.user_id) if created else ():
        schedule_security_notification(
            background_tasks,
            user_id=account,
            event=SecurityEvent.ACCOUNT_LINKED,
            request=request,
        )
    return IdentityLinkStatusResponse(status="ok")


@router.post(
    "/identity-links/unlink", response_model=IdentityLinkStatusResponse, tags=["account-linking"]
)
async def unlink_identity(
    body: IdentityLinkTarget,
    request: Request,
    background_tasks: BackgroundTasks,
    user: SessionUser,
    db: AsyncSession = Depends(get_db),
) -> IdentityLinkStatusResponse:
    """Stop counting an account as the same owner as the session user.

    Either side can cut the link from its own session; the other account
    does not have to be signed in. 404 when the account is not linked.
    """
    await IdentityLinkService(db).unlink(
        user["user_id"],
        body.user_id,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    for account in (user["user_id"], body.user_id):
        schedule_security_notification(
            background_tasks,
            user_id=account,
            event=SecurityEvent.ACCOUNT_UNLINKED,
            request=request,
        )
    return IdentityLinkStatusResponse(status="ok")
