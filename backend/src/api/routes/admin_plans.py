"""Admin Plan Management API Routes.

Issue #149: Plan tier enforcement - Admin management endpoints
Issue #252: Session-only authentication (no API keys)

Allows admins to:
- View all workspaces with plan info
- Change workspace plan tiers
- Set custom quota overrides
- View plan change audit log
"""

import dataclasses
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import require_admin as auth_require_admin
from config.plan_tiers import PLAN_TIERS, PlanName, get_plan_tier
from db.base import get_db
from models.auth import (
    ENTITLEMENT_SOURCE_ADMIN_GRANT,
    Context,
    PlanChange,
    User,
    Workspace,
    WorkspaceInvitation,
    WorkspaceMember,
)
from models.memory import Memory
from models.resource import WorkspaceAddon  # Issue #665: row-based admin grants
from services.addon_calculator_service import ADDON_UNIT_VALUES, AddonCalculatorService
from services.effective_quota_service import EffectiveQuotaService
from utils import db_transaction
from utils.datetime import to_utc_iso, utcnow
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/admin/plans", tags=["admin-plans"])


# ============================================================================
# Request/Response Models
# ============================================================================


class AdminWorkspacePlanInfo(BaseModel):
    """Workspace with plan information.

    Issue #276: Slug removed.
    """

    id: str
    name: str
    plan_name: str
    owner_user_id: str
    owner_name: str | None = None
    owner_email: str | None = None
    total_memories: int
    memory_limit: int
    mcp_calls_per_day: int
    mcp_calls_per_week: int


class AdminUpdatePlanRequest(BaseModel):
    """Request to update workspace plan tier."""

    plan_name: str = Field(..., pattern=r"^(free|basic|pro)$")
    reason: str | None = None


class QuotaBreakdown(BaseModel):
    """Quota values for a single tier (Issue #665).

    Extended to 9 fields so the GET ``effective`` block surfaces every
    addon type the PUT handler accepts — review-finding #8 fixed the
    write-only hole where storage / rest_quota / public_quota /
    sleep_contexts could be set but not read back as an effective value.

    The legacy 5 fields stay required (callers always populated them);
    the 4 new fields default to 0 so existing TS consumers ignore the
    additive keys without runtime impact.
    """

    memory_limit: int
    mcp_calls_per_day: int
    max_contexts: int
    max_members: int
    analysis_runs_per_day: int
    rest_calls_per_day: int = 0
    public_calls_per_day: int = 0
    storage_bytes_limit: int = 0
    sleep_enabled_contexts_limit: int = 0
    max_resource_tokens: int = 0  # Issue #663: tier-fixed (no addon), surfaced read-only
    max_connectors: int = 0  # Spec 2026-06-02: tier base for the extra_connectors addon


class AddonValues(BaseModel):
    """Addon bonus values.

    Issue #665: Extended to expose all 9 addon cache columns so the admin
    UI can read back any value the PUT handler accepts. Pre-#665 only the
    5 that were directly writable from the legacy handler were surfaced.
    """

    memory_bonus: int = 0
    mcp_quota_bonus: int = 0
    rest_quota_bonus: int = 0
    public_quota_bonus: int = 0
    member_bonus: int = 0
    context_bonus: int = 0
    analysis_bonus: int = 0
    storage_bonus_mb: int = 0
    sleep_contexts_bonus: int = 0
    connector_bonus: int = 0


class UsageValues(BaseModel):
    """Current usage values."""

    memories: int
    contexts: int
    members: int


class SpendCapValues(BaseModel):
    """BYOK embedding spend cap values (Issue #709).

    All amounts are USD floats; ``None`` means "uncapped". Both the override
    (per-workspace, on ``Workspace``) and the tier default (on ``PlanTier``)
    are surfaced so the admin UI can render "override / tier default /
    effective" the same way the addon breakdown does for additive quotas.
    """

    tier_default_daily_usd: float | None = None
    tier_default_monthly_usd: float | None = None
    override_daily_usd: float | None = None
    override_monthly_usd: float | None = None
    effective_daily_usd: float | None = None
    effective_monthly_usd: float | None = None
    current_daily_usd: float = 0.0
    current_monthly_usd: float = 0.0


class WorkspaceQuotaDetail(BaseModel):
    """Detailed quota breakdown for a workspace."""

    workspace_id: str
    workspace_name: str
    plan_name: str
    base: QuotaBreakdown
    addon: AddonValues
    effective: QuotaBreakdown
    usage: UsageValues
    spend_cap: SpendCapValues | None = None  # Issue #709


class UpdateAddonRequest(BaseModel):
    """Request to update workspace addon bonuses (Issue #665).

    All 9 fields are **optional with no-touch semantics**: when a field is
    omitted from the request body, the corresponding admin grant is left
    untouched. To zero out an addon, the client must send the field
    explicitly with value ``0``.

    Concrete values are bounded ``0 <= value <= 2_000_000_000``. The upper
    bound is below PostgreSQL ``INTEGER`` max (2_147_483_647) so the
    multiplied-out cache-column write at recalc time never overflows.
    Per-handler logic additionally rejects values lower than the active
    Stripe-purchased SUM for the same addon type (HTTP 400) so silent
    clamping cannot happen.

    Wire-format compatibility note (#665 review fix #2): pre-#665 the 5
    legacy fields defaulted to ``int = Field(0)`` so omitting them meant
    "set to zero". That semantics is incompatible with the row-based SSoT
    design — an omitted-default 0 would silently DELETE existing
    WorkspaceAddon admin_grant rows on partial-update PUTs. The unified
    optional shape closes this footgun. Clients that intentionally want
    to zero a field continue to work by sending the field explicitly with
    value 0; clients that omit the field get the safer no-touch behavior.
    """

    addon_memory_bonus: int | None = Field(None, ge=0, le=2_000_000_000)
    addon_mcp_quota_bonus: int | None = Field(None, ge=0, le=2_000_000_000)
    addon_member_bonus: int | None = Field(None, ge=0, le=2_000_000_000)
    addon_context_bonus: int | None = Field(None, ge=0, le=2_000_000_000)
    addon_analysis_bonus: int | None = Field(None, ge=0, le=2_000_000_000)
    addon_rest_quota_bonus: int | None = Field(None, ge=0, le=2_000_000_000)
    addon_public_quota_bonus: int | None = Field(None, ge=0, le=2_000_000_000)
    addon_storage_bonus_mb: int | None = Field(None, ge=0, le=2_000_000_000)
    addon_sleep_contexts_bonus: int | None = Field(None, ge=0, le=2_000_000_000)
    addon_connector_bonus: int | None = Field(None, ge=0, le=2_000_000_000)


