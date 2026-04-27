"""Self-service account erasure endpoints (Issue #360).

Routes that let an authenticated user request, confirm, cancel, and
inspect their own GDPR-Art.17 / APPI account-deletion flow. Admin force-
erase lives in `admin.py` and goes through the same service.

Auth model: every endpoint uses `SessionUser` (browser session only, no
API keys) — a leaked API key must never be enough to trigger account
self-deletion. This mirrors the discipline already used by `/users/me`
and the billing checkout endpoints.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import SessionUser
from db.base import get_db
from services.account_erasure_service import AccountErasureService
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/me/account", tags=["account-erasure"])


# ---------------------------------------------------------------------------
# Schemas — kept inline (small + endpoint-specific). Mirrors the lightweight
# schema discipline used by routes/users.py.
# ---------------------------------------------------------------------------


class ErasureRequestCreateResponse(BaseModel):
    """Returned after creating a self-service erasure request.

    The raw confirmation token is returned ONCE in this response. The
    frontend is responsible for the confirm UX — password-auth users
    re-enter their password alongside this token; OAuth users present
    the token via whatever flow the frontend wires (e.g. a confirm
    button on the same screen). The token is also bound to the active
    session via ``erasure_token:{token}`` in Redis (TTL 1h) and is
    single-use.

    The frontend SHOULD treat this token as sensitive and not log it.
    Issue #463 #4 tracks gating the token-in-response on auth method
    once a real email provider replaces the LoggingEmailService stub.
    """

    request_id: UUID
    status: str
    requested_at: datetime
    confirm_token: str = Field(
        description=(
            "One-time confirmation token. Use POST /me/account/erasure-confirm "
            "within 1 hour. Password-auth users must additionally re-enter "
            "their password."
        )
    )


class ErasureConfirmRequest(BaseModel):
    """Payload for POST /me/account/erasure-confirm."""

    token: str
    password: str | None = Field(
        default=None,
        description="Required only for password-auth users. OAuth users omit.",
    )


class ErasureRequestStateResponse(BaseModel):
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

    The receipt notification is dispatched (currently to logs, manually
    forwarded by ops per the runbook). Returns 409 if an active request
    already exists for the user, 403 if the user is the protected
    initial admin.
    """
    service = AccountErasureService(db)
    record, token = await service.request_self_service_erasure(
        user_id=user["user_id"],
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    return ErasureRequestCreateResponse(
        request_id=record.id,
        status=record.status,
        requested_at=record.requested_at,
        confirm_token=token,
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
