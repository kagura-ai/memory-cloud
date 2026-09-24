"""Re-accepting the terms of service (Issue #1665).

When the deployment's ``TERMS_VERSION`` changes, a signed-in user whose latest
accepted version differs sees ``terms_acceptance_required: true`` on
``GET /auth/me`` and the web UI blocks on an "accept the updated terms" step.
That step posts here.

- ``404`` while ``TERMS_VERSION`` is empty: the feature does not exist then, the
  same answer the other default-off surfaces give.
- ``409`` when ``version`` is not the current one — typically a page loaded
  before the version changed. The detail says so; the client reloads.
- ``200`` otherwise. Idempotent: accepting the version already on the user's
  newest row writes nothing and still answers ``200``.

Session auth only (``SessionUser``), like the rest of ``/me``: an API key or an
OAuth bearer token cannot accept terms on a person's behalf. The body is JSON,
so a cross-site form post cannot reach it, and the session cookie is
``SameSite=Lax``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import SessionUser
from db.base import get_db
from services.terms_service import TermsService, current_terms_version

router = APIRouter(prefix="/me", tags=["me-terms"])


class TermsAcceptanceRequest(BaseModel):
    """The version the user just agreed to — must be the current one."""

    version: str = Field(..., min_length=1, max_length=64)


class TermsAcceptanceResponse(BaseModel):
    """``recorded`` is false when this version was already accepted."""

    version: str
    recorded: bool
    terms_acceptance_required: bool


@router.post("/terms-acceptance", response_model=TermsAcceptanceResponse)
async def accept_terms(
    payload: TermsAcceptanceRequest,
    request: Request,
    user: SessionUser,
    db: AsyncSession = Depends(get_db),
) -> TermsAcceptanceResponse:
    """Record that the signed-in user accepts the current terms version.

    Raises:
        HTTPException(404): ``TERMS_VERSION`` is not set.
        HTTPException(409): ``version`` is not the current terms version.
    """
    current = current_terms_version()
    if current is None:
        raise HTTPException(status_code=404, detail="Terms acceptance is not enabled")
    if payload.version != current:
        raise HTTPException(
            status_code=409,
            detail=(
                "The terms have changed since this page was loaded. "
                "Reload to see the current version."
            ),
        )

    result = await TermsService(db).record(
        user_id=user["user_id"],
        user_email=user.get("email") or "",
        version=current,
        source="reaccept",
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    return TermsAcceptanceResponse(
        version=result.version, recorded=result.recorded, terms_acceptance_required=False
    )