class UpdateSpendCapRequest(BaseModel):
    """Request to update the per-workspace embedding spend cap (Issue #709).

    Both fields are optional: ``None`` means "remove the per-workspace
    override and fall back to the tier default" (NOT "uncap"). To genuinely
    uncap a workspace below its tier default, an admin would need to bump
    the workspace to a tier with no default cap (or override the tier's
    cap via ``plan_*_embedding_*_cap_usd`` env vars).

    Values are bounded ``>= 0`` (CHECK constraint enforces same on the DB);
    the route handler additionally rejects values above the workspace's
    current tier default to keep tier-bounded edit affordance honest.
    """

    embedding_daily_cap_usd: float | None = Field(None, ge=0)
    embedding_monthly_cap_usd: float | None = Field(None, ge=0)


class PlanChangeAuditEntry(BaseModel):
    """Plan change audit log entry."""

    id: int
    workspace_id: str
    workspace_name: str
    old_plan: str | None
    new_plan: str
    changed_by: str
    changed_at: str
    reason: str | None


class AddonCacheDriftEntry(BaseModel):
    """One drifted addon cache column for a workspace (Issue #799).

    Reports a workspace whose cached ``addon_*_bonus`` column disagrees with
    the row-based SSoT (``SUM(active WorkspaceAddon.quantity) × unit_value``).
    A healthy system returns an empty list; the ``e23_799`` normalization
    migration brings the live count to 0.
    """

    workspace_id: str
    workspace_name: str
    addon_type: str
    cache_column: str
    cache_value: int
    expected_value: int


class PlanTierInfo(BaseModel):
    """Plan tier configuration served to the admin tiers comparison table.

    Issue #664. Reflects environment variable overrides applied at module
    import time (see ``config.plan_tiers._apply_settings_overrides``).
    """

    name: str
    display_name: str
    price_monthly: int
    max_contexts_per_workspace: int
    max_members_per_workspace: int
    max_resource_tokens: int
    memory_limit: int
    mcp_calls_per_day: int
    mcp_calls_per_week: int
    rest_calls_per_day: int
    rest_calls_per_week: int
    public_calls_per_day: int
    public_calls_per_week: int
    bound_public_calls_per_minute: int
    analysis_runs_per_day: int
    storage_limit_bytes: int
    sleep_enabled_contexts_limit: int
    embedding_daily_cap_usd: float | None = None  # Issue #709
    embedding_monthly_cap_usd: float | None = None  # Issue #709
    allows_shared_contexts: bool
    features: list[str]


# ============================================================================
# Admin-only Dependency
# ============================================================================

# Issue #252: Use existing require_admin from auth.dependencies (Session-only)
require_admin = auth_require_admin


# ============================================================================
# Admin Plan Management Endpoints
# ============================================================================


