"""Owner-only billing handoff endpoint (#1093).

``POST /api/v1/billing/handoff`` lets an authenticated workspace **owner** start
a billing session on the external billing host without that host re-implementing
user auth. It mints a short-lived Ed25519-signed token (see
``auth.billing_handoff.BillingHandoffSigner``) bound to
``(user_id, workspace_id, role=owner)``.

Two-layer gate, deliberately strict:

1. **session-auth only** (``SessionUser`` → ``require_session_auth``): a Bearer
   credential (API key / OAuth) is rejected 403. A leaked long-lived API key must
   never be able to open a billing session (mirrors the #398 billing RBAC stance).
2. **owner-only against the explicit target workspace**: the owner check runs on
   the request-body ``workspace_id``, never the caller's mutable
   ``current_workspace_id`` — closing the #389 multi-workspace cross-tenant trap.

Stripe-agnostic and always mounted (unlike the ``BILLING_ENABLED``-gated Stripe
plugin): the token is the auth primitive the billing host consumes, independent
of which payment processor it wraps.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.billing_handoff import BillingHandoffSigner
from auth.dependencies import SessionUser
from db.base import get_db
from models.api_base import TZAwareBaseModel
from models.auth import Workspace
from services.permission_service import PermissionService
from utils.auth_helpers import get_user_id

router = APIRouter(prefix="/billing", tags=["billing-handoff"])


class BillingHandoffRequest(BaseModel):
    """Request to mint a handoff token for one owned workspace."""

    workspace_id: UUID = Field(
        ...,
        description=(
            "The workspace to start a billing session for. The caller MUST be "
            "its owner — the owner check binds to this value, not the session's "
            "current workspace."
        ),
    )


class BillingHandoffResponse(TZAwareBaseModel):
    """The minted handoff token and the metadata the billing host needs."""

    token: str = Field(..., description="Ed25519-signed JWT (alg=EdDSA).")
    token_type: str = Field(default="billing_handoff", description="Token kind discriminator.")
    kid: str = Field(..., description="Signing key id — selects the verifier's public key.")
    jti: str = Field(..., description="Unique token id (verifier enforces single-use).")
    expires_at: datetime = Field(..., description="Token expiry (UTC, short-lived).")


@router.post("/handoff", response_model=BillingHandoffResponse)
async def mint_billing_handoff(
    body: BillingHandoffRequest,
    user: SessionUser,
    db: AsyncSession = Depends(get_db),
) -> BillingHandoffResponse:
    """Mint an owner-scoped, short-lived billing handoff token.

    Owner-only + session-only. Returns 403 for admin/member/API-key callers and
    503 when the signing key is unconfigured (fail-closed).
    """
    user_id = get_user_id(user)

    # Owner gate against the EXPLICIT target workspace — check_workspace_owner
    # raises AuthorizationError (403) for non-owners and never trusts
    # current_workspace_id, so a multi-workspace member cannot mint for a
    # workspace they merely belong to (#389 guard).
    await PermissionService(db).check_workspace_owner(user_id, body.workspace_id)

    # Stamp the workspace's live ownership epoch into the token (#1100) so the
    # external verifier can reject it once ownership is transferred (the epoch
    # advances). Read AFTER the owner gate confirmed the workspace exists; a newer
    # epoch read here would only make the token immediately stale (fail-safe).
    ownership_epoch = (
        await db.execute(
            select(Workspace.ownership_epoch).where(
                Workspace.id == body.workspace_id,
                Workspace.deleted_at.is_(None),
            )
        )
    ).scalar_one()

    # Mint AFTER authz so an unconfigured deployment returns 403 to non-owners
    # (no info leak) and 503 only to an authorized owner.
    minted = BillingHandoffSigner().mint(
        user_id=user_id,
        workspace_id=body.workspace_id,
        ownership_epoch=ownership_epoch,
    )

    # Issuance is audit-logged exactly once, inside BillingHandoffSigner.mint()
    # as "billing_handoff_minted" (same fields + expires_at) — no duplicate here.
    return BillingHandoffResponse(
        token=minted.token,
        kid=minted.kid,
        jti=minted.jti,
        expires_at=minted.expires_at,
    )
