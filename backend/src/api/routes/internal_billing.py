"""Internal billing-service endpoints (Issue #954).

The external billing service (kagura-ai/kagura-billing#4/#6) pushes entitlement
changes here. memory-cloud stays the **authorization source of truth** for
entitlement (plan tier + addon quota), so it never imports the Stripe SDK or
holds Stripe secrets — the billing service owns subscription lifecycle and only
pushes the resolved entitlement.

Design decisions (documented for the cross-repo contract; see #954):

- **SoT boundary.** memory-cloud persists *entitlement* only: ``plan_name``
  (tier) + the ``addon_*`` bonuses (quota). Subscription lifecycle fields
  (``status``, ``current_period_end``) are billing-owned — they are accepted in
  the contract for audit/forward-compat and echoed back, but NOT persisted here
  (no schema commitment until the kagura-billing RFC settles).
- **No destructive cascade.** Unlike the interactive owner-facing
  ``PUT /api/v1/workspaces/{id}/plan`` (member removal, memory transfer, token
  revocation, guarded downgrades), this automated webhook ONLY sets the
  canonical entitlement. Feature/quota enforcement is gate-time (reads
  ``plan_name``), so a downgrade takes effect immediately without this endpoint
  silently destroying members/memories on a billing glitch. Billing-driven
  membership/context cleanup is handled by the interactive flow or a
  reconciliation job (kagura-billing#5), not here.
- **Idempotent.** PUT sets absolute values; re-delivery (reconciliation) yields
  the same state and 200, never a "already on this plan" 400.
- **Internal-only.** Mounted under ``/internal`` (NOT ``/api/v1``) so it stays
  off the public surface (#622 freeze) and is easy to block at the edge. Reach
  it only over the internal network.
"""

from __future__ import annotations

import secrets
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config.plan_tiers import PLAN_TIERS
from config.settings import get_settings
from db.base import get_db
from models.api_base import TZAwareBaseModel
from models.auth import Workspace
from utils.exceptions import (
    AuthenticationError,
    MemoryCloudException,
    NotFoundException,
    ValidationError,
)
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/internal", tags=["internal-billing"])

# Friendly addon dimension name → Workspace column. Billing pushes absolute
# bonus values keyed by these stable names so the wire contract is decoupled
# from the ORM column names. Unknown keys are rejected (contract-mismatch guard).
_ADDON_COLUMNS: dict[str, str] = {
    "memory": "addon_memory_bonus",
    "mcp_quota": "addon_mcp_quota_bonus",
    "rest_quota": "addon_rest_quota_bonus",
    "public_quota": "addon_public_quota_bonus",
    "member": "addon_member_bonus",
    "context": "addon_context_bonus",
    "analysis": "addon_analysis_bonus",
    "storage_mb": "addon_storage_bonus_mb",
    "sleep_contexts": "addon_sleep_contexts_bonus",  # Issue #560
    "connector": "addon_connector_bonus",
}


async def verify_billing_service_token(authorization: str | None = Header(None)) -> None:
    """Authenticate the billing service by its shared service token (RFC 6750 Bearer).

    Fail-closed: an unset ``BILLING_SERVICE_TOKEN`` disables the endpoint (503),
    so a misconfigured deployment never accepts unauthenticated entitlement
    pushes. Mirrors ``workers.verify_worker_token``.
    """
    expected = get_settings().billing_service_token
    if not expected:
        # No canonical 503 subclass for "endpoint disabled"; raise the base
        # MemoryCloudException so the global handler emits the canonical envelope
        # (and we avoid a raw HTTPException — #992 ratchet).
        raise MemoryCloudException(
            "Internal billing endpoint is not configured",
            status_code=503,
            error_code="BILLING-001",
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise AuthenticationError("Billing service token required")
    token = authorization[len("Bearer ") :]
    if not secrets.compare_digest(token, expected):
        raise AuthenticationError("Invalid billing service token")


class BillingPlanPush(BaseModel):
    """Entitlement push from the billing service (#954 boundary contract)."""

    plan_name: str = Field(..., description="Entitlement tier: free | basic | pro")
    status: str | None = Field(
        default=None,
        max_length=50,
        description=(
            "Billing subscription status (Stripe-agnostic free-form). Billing-owned; "
            "accepted for audit/forward-compat, NOT persisted or enforced here."
        ),
    )
    current_period_end: datetime | None = Field(
        default=None,
        description="Subscription period end. Billing-owned; accepted for audit, not persisted here.",
    )
    addons: dict[str, int] | None = Field(
        default=None,
        description=(
            "Absolute addon bonus values keyed by addon dimension "
            "(memory, mcp_quota, rest_quota, public_quota, member, context, "
            "analysis, storage_mb, sleep_contexts, connector). Partial update: "
            "omitted dimensions are left unchanged."
        ),
    )


class BillingPlanPushResult(TZAwareBaseModel):
    """Echo of the applied entitlement (idempotent)."""

    workspace_id: str
    plan_name: str
    addons: dict[str, int]
    status: str | None = None
    current_period_end: datetime | None = None
    applied: bool = True


@router.put("/workspaces/{workspace_id}/plan", response_model=BillingPlanPushResult)
async def set_workspace_plan_from_billing(
    workspace_id: str,
    body: BillingPlanPush,
    _: None = Depends(verify_billing_service_token),
    db: AsyncSession = Depends(get_db),
) -> BillingPlanPushResult:
    """Set a workspace's entitlement (plan tier + addon quota) from billing.

    Idempotent, service-authenticated, internal-only. Sets ``plan_name`` and any
    provided ``addons`` (absolute). Does not perform destructive downgrade
    cascades (see module docstring). Returns the full applied addon state.
    """
    # Validate the wire contract BEFORE any mutation. Canonical VAL-001 (422).
    if body.plan_name not in PLAN_TIERS:
        raise ValidationError(
            f"Invalid plan: {body.plan_name}. Valid plans: {list(PLAN_TIERS.keys())}",
            field="plan_name",
        )
    if body.addons:
        for key, value in body.addons.items():
            if key not in _ADDON_COLUMNS:
                raise ValidationError(
                    f"Unknown addon dimension: {key}. Valid dimensions: {sorted(_ADDON_COLUMNS)}",
                    field="addons",
                )
            if value < 0:
                raise ValidationError(
                    f"Addon '{key}' bonus must be >= 0, got {value}", field="addons"
                )

    try:
        ws_uuid = UUID(workspace_id)
    except ValueError as exc:
        raise ValidationError("Invalid workspace_id", field="workspace_id") from exc

    workspace = (
        await db.execute(select(Workspace).where(Workspace.id == ws_uuid))
    ).scalar_one_or_none()
    if workspace is None:
        raise NotFoundException("Workspace")

    # Apply entitlement (absolute set → idempotent).
    workspace.plan_name = body.plan_name
    if body.addons:
        for key, value in body.addons.items():
            setattr(workspace, _ADDON_COLUMNS[key], value)
    await db.commit()

    current_addons = {key: getattr(workspace, col) for key, col in _ADDON_COLUMNS.items()}
    logger.info(
        "billing_plan_pushed",
        workspace_id=workspace_id,
        plan_name=body.plan_name,
        billing_status=body.status,
        addons=body.addons,
    )
    return BillingPlanPushResult(
        workspace_id=workspace_id,
        plan_name=workspace.plan_name,
        addons=current_addons,
        status=body.status,
        current_period_end=body.current_period_end,
    )
