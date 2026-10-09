"""Internal billing-service endpoints (Issue #954).

The external billing service pushes entitlement
changes here. memory-cloud stays the **authorization source of truth** for
entitlement (plan tier + addon quota), so it never imports the Stripe SDK or
holds Stripe secrets — the billing service owns subscription lifecycle and only
pushes the resolved entitlement.

Design decisions (documented for the cross-repo contract; see #954):

- **SoT boundary.** memory-cloud persists *entitlement* only: ``plan_name``
  (tier) + the ``addon_*`` bonuses (quota). Subscription lifecycle fields
  (``status``, ``current_period_end``) are billing-owned — they are accepted in
  the contract for audit/forward-compat and echoed back, but NOT persisted here
  (no schema commitment until the billing RFC settles).
- **No destructive cascade.** Unlike the interactive owner-facing
  ``PUT /api/v1/workspaces/{id}/plan`` (member removal, memory transfer, token
  revocation, guarded downgrades), this automated webhook ONLY sets the
  canonical entitlement. Feature/quota enforcement is gate-time (reads
  ``plan_name``), so a downgrade takes effect immediately without this endpoint
  silently destroying members/memories on a billing glitch. Billing-driven
  membership/context cleanup is handled by the interactive flow or a
  reconciliation job, not here.
- **Idempotent, full-replace addons.** PUT sets absolute values; re-delivery
  (reconciliation) yields the same state and 200, never a "already on this plan"
  400. When ``addons`` is provided it is the **complete desired addon state**:
  every dimension absent from the map is reset to 0, so a tier change cannot
  strand a prior tier's addon bonus (over-grant). The billing service therefore
  pushes the full addon state for the new tier on every change (an empty ``{}``
  zeros all). Omit ``addons`` entirely (null) for a tier-only change that leaves
  the existing addon bonuses untouched.
- **Internal-only.** Mounted under ``/internal`` (NOT ``/api/v1``) so it stays
  off the public surface (#622 freeze). It is also blocked at the edge:
  ``terraform/single-server/Caddyfile.tpl`` has a ``handle /internal* {respond
  404}`` block and ``deploy.sh`` fails the deploy via ``verify_internal_blocked``
  if that block is ever missing. Reach it only over the internal Docker network;
  the ``BILLING_SERVICE_TOKEN`` bearer is the in-app control on top of that.
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
from models.auth import ENTITLEMENT_SOURCE_EXTERNAL_BILLING, Workspace
from services.downgrade_eligibility_service import (
    DowngradeEligibilityService,
    TierDowngradeEligibility,
)
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

# Upper bound on any addon bonus, matching the admin quota endpoint's
# ``le=2_000_000_000``. Keeps an oversized push a clean 422 instead of letting it
# overflow the INTEGER column and surface as a 500 at commit.
_ADDON_MAX_BONUS = 2_000_000_000


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

    plan_name: str = Field(
        ...,
        description="Entitlement tier: one of the registered plan keys (free | basic | pro | promax)",
    )
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
            "analysis, storage_mb, sleep_contexts, connector). FULL REPLACE: when "
            "provided this is the complete addon state — dimensions omitted from "
            "the map are reset to 0 (an empty object zeros all). Omit the field "
            "entirely (null) to leave existing addons unchanged (tier-only change)."
        ),
    )


class BillingPlanPushResult(TZAwareBaseModel):
    """Echo of the applied entitlement (idempotent)."""

    workspace_id: str
    plan_name: str
    addons: dict[str, int]
    entitlement_source: str
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

    Idempotent, service-authenticated, internal-only. Sets ``plan_name``; when
    ``addons`` is provided it is applied as a FULL REPLACE (dimensions omitted
    from the map reset to 0) so a tier change cannot strand a prior tier's addon
    bonus; omit ``addons`` to leave them unchanged. Does not perform destructive
    downgrade cascades (see module docstring). Returns the full applied addon state.
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
            if value > _ADDON_MAX_BONUS:
                raise ValidationError(
                    f"Addon '{key}' bonus exceeds the maximum {_ADDON_MAX_BONUS}, got {value}",
                    field="addons",
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

    # Apply entitlement (absolute set → idempotent). Mark provenance as
    # billing-owned (#1095) so the external reconciler may reconcile this row;
    # a prior admin/comp grant is overwritten here by an explicit billing push.
    workspace.plan_name = body.plan_name
    workspace.entitlement_source = ENTITLEMENT_SOURCE_EXTERNAL_BILLING
    # Full-replace: a provided ``addons`` map is the COMPLETE desired state — zero
    # every dimension first, then apply the provided values, so a partial or empty
    # push cannot leave a higher tier's addon bonus stranded (over-grant).
    # ``addons is None`` (field omitted) is a tier-only change: leave addons as-is.
    if body.addons is not None:
        for col in _ADDON_COLUMNS.values():
            setattr(workspace, col, 0)
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
        entitlement_source=workspace.entitlement_source,
    )
    return BillingPlanPushResult(
        workspace_id=workspace_id,
        plan_name=workspace.plan_name,
        addons=current_addons,
        entitlement_source=workspace.entitlement_source,
        status=body.status,
        current_period_end=body.current_period_end,
    )


class WorkspaceEntitlementView(BaseModel):
    """Read model for the reconciler (#1095): the current entitlement + provenance."""

    workspace_id: str
    plan_name: str
    addons: dict[str, int]
    entitlement_source: str


@router.get("/workspaces/{workspace_id}/plan", response_model=WorkspaceEntitlementView)
async def get_workspace_entitlement(
    workspace_id: str,
    _: None = Depends(verify_billing_service_token),
    db: AsyncSession = Depends(get_db),
) -> WorkspaceEntitlementView:
    """Read a workspace's current entitlement + ``entitlement_source`` (#1095).

    Service-authenticated, internal-only. The external reconciler reads this to
    decide whether to reconcile a workspace (``external_billing``) or leave it
    untouched (``admin_grant``) — so a periodic billing reconcile never reverts a
    locally-owned admin/comp grant.
    """
    try:
        ws_uuid = UUID(workspace_id)
    except ValueError as exc:
        raise ValidationError("Invalid workspace_id", field="workspace_id") from exc

    # Soft-delete safe (#687/#681 pattern): a soft-deleted workspace is "gone" →
    # 404, so the reconciler treats it as cancellable rather than resurrecting a
    # stale entitlement. (The idempotent PUT deliberately does NOT filter — a #954
    # reconciliation set may target a just-deleted row; a READ for a skip decision
    # is the opposite concern.)
    workspace = (
        await db.execute(
            select(Workspace).where(Workspace.id == ws_uuid, Workspace.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if workspace is None:
        raise NotFoundException("Workspace")

    current_addons = {key: getattr(workspace, col) for key, col in _ADDON_COLUMNS.items()}
    return WorkspaceEntitlementView(
        workspace_id=workspace_id,
        plan_name=workspace.plan_name,
        addons=current_addons,
        entitlement_source=workspace.entitlement_source,
    )


class DowngradeEligibilityView(BaseModel):
    """Usage-fit downgrade eligibility for every tier below the current one (#1123)."""

    workspace_id: str
    current_plan: str
    targets: list[TierDowngradeEligibility]


@router.get(
    "/workspaces/{workspace_id}/downgrade-eligibility",
    response_model=DowngradeEligibilityView,
)
async def get_downgrade_eligibility(
    workspace_id: str,
    _: None = Depends(verify_billing_service_token),
    db: AsyncSession = Depends(get_db),
) -> DowngradeEligibilityView:
    """Report whether a workspace's usage fits each lower tier (#1123).

    Service-authenticated, internal-only. memory-cloud is the usage SoT; the
    external billing service gates its portal downgrade UI on this so it never
    offers a downgrade that current usage cannot satisfy (purchased addons are
    kept — the fit is against the target tier base + retained addon bonuses).
    The absolute ``PUT .../plan`` push stays reconcile-safe and does NOT itself
    enforce this guard; enforcement is the portal's responsibility, informed by
    this read. Soft-deleted workspaces 404 (consistent with the entitlement read).
    """
    try:
        ws_uuid = UUID(workspace_id)
    except ValueError as exc:
        raise ValidationError("Invalid workspace_id", field="workspace_id") from exc

    workspace = (
        await db.execute(
            select(Workspace).where(Workspace.id == ws_uuid, Workspace.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if workspace is None:
        raise NotFoundException("Workspace")

    targets = await DowngradeEligibilityService(db).evaluate(workspace)
    return DowngradeEligibilityView(
        workspace_id=workspace_id,
        current_plan=workspace.plan_name,
        targets=targets,
    )


# ============================================================================
# Pre-expiry capacity notice (#1941)
# ============================================================================

# A notice is sent at most once per (workspace, period_end). The key outlives
# the period end by this much so a late retry from the billing service is
# still recognised as a repeat.
_NOTICE_KEY_GRACE_SECONDS = 7 * 24 * 3600


class CapacityLockNoticeRequest(BaseModel):
    """Body of ``POST /internal/workspaces/{id}/capacity-lock-notice`` (#1941)."""

    period_end: datetime = Field(
        ..., description="When the paid period ends and the workspace returns to Free"
    )


class CapacityLockNoticeResult(BaseModel):
    """Outcome of a notice request. Never carries the recipient address.

    ``reason`` (when ``sent`` is false): ``within_capacity`` (the projected
    Free state fits — nothing to warn about), ``already_sent`` (a notice for
    this ``period_end`` was already sent), ``no_owner_email`` or
    ``delivery_failed`` (retry later; the idempotency claim is released).
    """

    sent: bool
    reason: str | None = None
    over_memories: int
    over_bytes: int


def _naive_utc(value: datetime) -> datetime:
    """An aware instant as naive UTC (the codebase's storage convention)."""
    offset = value.utcoffset()
    return value if offset is None else value.replace(tzinfo=None) - offset


def _notice_key(workspace_id: UUID, period_end: datetime) -> str:
    end = _naive_utc(period_end).isoformat(timespec="seconds")
    return f"capacity_lock_notice:{workspace_id}:{end}"


def _notice_ttl_seconds(period_end: datetime) -> int:
    from utils.datetime import utcnow

    remaining = int((_naive_utc(period_end) - utcnow()).total_seconds())
    return max(remaining, 0) + _NOTICE_KEY_GRACE_SECONDS


@router.post(
    "/workspaces/{workspace_id}/capacity-lock-notice",
    response_model=CapacityLockNoticeResult,
)
async def send_capacity_lock_notice(
    workspace_id: str,
    body: CapacityLockNoticeRequest,
    _: None = Depends(verify_billing_service_token),
    db: AsyncSession = Depends(get_db),
) -> CapacityLockNoticeResult:
    """Warn the owner before a paid period ends that Free would lock the workspace (#1941).

    memory-cloud does not store ``current_period_end``, so the billing service
    decides WHEN (e.g. 7 days before the end of a cancelled subscription) and
    calls this. memory-cloud evaluates the projected Free state with the same
    counting as the capacity lock — Free tier limits plus the bonuses a
    downgrade keeps, files of deleted contexts excluded — and, when over,
    emails the workspace owner (plain text, no memory content).

    Idempotent per ``(workspace, period_end)``: a repeat answers
    ``sent=false, reason="already_sent"``. A failed delivery releases the
    claim so a retry can send. 404 for a missing or soft-deleted workspace.
    """
    from db.redis import get_redis_client
    from models.auth import User
    from services.capacity_lock import projected_free_capacity
    from services.email_service import get_email_service

    try:
        ws_uuid = UUID(workspace_id)
    except ValueError as exc:
        raise ValidationError("Invalid workspace_id", field="workspace_id") from exc

    workspace = (
        await db.execute(
            select(Workspace).where(Workspace.id == ws_uuid, Workspace.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if workspace is None:
        raise NotFoundException("Workspace")

    lock = await projected_free_capacity(db, workspace)
    if lock is None:
        return CapacityLockNoticeResult(
            sent=False, reason="within_capacity", over_memories=0, over_bytes=0
        )

    def result(sent: bool, reason: str | None = None) -> CapacityLockNoticeResult:
        return CapacityLockNoticeResult(
            sent=sent,
            reason=reason,
            over_memories=lock.over_memories,
            over_bytes=lock.over_bytes,
        )

    owner_email = (
        await db.execute(select(User.email).where(User.user_id == workspace.owner_user_id))
    ).scalar_one_or_none()
    if not owner_email:
        logger.warning("capacity_lock_notice_no_owner_email", workspace_id=workspace_id)
        return result(False, "no_owner_email")

    key = _notice_key(ws_uuid, body.period_end)
    redis = get_redis_client()
    try:
        claimed = await redis.set(key, "1", nx=True, ex=_notice_ttl_seconds(body.period_end))
    except Exception as exc:  # noqa: BLE001 — fail closed: never risk a duplicate
        logger.warning(
            "capacity_lock_notice_dedup_unavailable",
            workspace_id=workspace_id,
            error_type=type(exc).__name__,
        )
        raise MemoryCloudException(
            "Notice deduplication is unavailable; retry later",
            status_code=503,
            error_code="BILLING-002",
        ) from exc
    if not claimed:
        return result(False, "already_sent")

    base_url = get_settings().frontend_url.strip().rstrip("/")
    try:
        delivered = await get_email_service().send_capacity_lock_notice(
            to_email=owner_email,
            workspace_name=workspace.name,
            period_end=body.period_end,
            over_memories=lock.over_memories,
            over_bytes=lock.over_bytes,
            cleanup_url=lock.cleanup_url,
            contexts_url=f"{base_url}/workspace/contexts",
        )
    except Exception as exc:  # noqa: BLE001 — implementations must not raise; be safe
        logger.warning(
            "capacity_lock_notice_send_raised",
            workspace_id=workspace_id,
            error_type=type(exc).__name__,
        )
        delivered = False
    if not delivered:
        try:
            await redis.delete(key)
        except Exception:  # noqa: BLE001 — the TTL still bounds it
            logger.warning("capacity_lock_notice_release_failed", workspace_id=workspace_id)
        return result(False, "delivery_failed")

    logger.info(
        "capacity_lock_notice_sent",
        workspace_id=workspace_id,
        over_memories=lock.over_memories,
        over_bytes=lock.over_bytes,
    )
    return result(True)
