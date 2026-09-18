"""Workspace Plan Management API Routes.

Issue #164: User Management拡張 + Plan Management改善
Issue #252: Session-only authentication (no API keys)

Owner-facing read views of a workspace's plan.

Self-service plan *changes* were removed in #1096: the in-process Stripe billing
path is retired and the backend is Stripe-agnostic. Plan changes happen via the
billing handoff (#1093) / internal entitlement endpoint (#954), or
administratively via /admin/plans. This module is read-only.

Endpoints:
- GET /api/v1/workspaces/{workspace_id}/plan - Get workspace plan info
- GET /api/v1/workspaces/plans/available - List available plans
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import SessionUser
from config.plan_tiers import PLAN_ORDER, PLAN_TIERS, PlanTier, get_plan_tier, plan_rank
from db.base import get_db
from models.auth import (
    Context,
    Workspace,
    WorkspaceMember,
)
from models.memory import Memory
from services.permission_service import PermissionService
from utils.logger import get_logger
from utils.plan_resolver import tier_owned_workspace_cap

logger = get_logger(__name__)

router = APIRouter(tags=["workspace-plan"])


# ============================================================================
# Request/Response Models
# ============================================================================


class WorkspacePlanQuotas(BaseModel):
    """Effective quotas of one workspace, as ``GET /workspaces/{id}/plan`` returns them.

    Typed rather than a bare ``dict`` because the frontend reads named keys off
    this payload: the token screens take "used / max" and the quota-capacity
    line from ``max_resource_tokens`` / ``max_quota_capacity`` (#1560). A rename
    here must fail pyright at the construction site, not silently degrade the UI
    to its "cap unknown" fallback.
    """

    memory_limit: int
    max_contexts: int
    # Issue #242: SERVE caps for resource tokens. They stay positive on M/L for
    # tokens that already exist there even though creating a new one is XL-only
    # (#1551) — the create gate is the tier matrix's ``resources`` boolean.
    max_resource_tokens: int
    max_quota_capacity: int  # events/hour across all active tokens
    mcp_calls_per_day: int
    mcp_calls_per_week: int
    rest_calls_per_day: int
    public_calls_per_day: int


class WorkspacePlanInfo(BaseModel):
    """Workspace plan information with usage stats."""

    workspace_id: str
    workspace_name: str
    current_plan: str
    plan_display_name: str
    price_monthly: int
    usage: dict  # Current usage (memories, storage, contexts)
    quotas: WorkspacePlanQuotas
    can_upgrade: bool
    can_downgrade: bool


class AvailablePlanInfo(BaseModel):
    """Available plan tier information."""

    name: str
    display_name: str
    price_monthly: int
    quotas: dict
    features: list[str]


class PlanTierFeature(BaseModel):
    """One tier's curated feature/limit values for the comparison matrix (#1138).

    Single source of truth = ``config/plan_tiers.py`` (env-overridable). Price is
    intentionally omitted — pricing lives in www / the payment service; this
    surface is feature *limits* only, so the OSS Plan page never fabricates an
    amount (consistent with #1141 / #1096). Numeric fields use ``0`` for "not
    available on this tier"; the frontend renders that as ✗.
    """

    name: str
    display_name: str
    # Numeric limits (0 == not available on this tier)
    max_contexts: int
    max_members: int
    # #1550: owned-workspace cap a user whose highest-tier workspace is on
    # this tier gets with zero slot bonus (1 base + owned_workspace_grant).
    owned_workspaces: int
    memory_limit: int
    memories_per_day: int  # Issue #1549: memories created per UTC day
    storage_limit_bytes: int
    mcp_calls_per_day: int
    rest_calls_per_day: int
    public_calls_per_day: int
    max_resource_tokens: int
    max_connectors: int
    analysis_runs_per_day: int
    sleep_enabled_contexts_limit: int
    # Boolean capabilities
    reranking: bool
    managed_embeddings: bool
    # Issue #1569: Memory Analysis may run on the deployment's managed LLM
    # lane (no workspace key). Only takes effect where MANAGED_LLM_* is set.
    managed_llm: bool
    secret_store: bool
    shared_contexts: bool
    team_invitations: bool
    # Issue #1551: XL-only "may create" gates. When false, the matching
    # numeric rows above read 0 in this (creation) view.
    resources: bool
    connectors: bool
    public_contexts: bool


# ============================================================================
# Endpoints
# ============================================================================


@router.get("/workspaces/{workspace_id}/plan", response_model=WorkspacePlanInfo)
async def get_workspace_plan(
    workspace_id: str,
    user: SessionUser,
    db: AsyncSession = Depends(get_db),
):
    """Get workspace plan information.

    Issue #246: Owner-only access (frontend shows in owner-only menu).

    Returns current plan, usage stats, quotas, and upgrade/downgrade options.
    """
    workspace_uuid = UUID(workspace_id)

    # Issue #246: Owner-only access
    perm_service = PermissionService(db)
    await perm_service.check_workspace_owner(user["user_id"], workspace_uuid)

    # Get workspace
    workspace_result = await db.execute(select(Workspace).where(Workspace.id == workspace_uuid))
    workspace = workspace_result.scalar_one_or_none()

    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")

    # Get plan tier info
    plan_tier = get_plan_tier(workspace.plan_name)

    # Get current usage stats
    # Count memories across all workspace members
    members_result = await db.execute(
        select(WorkspaceMember.user_id).where(WorkspaceMember.workspace_id == workspace_uuid)
    )
    member_user_ids = [row[0] for row in members_result.all()]

    memory_count = 0
    if member_user_ids:
        memory_count_result = await db.execute(
            select(func.count(Memory.id)).where(Memory.user_id.in_(member_user_ids))
        )
        memory_count = memory_count_result.scalar() or 0

    # Count contexts
    context_count_result = await db.execute(
        select(func.count(Context.id)).where(
            Context.workspace_id == workspace_uuid, Context.deleted_at.is_(None)
        )
    )
    context_count = context_count_result.scalar() or 0

    # Log for debugging
    logger.info(
        "get_workspace_plan",
        workspace_id=str(workspace_uuid),
        workspace_name=workspace.name,
        plan_name=workspace.plan_name,
        context_count=context_count,
        max_contexts=plan_tier.max_contexts_per_workspace,
        memory_count=memory_count,
    )

    # Determine upgrade/downgrade options
    current_index = plan_rank(workspace.plan_name)

    return WorkspacePlanInfo(
        workspace_id=str(workspace.id),
        workspace_name=workspace.name,
        current_plan=workspace.plan_name,
        plan_display_name=plan_tier.display_name,
        price_monthly=plan_tier.price_monthly,
        usage={
            "memories": memory_count,
            "contexts": context_count,
        },
        quotas=WorkspacePlanQuotas(
            memory_limit=workspace.effective_memory_limit,
            max_contexts=workspace.effective_max_contexts,
            max_resource_tokens=plan_tier.max_resource_tokens,  # Issue #242
            max_quota_capacity=plan_tier.max_resource_tokens * 10000,  # Issue #242: events/hour
            mcp_calls_per_day=workspace.effective_mcp_calls_per_day,
            mcp_calls_per_week=workspace.effective_mcp_calls_per_week,
            rest_calls_per_day=workspace.effective_rest_calls_per_day,
            public_calls_per_day=workspace.effective_public_calls_per_day,
        ),
        can_upgrade=current_index < len(PLAN_ORDER) - 1,
        can_downgrade=current_index > 0,
    )


@router.get("/workspaces/plans/available", response_model=list[AvailablePlanInfo])
async def get_available_plans(
    user: SessionUser,
):
    """Get available plan tiers.

    Public information, accessible by any authenticated user.
    """
    # Feature display name mapping
    feature_display_names = {
        "api_keys": "API Keys",
        "reranking": "Search Reranking",
        "oauth": "OAuth Applications",
        "team_invitations": "Team Invitations",
        "shared_contexts": "Shared Contexts",
    }

    return [
        AvailablePlanInfo(
            name=tier.name,
            display_name=tier.display_name,
            price_monthly=tier.price_monthly,
            quotas={
                "memory_limit": tier.memory_limit,
                "max_contexts": tier.max_contexts_per_workspace,
                "mcp_calls_per_day": tier.mcp_calls_per_day,
                "mcp_calls_per_week": tier.mcp_calls_per_week,
                "rest_calls_per_day": tier.rest_calls_per_day,
                "public_calls_per_day": tier.public_calls_per_day,
            },
            features=[feature_display_names.get(f, f) for f in sorted(tier.features)],
        )
        for tier in PLAN_TIERS.values()
    ]


def _plan_tier_feature(tier: PlanTier) -> PlanTierFeature:
    """Project a ``PlanTier`` onto the curated comparison-matrix shape (#1138).

    Issue #1551: the matrix is the *creation* view. Resource tokens, connectors
    and public calls are XL-only to create, so on tiers without the feature
    those numeric rows read 0 (✗) here — WITHOUT touching the tier dataclass,
    whose positive M/L caps still serve objects that already exist.
    """
    has_resources = "resources" in tier.features
    has_connectors = "connectors" in tier.features
    has_public = "public_contexts" in tier.features
    return PlanTierFeature(
        name=tier.name,
        display_name=tier.display_name,
        max_contexts=tier.max_contexts_per_workspace,
        max_members=tier.max_members_per_workspace,
        owned_workspaces=tier_owned_workspace_cap(tier),
        memory_limit=tier.memory_limit,
        memories_per_day=tier.memories_per_day,
        storage_limit_bytes=tier.storage_limit_bytes,
        mcp_calls_per_day=tier.mcp_calls_per_day,
        rest_calls_per_day=tier.rest_calls_per_day,
        public_calls_per_day=tier.public_calls_per_day if has_public else 0,
        max_resource_tokens=tier.max_resource_tokens if has_resources else 0,
        max_connectors=tier.max_connectors if has_connectors else 0,
        analysis_runs_per_day=tier.analysis_runs_per_day,
        sleep_enabled_contexts_limit=tier.sleep_enabled_contexts_limit,
        reranking="reranking" in tier.features,
        managed_embeddings="managed_embeddings" in tier.features,
        managed_llm="managed_llm" in tier.features,
        secret_store="secret_store" in tier.features,
        shared_contexts=tier.allows_shared_contexts,
        team_invitations="team_invitations" in tier.features,
        resources=has_resources,
        connectors=has_connectors,
        public_contexts=has_public,
    )


@router.get("/workspaces/plans/tiers", response_model=list[PlanTierFeature])
async def get_plan_tier_matrix(
    user: SessionUser,
) -> list[PlanTierFeature]:
    """Curated per-tier feature matrix for the Plan page comparison (#1138).

    Single source of truth = ``config/plan_tiers.py`` (env-overridable). Price is
    omitted by design — pricing lives in www / the payment service. Returns the
    tiers in upgrade order (free → basic → pro). Public reference data; accessible
    by any authenticated session user (the Plan page itself is owner-gated).

    Path note: the ``/workspaces/plans/...`` two-segment shape (mirroring
    ``/workspaces/plans/available``) is deliberate — a one-segment
    ``/workspaces/plan-tiers`` would be shadowed by the earlier-registered
    ``GET /workspaces/{workspace_id}`` route and 422 on the UUID parse.
    """
    # Iterate PLAN_TIERS directly (insertion order = PLAN_ORDER, lowest first) so a
    # custom/added tier is never silently dropped — mirrors get_available_plans.
    return [_plan_tier_feature(tier) for tier in PLAN_TIERS.values()]