@router.get("/workspaces", response_model=list[AdminWorkspacePlanInfo])
async def list_workspaces_with_plans(
    admin_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """List all workspaces with plan info and usage stats.

    Admin-only endpoint - shows all workspaces in the system.

    Returns:
        List of all workspaces with plan tiers and current usage
    """
    async with db_transaction(db, "list_workspaces_with_plans", "Failed to list workspaces"):
        # Get ALL workspaces (admin can see all)
        workspaces_result = await db.execute(
            select(Workspace).where(Workspace.deleted_at.is_(None)).order_by(Workspace.created_at)
        )
        workspaces = workspaces_result.scalars().all()

        # Get owner info for all workspaces
        from models.auth import User

        owner_ids = [workspace.owner_user_id for workspace in workspaces]
        if owner_ids:
            users_result = await db.execute(select(User).where(User.user_id.in_(owner_ids)))
            users = {u.user_id: u for u in users_result.scalars().all()}
        else:
            users = {}

        workspace_infos = []

        # Critical Fix: Avoid N+1 query - fetch all workspace members and memories in bulk
        workspace_ids = [w.id for w in workspaces]

        # Fetch all members for all workspaces in one query
        all_members_result = await db.execute(
            select(WorkspaceMember).where(WorkspaceMember.workspace_id.in_(workspace_ids))
        )
        members_by_workspace = {}
        for member in all_members_result.scalars():
            if member.workspace_id not in members_by_workspace:
                members_by_workspace[member.workspace_id] = []
            members_by_workspace[member.workspace_id].append(member.user_id)

        # Fetch all memory counts grouped by workspace
        memory_counts_result = await db.execute(
            select(Memory.workspace_id, func.count(Memory.id).label("memory_count"))
            .where(
                Memory.workspace_id.in_(workspace_ids),
                Memory.deleted_at.is_(None),
            )
            .group_by(Memory.workspace_id)
        )
        memory_counts_by_workspace = {
            row.workspace_id: row.memory_count for row in memory_counts_result.all()
        }

        for workspace in workspaces:
            total_memories = memory_counts_by_workspace.get(workspace.id, 0)

            owner = users.get(workspace.owner_user_id)
            workspace_infos.append(
                AdminWorkspacePlanInfo(
                    id=str(workspace.id),
                    name=workspace.name,
                    plan_name=workspace.plan_name,
                    owner_user_id=workspace.owner_user_id,
                    owner_name=owner.name if owner else None,
                    owner_email=owner.email if owner else None,
                    total_memories=total_memories,
                    memory_limit=workspace.effective_memory_limit,
                    mcp_calls_per_day=workspace.effective_mcp_calls_per_day,
                    mcp_calls_per_week=workspace.effective_mcp_calls_per_week,
                )
            )

        logger.info(
            f"Admin listed {len(workspace_infos)} workspaces", admin_user=admin_user["user_id"]
        )
        return workspace_infos


@router.get("/tiers", response_model=list[PlanTierInfo])
async def list_plan_tiers(
    admin_user: dict = Depends(require_admin),
) -> list[PlanTierInfo]:
    """List all plan tier configurations.

    Issue #664: Admin tiers tab consumes this endpoint instead of i18n
    hardcoded values, so values stay in sync with environment overrides
    (``PLAN_FREE_MEMORY_LIMIT`` etc.). No DB access — ``PLAN_TIERS`` is a
    process-global registry populated at import time.

    Returns:
        List of plan tier info in canonical FREE → BASIC → PRO order.
    """
    # Pydantic v2 BaseModel defaults to ``extra='ignore'``, so legacy
    # ``daily_api_limit`` / ``weekly_api_limit`` on the dataclass are
    # silently dropped — they intentionally do not surface on the admin
    # tiers tab (#664). ``features`` is overridden with a sorted list
    # because ``asdict`` materializes the frozenset in arbitrary order.
    ordered = (PlanName.FREE, PlanName.BASIC, PlanName.PRO)
    tiers = [
        PlanTierInfo(
            **{
                **dataclasses.asdict(PLAN_TIERS[plan]),
                "features": sorted(PLAN_TIERS[plan].features),
            }
        )
        for plan in ordered
    ]

    logger.info("admin_listed_plan_tiers", admin_user=admin_user["user_id"])
    return tiers


@router.put("/workspaces/{workspace_id}/plan")
async def update_workspace_plan(
    workspace_id: str,
    request: AdminUpdatePlanRequest,
    admin_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Change workspace plan tier.

    Admin-only endpoint.

    Args:
        workspace_id: Workspace ID
        request: Update plan request with new plan name and optional reason

    Returns:
        Success message

    Raises:
        404: Workspace not found
        422: Invalid plan tier
    """
    from uuid import UUID

    async with db_transaction(db, "update_workspace_plan", "Failed to update workspace plan"):
        # Get workspace
        workspace_result = await db.execute(
            select(Workspace).where(
                Workspace.id == UUID(workspace_id),
                Workspace.deleted_at.is_(None),  # #687 / #681 pattern: soft-delete safe
            )
        )
        workspace = workspace_result.scalar_one_or_none()

        if not workspace:
            raise HTTPException(status_code=404, detail=f"Workspace {workspace_id} not found")

        # Validate new plan tier
        try:
            new_plan_tier = get_plan_tier(request.plan_name)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e

        # Store old values for audit
        old_plan = workspace.plan_name
        # #805: memory_limit is no longer a Workspace column — source the audit
        # "old" value from the old plan tier (before plan_name mutates below).
        old_memory_limit = get_plan_tier(old_plan).memory_limit
        old_daily_limit = workspace.daily_api_limit
        old_weekly_limit = workspace.weekly_api_limit

        # Update workspace plan and quotas
        workspace.plan_name = request.plan_name
        workspace.daily_api_limit = new_plan_tier.daily_api_limit
        workspace.weekly_api_limit = new_plan_tier.weekly_api_limit
        # #1095: a manual system-admin set is a locally-owned grant — mark it so the
        # external billing reconciler never reverts an admin/comp grant.
        workspace.entitlement_source = ENTITLEMENT_SOURCE_ADMIN_GRANT

        # Create audit log entry
        audit_entry = PlanChange(
            workspace_id=workspace.id,
            old_plan=old_plan,
            new_plan=request.plan_name,
            changed_by=admin_user["user_id"],
            changed_at=utcnow(),
            reason=request.reason,
            old_memory_limit=old_memory_limit,
            old_daily_api_limit=old_daily_limit,
            old_weekly_api_limit=old_weekly_limit,
            new_memory_limit=new_plan_tier.memory_limit,
            new_daily_api_limit=new_plan_tier.daily_api_limit,
            new_weekly_api_limit=new_plan_tier.weekly_api_limit,
        )
        db.add(audit_entry)

        # Issue #149: If downgrading to Free, disable reranking on all contexts
        if request.plan_name == "free" and old_plan in ("basic", "pro"):
            from models.auth import Context
            from models.config import ContextSearchConfig

            # Get all contexts for this workspace
            contexts_result = await db.execute(
                select(Context).where(
                    Context.workspace_id == UUID(workspace_id), Context.deleted_at.is_(None)
                )
            )
            contexts_list = contexts_result.scalars().all()

            # Critical Fix: Avoid N+1 query - batch fetch all context configs
            context_ids = [ctx.id for ctx in contexts_list]
            configs_result = await db.execute(
                select(ContextSearchConfig).where(ContextSearchConfig.context_id.in_(context_ids))
            )
            configs_by_context = {cfg.context_id: cfg for cfg in configs_result.scalars()}

            # Disable reranking on all context configs
            disabled_count = 0
            for context in contexts_list:
                config = configs_by_context.get(context.id)

                if config and config.use_rerank:
                    config.use_rerank = False
                    disabled_count += 1
                    logger.info(
                        f"Disabled reranking for context {context.id} "
                        f"due to workspace downgrade to Free plan"
                    )

            if disabled_count > 0:
                logger.info(
                    f"Disabled reranking on {disabled_count} context(s) "
                    f"for workspace {workspace_id} (downgrade to Free)"
                )

        await db.commit()

        logger.info(
            "admin_plan_changed",
            admin_user=admin_user["user_id"],
            workspace_id=workspace_id,
            old_plan=old_plan,
            new_plan=request.plan_name,
            reason=request.reason,
        )

        return {"message": f"Plan changed from {old_plan} to {request.plan_name}"}


@router.get("/audit", response_model=list[PlanChangeAuditEntry])
async def get_plan_change_audit(
    admin_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    limit: int = 100,
):
    """Get plan change audit log.

    Admin-only endpoint.

    Args:
        limit: Maximum entries to return (default: 100)

    Returns:
        List of plan change audit entries
    """
    async with db_transaction(db, "get_plan_audit", "Failed to get audit log"):
        # Get audit entries with workspace names and user names.
        # #687: audit/history — intentionally NO Workspace.deleted_at filter.
        # The audit log records historical plan changes; entries for workspaces
        # that have since been soft-deleted must remain visible so admins can
        # reconcile past billing / plan transitions.
        audit_result = await db.execute(
            select(PlanChange, Workspace.name, User.name)
            .join(Workspace, PlanChange.workspace_id == Workspace.id)
            .outerjoin(User, PlanChange.changed_by == User.user_id)
            .order_by(PlanChange.changed_at.desc())
            .limit(limit)
        )

        entries = []
        for audit, workspace_name, user_name in audit_result.all():
            entries.append(
                PlanChangeAuditEntry(
                    id=audit.id,
                    workspace_id=str(audit.workspace_id),
                    workspace_name=workspace_name,
                    old_plan=audit.old_plan,
                    new_plan=audit.new_plan,
                    changed_by=user_name or audit.changed_by,
                    changed_at=to_utc_iso(audit.changed_at) or "",
                    reason=audit.reason,
                )
            )

        logger.info(
            f"Admin retrieved {len(entries)} audit entries", admin_user=admin_user["user_id"]
        )
        return entries


@router.get("/addon-cache-consistency", response_model=list[AddonCacheDriftEntry])
async def get_addon_cache_consistency(
    admin_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Detect workspaces whose addon cache columns drifted from the row SSoT.

    Issue #799. For every active (non-deleted) workspace and every addon type
    in ``_ADDON_FIELD_SPECS``, compares the cached ``addon_*_bonus`` column to
    ``SUM(active WorkspaceAddon.quantity) × unit_value`` — the same invariant
    enforced by ``AddonCalculatorService.recalculate_workspace_bonuses`` and
    asserted by ``tests/integration/_addon_helpers.assert_addon_invariant``.

    On-demand audit, not a startup probe: the ``e23_799`` migration brings the
    live count to 0, so a per-boot full-table scan (all workspaces × 9 columns)
    would be wasted work. Each drifted column is logged at WARNING and returned
    for admin review.

    Returns:
        List of drifted (workspace, addon_type) entries. Empty when healthy.
    """
    async with db_transaction(db, "addon_cache_consistency", "Failed to audit addon caches"):
        now = utcnow()

        # All active workspaces, keyed by id for cache-column lookup.
        ws_result = await db.execute(select(Workspace).where(Workspace.deleted_at.is_(None)))
        workspaces = {ws.id: ws for ws in ws_result.scalars().all()}

        entries: list[AddonCacheDriftEntry] = []

        # One grouped SUM per addon_type → constant query count regardless of
        # workspace count. The active-window predicate mirrors the runtime
        # recalc (addon_calculator_service.py:112-118) so the comparison
        # matches what the next recalc would compute.
        for spec in _ADDON_FIELD_SPECS:
            sum_result = await db.execute(
                select(
                    WorkspaceAddon.workspace_id,
                    func.coalesce(func.sum(WorkspaceAddon.quantity), 0),
                )
                .where(
                    WorkspaceAddon.addon_type == spec.addon_type,
                    WorkspaceAddon.active_from <= now,
                    (WorkspaceAddon.active_until.is_(None) | (WorkspaceAddon.active_until > now)),
                )
                .group_by(WorkspaceAddon.workspace_id)
            )
            summed = {ws_id: int(total) for ws_id, total in sum_result.all()}

            for ws_id, workspace in workspaces.items():
                expected = summed.get(ws_id, 0) * spec.unit_value
                actual = getattr(workspace, spec.field_name)
                if actual != expected:
                    logger.warning(
                        "addon_cache_drift",
                        workspace_id=str(ws_id),
                        addon_type=spec.addon_type,
                        cache_column=spec.field_name,
                        cache_value=actual,
                        expected_value=expected,
                    )
                    entries.append(
                        AddonCacheDriftEntry(
                            workspace_id=str(ws_id),
                            workspace_name=workspace.name,
                            addon_type=spec.addon_type,
                            cache_column=spec.field_name,
                            cache_value=actual,
                            expected_value=expected,
                        )
                    )

        logger.info(
            "addon_cache_consistency_audit",
            admin_user=admin_user["user_id"],
            workspaces_scanned=len(workspaces),
            drift_count=len(entries),
        )
        return entries


# ============================================================================
# Workspace Quota Detail Endpoints (Issue #325)
# ============================================================================


@router.get("/workspaces/{workspace_id}/quotas", response_model=WorkspaceQuotaDetail)
async def get_workspace_quotas(
    workspace_id: str,
    admin_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Get detailed quota breakdown for a workspace.

    Returns base plan limits, addon bonuses, effective totals, and current usage.
    """
    ws_uuid = UUID(workspace_id)

    async with db_transaction(db, "get_workspace_quotas", "Failed to get quota details"):
        # Get workspace
        result = await db.execute(
            select(Workspace).where(
                Workspace.id == ws_uuid,
                Workspace.deleted_at.is_(None),  # #687 / #681 pattern: soft-delete safe
            )
        )
        workspace = result.scalar_one_or_none()
        if not workspace:
            raise HTTPException(status_code=404, detail="Workspace not found")

        plan_tier = get_plan_tier(workspace.plan_name)

        # Get effective quotas
        effective_service = EffectiveQuotaService(db)
        effective = await effective_service.get_effective_quotas(ws_uuid)

        # Get usage: memories count
        memory_count_result = await db.execute(
            select(func.count(Memory.id)).where(
                Memory.workspace_id == ws_uuid,
                Memory.deleted_at.is_(None),
            )
        )
        memory_count = memory_count_result.scalar() or 0

        # Get usage: context count (non-deleted only)
        context_count_result = await db.execute(
            select(func.count(Context.id)).where(
                Context.workspace_id == ws_uuid,
                Context.deleted_at.is_(None),
            )
        )
        context_count = context_count_result.scalar() or 0

        # Get usage: member count + pending invitations
        member_count_result = await db.execute(
            select(func.count(WorkspaceMember.id)).where(WorkspaceMember.workspace_id == ws_uuid)
        )
        pending_invite_result = await db.execute(
            select(func.count(WorkspaceInvitation.id)).where(
                WorkspaceInvitation.workspace_id == ws_uuid,
                WorkspaceInvitation.accepted_at.is_(None),
                WorkspaceInvitation.expires_at > func.now(),
            )
        )
        member_count = (member_count_result.scalar() or 0) + (pending_invite_result.scalar() or 0)

        # Issue #709: BYOK embedding spend cap breakdown + current spend.
        # Spend reads from Redis counters via ``EmbeddingSpendCapService``;
        # Redis miss returns 0 (admin UI should treat 0 as "unknown" when
        # paired with an out-of-band Redis alert).
        from services.embedding_spend_cap_service import EmbeddingSpendCapService

        spend_cap_svc = EmbeddingSpendCapService(db)
        current_daily, current_monthly = await spend_cap_svc.get_current_spend(ws_uuid)
        effective_daily = workspace.effective_embedding_daily_cap_usd
        effective_monthly = workspace.effective_embedding_monthly_cap_usd
        spend_cap_payload = SpendCapValues(
            tier_default_daily_usd=plan_tier.embedding_daily_cap_usd,
            tier_default_monthly_usd=plan_tier.embedding_monthly_cap_usd,
            override_daily_usd=(
                float(workspace.embedding_daily_cap_usd)
                if workspace.embedding_daily_cap_usd is not None
                else None
            ),
            override_monthly_usd=(
                float(workspace.embedding_monthly_cap_usd)
                if workspace.embedding_monthly_cap_usd is not None
                else None
            ),
            effective_daily_usd=float(effective_daily) if effective_daily is not None else None,
            effective_monthly_usd=(
                float(effective_monthly) if effective_monthly is not None else None
            ),
            current_daily_usd=float(current_daily),
            current_monthly_usd=float(current_monthly),
        )

        return WorkspaceQuotaDetail(
            workspace_id=workspace_id,
            workspace_name=workspace.name,
            plan_name=workspace.plan_name,
            base=QuotaBreakdown(
                # #801: read the plan tier, not the per-workspace cache column.
                # Every sibling field below reads plan_tier.*; memory_limit was the
                # lone exception, so a stale Workspace.memory_limit (e.g. a PRO
                # workspace still carrying the FREE-tier 1000) made the admin dialog
                # preview off by 100x. plan_tier is the SSoT the effective calc uses.
                memory_limit=plan_tier.memory_limit,
                mcp_calls_per_day=plan_tier.mcp_calls_per_day,
                max_contexts=plan_tier.max_contexts_per_workspace,
                max_members=plan_tier.max_members_per_workspace,
                analysis_runs_per_day=plan_tier.analysis_runs_per_day,
                rest_calls_per_day=plan_tier.rest_calls_per_day,
                public_calls_per_day=plan_tier.public_calls_per_day,
                storage_bytes_limit=plan_tier.storage_limit_bytes,
                sleep_enabled_contexts_limit=plan_tier.sleep_enabled_contexts_limit,
                max_resource_tokens=plan_tier.max_resource_tokens,
                max_connectors=plan_tier.max_connectors,
            ),
            addon=AddonValues(
                memory_bonus=workspace.addon_memory_bonus,
                mcp_quota_bonus=workspace.addon_mcp_quota_bonus,
                rest_quota_bonus=workspace.addon_rest_quota_bonus,
                public_quota_bonus=workspace.addon_public_quota_bonus,
                member_bonus=workspace.addon_member_bonus,
                context_bonus=workspace.addon_context_bonus,
                analysis_bonus=workspace.addon_analysis_bonus,
                storage_bonus_mb=workspace.addon_storage_bonus_mb,
                sleep_contexts_bonus=workspace.addon_sleep_contexts_bonus,
                connector_bonus=workspace.addon_connector_bonus,
            ),
            effective=QuotaBreakdown(
                memory_limit=effective["memory_limit"],
                mcp_calls_per_day=effective["mcp_calls_per_day"],
                max_contexts=effective["max_contexts"],
                max_members=effective["max_members"],
                analysis_runs_per_day=effective["analysis_runs_per_day"],
                rest_calls_per_day=effective["rest_calls_per_day"],
                public_calls_per_day=effective["public_calls_per_day"],
                storage_bytes_limit=effective["storage_bytes_limit"],
                sleep_enabled_contexts_limit=effective["sleep_enabled_contexts_limit"],
                max_resource_tokens=effective["max_resource_tokens"],
                max_connectors=workspace.effective_max_connectors,
            ),
            usage=UsageValues(
                memories=memory_count,
                contexts=context_count,
                members=member_count,
            ),
            spend_cap=spend_cap_payload,
        )


# ---------------------------------------------------------------------------
# Issue #665: addon UPSERT spec table + helpers
# ---------------------------------------------------------------------------
# Each addon cache column maps 1:1 to:
#   - a request field on ``UpdateAddonRequest``,
#   - a ``WorkspaceAddon.addon_type`` enum value,
#   - a unit value from ``ADDON_UNIT_VALUES`` (used to convert between the
#     request's bonus integer and the row's ``quantity`` integer), and
#   - an optional "guard kind" — the name of a usage counter that must
#     not exceed the new effective limit when the bonus is reduced (LD-7).
#
# The handler iterates the spec table once for validation (raising 400 on
# any conflict BEFORE writing) and once for mutation (UPSERT or DELETE the
# admin_grant row). Adding a new addon type is a one-line change here.


@dataclasses.dataclass(frozen=True)
class _AddonFieldSpec:
    """One row in the addon UPSERT spec table (Issue #665).

    ``field_name`` doubles as the ``Workspace.addon_*_bonus`` cache column
    name; the two are identical by convention (e.g. ``addon_memory_bonus``)
    and the spec keeps a single source of truth so they cannot drift.
    """

    field_name: str  # ``UpdateAddonRequest`` AND ``Workspace`` attribute (same name)
    addon_type: str  # the ``WorkspaceAddon.addon_type`` enum value
    unit_value: int  # from ``ADDON_UNIT_VALUES`` — bonus = quantity * unit_value
    guard_kind: str | None  # 'member' / 'context' / 'memory' / 'sleep_contexts' / None


_ADDON_FIELD_SPECS: tuple[_AddonFieldSpec, ...] = (
    _AddonFieldSpec(
        "addon_memory_bonus",
        "extra_memory",
        ADDON_UNIT_VALUES["extra_memory"],
        "memory",
    ),
    _AddonFieldSpec(
        "addon_mcp_quota_bonus",
        "extra_mcp_quota",
        ADDON_UNIT_VALUES["extra_mcp_quota"],
        None,  # daily Redis-reset quota; admin throttling mid-day is allowed
    ),
    _AddonFieldSpec(
        "addon_rest_quota_bonus",
        "extra_rest_quota",
        ADDON_UNIT_VALUES["extra_rest_quota"],
        None,
    ),
    _AddonFieldSpec(
        "addon_public_quota_bonus",
        "extra_public_quota",
        ADDON_UNIT_VALUES["extra_public_quota"],
        None,
    ),
    _AddonFieldSpec(
        "addon_member_bonus",
        "extra_members",
        ADDON_UNIT_VALUES["extra_members"],
        "member",
    ),
    _AddonFieldSpec(
        "addon_context_bonus",
        "extra_contexts",
        ADDON_UNIT_VALUES["extra_contexts"],
        "context",
    ),
    _AddonFieldSpec(
        "addon_analysis_bonus",
        "extra_analysis_runs",
        ADDON_UNIT_VALUES["extra_analysis_runs"],
        None,  # daily counter; admin reduction is acceptable
    ),
    _AddonFieldSpec(
        "addon_storage_bonus_mb",
        "extra_storage",
        ADDON_UNIT_VALUES["extra_storage"],
        None,  # Storage-usage tracking not yet implemented; guard pending follow-up.
    ),
    _AddonFieldSpec(
        "addon_sleep_contexts_bonus",
        "extra_sleep_contexts",
        ADDON_UNIT_VALUES["extra_sleep_contexts"],
        "sleep_contexts",
    ),
    _AddonFieldSpec(
        "addon_connector_bonus",
        "extra_connectors",
        ADDON_UNIT_VALUES["extra_connectors"],
        None,  # Spec 2026-06-02: no usage-clamp guard (cap enforced at create time)
    ),
)


@dataclasses.dataclass(frozen=True)
class _GrantPlan:
    """Pending mutation produced by validation pass; consumed by mutation pass."""

    addon_type: str
    quantity: int  # >= 0; 0 means "delete existing admin_grant row (if any)"


async def _count_addon_usage(db: AsyncSession, workspace_id: UUID, guard_kind: str) -> int:
    """Current usage for an LD-7 persistent-addon overflow guard."""
    if guard_kind == "member":
        # Match the get_workspace_quotas accounting: active members + pending invitations.
        members_result = await db.execute(
            select(func.count(WorkspaceMember.id)).where(
                WorkspaceMember.workspace_id == workspace_id
            )
        )
        invites_result = await db.execute(
            select(func.count(WorkspaceInvitation.id)).where(
                WorkspaceInvitation.workspace_id == workspace_id,
                WorkspaceInvitation.accepted_at.is_(None),
                WorkspaceInvitation.expires_at > func.now(),
            )
        )
        return (members_result.scalar() or 0) + (invites_result.scalar() or 0)
    if guard_kind == "context":
        result = await db.execute(
            select(func.count(Context.id)).where(
                Context.workspace_id == workspace_id,
                Context.deleted_at.is_(None),
            )
        )
        return result.scalar() or 0
    if guard_kind == "memory":
        result = await db.execute(
            select(func.count(Memory.id)).where(
                Memory.workspace_id == workspace_id,
                Memory.deleted_at.is_(None),
            )
        )
        return result.scalar() or 0
    if guard_kind == "sleep_contexts":
        result = await db.execute(
            select(func.count(Context.id)).where(
                Context.workspace_id == workspace_id,
                Context.deleted_at.is_(None),
                Context.sleep_mode != "skip",
            )
        )
        return result.scalar() or 0
    raise ValueError(f"unknown guard_kind: {guard_kind!r}")


def _effective_for_guard(plan_tier, guard_kind: str, requested_bonus: int) -> int:
    """New effective limit for an LD-7 guard, mirroring ``_zero_floor``.

    A tier with base ``0`` always yields ``0`` regardless of the addon —
    matches the runtime ``Workspace.effective_*`` properties so admins
    cannot bypass the tier gate via a manual grant (#569 defense).
    """
    base_map = {
        "memory": plan_tier.memory_limit,
        "member": plan_tier.max_members_per_workspace,
        "context": plan_tier.max_contexts_per_workspace,
        "sleep_contexts": plan_tier.sleep_enabled_contexts_limit,
    }
    base = base_map[guard_kind]
    if base == 0:
        return 0
    return base + requested_bonus


@router.put("/workspaces/{workspace_id}/quotas")
async def update_workspace_quotas(
    workspace_id: str,
    request: UpdateAddonRequest,
    admin_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Update workspace addon bonuses via WorkspaceAddon row UPSERT (#665).

    Pre-#665 this handler wrote ``Workspace.addon_*_bonus`` cache columns
    directly, bypassing the #570 contract. Once Stripe addon webhooks
    land, the cache recalc would silently overwrite admin grants. This
    handler now UPSERTs ``WorkspaceAddon`` rows with
    ``source='admin_grant'`` for each touched addon type, then calls
    ``AddonCalculatorService.recalculate_workspace_bonuses`` so the
    cache is regenerated from the union of Stripe rows + admin grants.

    Per LD-3: no ``db_transaction`` wrapper. The recalc service performs
    its own atomic commit that flushes the staged UPSERTs together. The
    validation pass below raises BEFORE any mutation, so there is no
    rollback target between mutation and recalc — ``db_transaction`` was
    only an error-boundary rollback, never an outer commit boundary
    (``utils/db_helpers.py:45-82``).

    Per LD-4 (revised by review-fix #2): all 9 request fields are
    ``int | None = None`` with no-touch semantics — omit a field to leave
    its admin grant untouched; send explicit ``0`` to zero it out. The
    pre-#665 legacy 5-field admin UI continues to work because it always
    sends all 5 values explicitly. The unified optional shape closes the
    partial-update footgun where omitted-default 0 silently DELETEd
    existing admin_grant rows.

    Per LD-2: absolute-value semantics. For each touched field, compute
    ``non_admin_total = SUM(active WorkspaceAddon rows WHERE source !=
    'admin_grant') * unit_value`` and reject (HTTP 400) when the request
    would require a negative admin grant — admin reductions cannot
    silently fall below the Stripe-purchased floor. The admin sees the
    conflict instead of getting an unexpected effective value.

    Per LD-7: persistent-addon overflow guard (member / context / memory
    / sleep_contexts). Reject (HTTP 400) when the new effective limit
    falls below current usage. Daily-reset quotas (mcp / rest / public /
    analysis) are intentionally NOT guarded — admin throttling mid-day
    is acceptable since the counter resets at next reset boundary. The
    ``addon_storage_bonus_mb`` guard is also deferred: per-workspace
    storage usage tracking does not yet exist in the codebase. Filed as
    a follow-up to add the storage guard once usage tracking lands.

    Per LD-9: ``extra_sleep_contexts`` PRO-tier check is intentionally
    NOT enforced here. ``_zero_floor`` clamps the effective limit to 0
    for tiers with base ``sleep_enabled_contexts_limit == 0`` (FREE,
    BASIC), so an admin grant on those tiers is harmless to user-facing
    behavior; the admin UI (#663) should display "no effect on this tier"
    rather than rejecting the request from this handler.
    """
    ws_uuid = UUID(workspace_id)
    now = utcnow()

    # 1. Fetch + soft-delete-safe workspace lookup (#687 / #681 pattern).
    result = await db.execute(
        select(Workspace).where(
            Workspace.id == ws_uuid,
            Workspace.deleted_at.is_(None),
        )
    )
    workspace = result.scalar_one_or_none()
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")

    plan_tier = get_plan_tier(workspace.plan_name)

    # 2. Validation pass — no DB writes. Build the mutation plan; raise
    #    400 on any conflict so the caller sees the first reason cleanly
    #    rather than discovering it mid-mutation.
    grants_to_apply: list[_GrantPlan] = []
    audit_changes: dict[str, str] = {}

    for spec in _ADDON_FIELD_SPECS:
        requested = getattr(request, spec.field_name)
        if requested is None:
            continue  # no-touch (LD-4 revised by review-fix #2: any of the 9 fields can be None)

        # field_name doubles as the Workspace cache-column name by convention.
        old_bonus = getattr(workspace, spec.field_name)

        # 2a. Divisibility — addons are sold in fixed-unit increments
        #     (see ``ADDON_UNIT_VALUES``). Reject early with a clear
        #     "value must be a multiple of N" error before the floor
        #     and overflow queries run. Stripe rows are integer-quantity
        #     by schema, so this check on ``requested`` implies the same
        #     check on the derived admin-portion below.
        if requested % spec.unit_value != 0:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{spec.field_name} value {requested} must be a multiple of "
                    f"{spec.unit_value} (addon type {spec.addon_type!r})"
                ),
            )

        # 2b. Compute Stripe-side floor for this addon_type. Predicate
        #     mirrors ``recalculate_workspace_bonuses`` (active window
        #     check at addon_calculator_service.py:99-104) so floor and
        #     cache cannot diverge.
        sum_result = await db.execute(
            select(func.coalesce(func.sum(WorkspaceAddon.quantity), 0)).where(
                WorkspaceAddon.workspace_id == ws_uuid,
                WorkspaceAddon.addon_type == spec.addon_type,
                WorkspaceAddon.source != "admin_grant",
                WorkspaceAddon.active_from <= now,
                ((WorkspaceAddon.active_until.is_(None)) | (WorkspaceAddon.active_until > now)),
            )
        )
        non_admin_quantity = int(sum_result.scalar() or 0)
        non_admin_total = non_admin_quantity * spec.unit_value

        # 2c. Reject silent clamp (LD-2). Admins must see when their
        #     requested value would fall below the Stripe-purchased floor.
        if requested < non_admin_total:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Cannot reduce {spec.field_name} below the Stripe-purchased floor "
                    f"of {non_admin_total} (active subscription provides "
                    f"{non_admin_quantity} unit(s) x {spec.unit_value}). "
                    f"Cancel or wait for the subscription to expire before reducing."
                ),
            )

        # 2d. Persistent-addon overflow guard (LD-7). Fires whenever the
        #     addon has a usage counter (member/context/memory/sleep_contexts),
        #     regardless of whether ``requested`` matches the cached
        #     ``old_bonus``. Review-finding #6: keying the skip on cache
        #     equality let upstream cache drift (e.g. operator SQL leaving
        #     the cache stale relative to actual usage) slip through a
        #     re-PUT of the cached value. The cost is one extra COUNT per
        #     touched persistent-addon field — admin-only path, acceptable.
        if spec.guard_kind is not None:
            usage_count = await _count_addon_usage(db, ws_uuid, spec.guard_kind)
            new_effective = _effective_for_guard(plan_tier, spec.guard_kind, requested)
            if usage_count > new_effective:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Cannot reduce {spec.field_name}: current "
                        f"{spec.guard_kind} count ({usage_count}) exceeds new "
                        f"effective limit ({new_effective})"
                    ),
                )

        # 2e. Compute admin_grant quantity. ``check_quantity_positive``
        #     forbids storing ``quantity == 0`` — the mutation pass DELETEs
        #     the row when this is 0. Divisibility is guaranteed by 2a +
        #     integer-quantity Stripe rows.
        admin_grant_quantity = (requested - non_admin_total) // spec.unit_value

        grants_to_apply.append(
            _GrantPlan(addon_type=spec.addon_type, quantity=admin_grant_quantity)
        )
        if old_bonus != requested:
            audit_changes[spec.field_name] = f"{old_bonus} -> {requested}"

    # 3. Mutation pass. Use ``INSERT ... ON CONFLICT DO UPDATE`` so
    #    concurrent admin PUTs against the same (workspace, addon_type)
    #    are race-free at the DB layer — the composite UNIQUE
    #    ``uq_workspace_addons_workspace_addon_source`` is the conflict
    #    target. A naive SELECT-then-INSERT pattern could race two
    #    admins past the SELECT and crash one of them on the UNIQUE
    #    constraint (HTTP 500 via UniqueViolation). The UPSERT statement
    #    has well-defined "first-write wins on insert, last-write wins
    #    on update" semantics, mirroring the user-visible single-admin
    #    PUT API contract.
    for plan in grants_to_apply:
        if plan.quantity == 0:
            # check_quantity_positive forbids quantity=0 storage; DELETE
            # the admin grant row instead. Single-statement is race-safe
            # (no-op when the row doesn't exist).
            await db.execute(
                sql_delete(WorkspaceAddon).where(
                    WorkspaceAddon.workspace_id == ws_uuid,
                    WorkspaceAddon.addon_type == plan.addon_type,
                    WorkspaceAddon.source == "admin_grant",
                )
            )
            continue

        stmt = (
            pg_insert(WorkspaceAddon)
            .values(
                workspace_id=ws_uuid,
                addon_type=plan.addon_type,
                source="admin_grant",
                quantity=plan.quantity,
                purchase_price_cents=None,
                stripe_product_id=None,
                active_from=now,
                active_until=None,
                created_by=admin_user["user_id"],
            )
            .on_conflict_do_update(
                constraint="uq_workspace_addons_workspace_addon_source",
                set_={
                    "quantity": plan.quantity,
                    # ``active_from``, ``active_until``, and ``created_by``
                    # are intentionally NOT updated on conflict — the audit
                    # trail records the original grant (when it first applied
                    # AND who first applied it). Subsequent re-grants only
                    # adjust the quantity; the structured log entry
                    # ``workspace_addon_updated`` captures the changing admin
                    # identity per-PUT. Review-finding #5 + #7: an unset
                    # ``set_`` for ``active_until`` previously clobbered any
                    # pre-existing expiration to NULL, silently extending
                    # time-bound grants; clobbering ``created_by`` made the
                    # audit trail half-preserved (timestamp from T1, attribution
                    # from T_last). Preserving all three closes both gaps.
                },
            )
        )
        await db.execute(stmt)

    # 4. Recalculate (skipped when validation produced no mutations).
    #    Commits the staged ``WorkspaceAddon`` UPSERTs and the refreshed
    #    ``addon_*_bonus`` cache columns atomically per the #570 contract
    #    docstring on ``AddonCalculatorService`` — also why this handler
    #    has no ``db_transaction(...)`` wrapper (LD-3, see function docstring).
    #
    # Review-finding #14: skip the recalc when ``grants_to_apply`` is empty
    # (e.g. an all-no-touch PUT) so we don't commit a no-op transaction and
    # emit a misleading ``addon_bonuses_recalculated`` log entry.
    if grants_to_apply:
        await AddonCalculatorService(db).recalculate_workspace_bonuses(ws_uuid)

    logger.info(
        "workspace_addon_updated",
        workspace_id=workspace_id,
        admin_user=admin_user["user_id"],
        changes=audit_changes,
    )

    return {"message": f"Quota addons updated for workspace {workspace.name}"}


