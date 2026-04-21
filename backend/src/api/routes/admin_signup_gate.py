"""Admin endpoints for the signup gate (Issue #358 Phase 1).

Routes under ``/api/v1/admin/signup-gate`` — system-admin only via
``AdminUser``. Exposes the singleton config (GET/PUT) and allowlist CRUD
(GET/POST/DELETE). Phase 2 modes are deliberately rejected at the API
boundary so a DB edit is the only way to reach them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import AdminUser
from db.base import get_db
from services.signup_gate_service import SignupGateService
from utils.github_user import GitHubUserNotFound
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/admin/signup-gate", tags=["admin-signup-gate"])


# ============================================================================
# Schemas
# ============================================================================


class SignupGateConfigResponse(BaseModel):
    """Config read-model.

    ``github_sponsors_grace_period_days`` is exposed for Phase 2 UX affordance
    even though Phase 1 doesn't act on it; showing the value keeps the
    admin UI honest about what will happen once Sponsors mode activates.
    """

    enabled: bool
    mode: Literal["manual", "github_sponsors", "both"]
    github_sponsors_grace_period_days: int

    model_config = ConfigDict(from_attributes=True)


class SignupGateConfigUpdate(BaseModel):
    enabled: bool
    mode: Literal["manual", "github_sponsors", "both"]


class AllowlistEntryResponse(BaseModel):
    id: UUID
    github_user_id: str
    github_username: str
    source: str
    state: str
    added_by_user_id: str | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AllowlistAddRequest(BaseModel):
    # GitHub usernames are 1–39 chars, alphanumeric with hyphens, never at
    # start/end. Enforce length + shape here so malformed payloads don't spend
    # a GitHub API request (rate-limited to 60/hr unauthenticated).
    github_username: str = Field(
        min_length=1,
        max_length=39,
        pattern=r"^[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,37}[a-zA-Z0-9])?$",
    )


# ============================================================================
# Config
# ============================================================================


@router.get("/config", response_model=SignupGateConfigResponse)
async def get_config(
    user: AdminUser,
    db: AsyncSession = Depends(get_db),
) -> SignupGateConfigResponse:
    svc = SignupGateService(db)
    config = await svc.get_config()
    return SignupGateConfigResponse.model_validate(config)


@router.put("/config", response_model=SignupGateConfigResponse)
async def update_config(
    payload: SignupGateConfigUpdate,
    user: AdminUser,
    db: AsyncSession = Depends(get_db),
) -> SignupGateConfigResponse:
    # Phase 1 refuses to persist Sponsors modes — the service would raise
    # NotImplementedError on check_access anyway, so reject early with a 400
    # that explains why.
    if payload.mode in ("github_sponsors", "both"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Mode '{payload.mode}' is reserved for Phase 2 "
                "(GitHub Sponsors integration — Issue #358 follow-up). "
                "Only 'manual' is accepted in Phase 1."
            ),
        )
    svc = SignupGateService(db)
    config = await svc.update_config(enabled=payload.enabled, mode=payload.mode)
    return SignupGateConfigResponse.model_validate(config)


# ============================================================================
# Allowlist
# ============================================================================


@router.get("/allowlist", response_model=list[AllowlistEntryResponse])
async def list_allowlist(
    user: AdminUser,
    db: AsyncSession = Depends(get_db),
) -> list[AllowlistEntryResponse]:
    svc = SignupGateService(db)
    entries = await svc.list_allowlist()
    return [AllowlistEntryResponse.model_validate(e) for e in entries]


@router.post(
    "/allowlist",
    response_model=AllowlistEntryResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_to_allowlist(
    payload: AllowlistAddRequest,
    user: AdminUser,
    db: AsyncSession = Depends(get_db),
) -> AllowlistEntryResponse:
    svc = SignupGateService(db)
    try:
        entry = await svc.add_to_allowlist(
            github_username=payload.github_username,
            added_by_user_id=user["user_id"],
        )
    except GitHubUserNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"GitHub user '{payload.github_username}' not found",
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        # GitHub API unreachable / rate-limited. 60 req/hr unauth means a busy
        # admin session can exhaust the quota; surface that as 502 with an
        # actionable message rather than leaking the raw httpx exception.
        logger.warning(
            "github_api_unavailable",
            extra={"github_username": payload.github_username, "error": str(exc)},
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                "Could not reach GitHub to resolve the username. "
                "The API may be rate-limited or temporarily unreachable; try again shortly."
            ),
        ) from exc
    return AllowlistEntryResponse.model_validate(entry)


@router.delete(
    "/allowlist/{entry_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def remove_from_allowlist(
    entry_id: UUID,
    user: AdminUser,
    db: AsyncSession = Depends(get_db),
) -> None:
    svc = SignupGateService(db)
    try:
        await svc.remove_from_allowlist(entry_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Allowlist entry not found"
        ) from exc
