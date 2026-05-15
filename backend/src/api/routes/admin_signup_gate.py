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
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import AdminUser
from db.base import get_db
from models.api_base import TZAwareBaseModel
from services.signup_gate_service import SignupGateService
from utils.github_user import GitHubUserNotFound
from utils.google_user import GoogleUserNotFound, resolve_google_sub_by_email
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
    # Phase 1 only accepts 'manual' on write. The response model keeps the
    # broader union so reads can still surface whatever the DB currently holds
    # (e.g. a Phase 2-deployment value), without advertising those values as
    # valid update inputs in the OpenAPI spec.
    mode: Literal["manual"]


class AllowlistEntryResponse(TZAwareBaseModel):
    """Allowlist entry read-model.

    #655 added ``provider`` / ``subject_id`` / ``subject_label`` as the
    canonical fields. The legacy ``github_user_id`` / ``github_username``
    fields are retained on the response for backward compatibility with
    pre-#655 admin tooling — for ``provider='google'`` rows these carry
    the sentinel values written by ``add_to_allowlist_entry``
    (``"google:<sub>"`` / email), which existing GitHub-only consumers
    can ignore via the new ``provider`` field.
    """

    id: UUID
    provider: str
    subject_id: str
    subject_label: str
    # Legacy fields (deprecated #655, drop in a future migration).
    github_user_id: str
    github_username: str
    source: str
    state: str
    added_by_user_id: str | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AllowlistAddRequest(BaseModel):
    """Allowlist add payload (provider-aware since #655).

    Three accepted shapes:

    * GitHub legacy (pre-#655): ``{github_username}`` — preserved verbatim
      so existing admin tooling keeps working without a flag day.
    * GitHub canonical: ``{provider: "github", github_username}`` — same
      semantics, but with the discriminator explicit.
    * Google: ``{provider: "google", email}`` — resolves the user's
      OIDC ``sub`` by looking up an existing ``users`` row with
      ``auth_provider='google'`` and matching email (v1 bootstrap UX —
      pre-OAuth invitation lands in Phase 2).

    Pydantic validates ``github_username`` shape when present (1–39 chars,
    alphanumeric + hyphens, no leading/trailing hyphen) so malformed
    payloads don't spend a GitHub API request (rate-limited to 60/hr
    unauthenticated). The ``email`` field uses the same regex pattern
    that the rest of the codebase uses for email-string validation
    (see ``models/schemas.py:830``); switching to ``pydantic.EmailStr``
    would require pulling in ``email-validator`` as a new dependency,
    which is not warranted for a single field.
    """

    # Default provider keeps the pre-#655 ``{github_username}`` payload
    # shape working (the discriminator was absent in v1).
    provider: Literal["github", "google"] = "github"
    github_username: str | None = Field(
        default=None,
        min_length=1,
        max_length=39,
        pattern=r"^[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,37}[a-zA-Z0-9])?$",
    )
    email: str | None = Field(
        default=None,
        max_length=255,
        # Same shape as the InvitationCreateRequest email field
        # (models/schemas.py:830) — kept identical so admin/invitation
        # flows share one validation surface.
        pattern=r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$",
    )

    @model_validator(mode="after")
    def _enforce_provider_field_match(self) -> AllowlistAddRequest:
        """Reject payloads where the supplied field doesn't match the provider.

        Without this, an admin could pass ``{provider: "github", email: ...}``
        and have the extra field silently ignored — an "add with extra data
        success" surface that hides typos. We want the request to fail
        explicitly with a 422 so the admin sees the mismatch (PR #657
        Copilot review #3).
        """
        if self.provider == "github":
            if self.email is not None:
                raise ValueError(
                    "email must not be set when provider='github'; use github_username instead"
                )
            if self.github_username is None:
                raise ValueError("github_username is required when provider='github'")
        else:  # provider == "google"
            if self.github_username is not None:
                raise ValueError(
                    "github_username must not be set when provider='google'; use email instead"
                )
            if self.email is None:
                raise ValueError("email is required when provider='google'")
        return self


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
    # Phase 2 modes are rejected by Pydantic at parse time (422) via the
    # narrowed Literal on SignupGateConfigUpdate.mode. No runtime guard
    # needed here.
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
    """Add a GitHub or Google user to the manual allowlist (#655).

    Provider is set explicitly via ``payload.provider`` (defaulting to
    ``"github"`` for pre-#655 callers). The required field per provider:

    * ``provider="github"`` → ``github_username`` (resolved to numeric ID
      via the GitHub API).
    * ``provider="google"`` → ``email`` (resolved to OIDC ``sub`` via the
      local ``users`` table; the user must have OAuth'd at least once).
    """
    svc = SignupGateService(db)
    try:
        if payload.provider == "github":
            if not payload.github_username:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="github_username is required when provider='github'",
                )
            entry = await svc.add_to_allowlist(
                github_username=payload.github_username,
                added_by_user_id=user["user_id"],
            )
        else:  # provider == "google"
            if not payload.email:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="email is required when provider='google'",
                )
            sub, canonical_email = await resolve_google_sub_by_email(payload.email, db)
            entry = await svc.add_to_allowlist_entry(
                provider="google",
                subject_id=sub,
                subject_label=canonical_email,
                added_by_user_id=user["user_id"],
            )
    except GitHubUserNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"GitHub user '{payload.github_username}' not found",
        ) from exc
    except GoogleUserNotFound as exc:
        # Distinct from the GitHub 404: the user simply hasn't OAuth'd yet
        # against this app, so we can't resolve their sub. Surface the
        # bootstrap UX hint from the exception message verbatim.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        # GitHub API unreachable / rate-limited. 60 req/hr unauth means a busy
        # admin session can exhaust the quota; surface that as 502 with an
        # actionable message rather than leaking the raw httpx exception.
        logger.warning(
            "github_api_unavailable",
            github_username=payload.github_username,
            error=str(exc),
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