def _reject_above_tier(
    field_name: str,
    requested: float | None,
    tier_default: float | None,
) -> None:
    """Raise HTTP 400 if ``requested`` exceeds the tier default (Issue #709).

    ``None`` on ``requested`` (clear override) is always allowed; ``None``
    on ``tier_default`` (uncapped tier) makes the override unbounded by
    design. Shared by both daily and monthly cap fields in
    ``update_workspace_spend_cap`` so the error wording stays identical.
    """
    if requested is None or tier_default is None:
        return
    if requested > tier_default:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{field_name} ({requested}) exceeds tier default ({tier_default}) — "
                "upgrade the plan or adjust the tier env override to lift this ceiling"
            ),
        )


@router.put("/workspaces/{workspace_id}/spend-cap")
async def update_workspace_spend_cap(
    workspace_id: str,
    request: UpdateSpendCapRequest,
    admin_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Update the per-workspace embedding spend cap override (Issue #709).

    Setting a field to ``None`` removes the override and falls back to the
    tier default. Setting a numeric value (>= 0) overrides the tier default
    for THIS workspace only. The handler rejects override values above the
    workspace's current tier default — admins lift the cap by upgrading the
    plan tier (or via ``plan_*_embedding_*_cap_usd`` env vars), not by
    raising individual workspaces past their tier ceiling.

    Concrete values are written through the SQLAlchemy ORM; the underlying
    ``CHECK (embedding_*_cap_usd >= 0)`` constraint in migration
    ``e16_709`` is the database-level backstop should the route validation
    ever be bypassed.
    """
    ws_uuid = UUID(workspace_id)

    async with db_transaction(db, "update_workspace_spend_cap", "Failed to update spend cap"):
        result = await db.execute(
            select(Workspace).where(
                Workspace.id == ws_uuid,
                Workspace.deleted_at.is_(None),  # #687 / #681 pattern: soft-delete safe
            )
        )
        workspace = result.scalar_one_or_none()
        if not workspace:
            raise HTTPException(status_code=404, detail="Workspace not found")

        plan_tier = get_plan_tier(workspace.plan_name)

        # Tier-bounded edit: refuse to set an override above the tier default.
        # ``None`` (clearing the override) is always allowed.
        _reject_above_tier(
            "embedding_daily_cap_usd",
            request.embedding_daily_cap_usd,
            plan_tier.embedding_daily_cap_usd,
        )
        _reject_above_tier(
            "embedding_monthly_cap_usd",
            request.embedding_monthly_cap_usd,
            plan_tier.embedding_monthly_cap_usd,
        )

        old_daily = workspace.embedding_daily_cap_usd
        old_monthly = workspace.embedding_monthly_cap_usd

        workspace.embedding_daily_cap_usd = (
            Decimal(str(request.embedding_daily_cap_usd))
            if request.embedding_daily_cap_usd is not None
            else None
        )
        workspace.embedding_monthly_cap_usd = (
            Decimal(str(request.embedding_monthly_cap_usd))
            if request.embedding_monthly_cap_usd is not None
            else None
        )

        await db.commit()

        logger.info(
            "workspace_spend_cap_updated",
            workspace_id=workspace_id,
            admin_user=admin_user["user_id"],
            changes={
                "embedding_daily_cap_usd": (f"{old_daily} -> {request.embedding_daily_cap_usd}"),
                "embedding_monthly_cap_usd": (
                    f"{old_monthly} -> {request.embedding_monthly_cap_usd}"
                ),
            },
        )

        return {"message": f"Spend cap updated for workspace {workspace.name}"}
