"""Dashboard quota meters built from the write gates' effective limits."""

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.routes.usage import CurrentUsage, PlanLimits, QuotaUsage
from auth.analysis_gates import _get_user_timezone
from auth.resource_tokens import workspace_regular_active_tokens
from config.plan_tiers import get_plan_tier
from config.settings import get_settings
from models.auth import Context, Workspace, WorkspaceInvitation, WorkspaceMember
from models.config import ContextSearchConfig
from models.resource import ResourceToken, WorkspaceConnector
from services.analysis.query_service import get_today_analysis_count
from services.content_chunk_policy import content_chunk_context_limit, content_chunks_gated
from services.llm_selector_policy import llm_selector_context_limit, llm_selector_gated
from services.permission_service import PermissionService
from services.plan_seats import live_contexts
from services.storage_quota_service import get_current_storage_usage
from utils.datetime import utcnow
from utils.exceptions import AuthorizationError, FeatureNotAvailableError


async def build_workspace_quota_usage(
    db: AsyncSession,
    workspace: Workspace,
    user_id: str,
    usage: CurrentUsage,
    plan: PlanLimits,
) -> list[QuotaUsage]:
    """Return aggregate counts only, scoped to the already-authenticated workspace.

    Pending invitations occupy member slots at invite time. Regular resource
    tokens use the mint gate's canonical population, excluding connector tokens.
    Per-key limits have no workspace-wide numerator.
    """
    workspace_id = workspace.id
    tier = get_plan_tier(workspace.plan_name)
    contexts = select(func.count(Context.id)).where(live_contexts(workspace_id)).scalar_subquery()
    members = (
        select(func.count(WorkspaceMember.id))
        .where(WorkspaceMember.workspace_id == workspace_id)
        .scalar_subquery()
    )
    pending = (
        select(func.count(WorkspaceInvitation.id))
        .where(
            WorkspaceInvitation.workspace_id == workspace_id,
            WorkspaceInvitation.accepted_at.is_(None),
            or_(
                WorkspaceInvitation.expires_at.is_(None), WorkspaceInvitation.expires_at > utcnow()
            ),
        )
        .scalar_subquery()
    )
    connectors = (
        select(func.count(WorkspaceConnector.id))
        .where(WorkspaceConnector.workspace_id == workspace_id)
        .scalar_subquery()
    )

    def configured_contexts(flag):
        return (
            select(func.count(ContextSearchConfig.context_id))
            .join(Context, Context.id == ContextSearchConfig.context_id)
            .where(live_contexts(workspace_id), flag.is_(True))
            .scalar_subquery()
        )

    counts = (
        await db.execute(
            select(
                contexts.label("contexts"),
                (members + pending).label("members"),
                connectors.label("connectors"),
                configured_contexts(ContextSearchConfig.content_chunks_enabled).label(
                    "content_chunks"
                ),
                configured_contexts(ContextSearchConfig.llm_selector_enabled).label("llm_selector"),
            )
        )
    ).one()
    tokens = (
        await db.execute(
            workspace_regular_active_tokens(
                workspace_id,
                func.count(ResourceToken.id).label("token_count"),
                func.coalesce(func.sum(ResourceToken.quota_events_per_hour), 0).label("capacity"),
            )
        )
    ).one()
    storage = await get_current_storage_usage(workspace_id, db)
    timezone = await _get_user_timezone(db, user_id)
    analyses = await get_today_analysis_count(db, workspace_id=workspace_id, user_timezone=timezone)

    meters = [
        QuotaUsage(
            id="memories",
            used=usage.memory_count,
            limit=plan.memory_limit,
            bonus=max(0, plan.memory_limit - tier.memory_limit),
        ),
        QuotaUsage(
            id="memories_daily",
            used=usage.memories_created_today,
            limit=plan.memories_per_day,
            bonus=max(0, plan.memories_per_day - tier.memories_per_day),
            period="day",
        ),
        QuotaUsage(
            id="storage",
            used=storage,
            limit=workspace.effective_storage_limit_bytes
            if workspace.effective_storage_limit_bytes > 0
            else None,
            bonus=max(0, workspace.effective_storage_limit_bytes - tier.storage_limit_bytes),
            unit="bytes",
        ),
        QuotaUsage(
            id="contexts",
            used=counts.contexts,
            limit=workspace.effective_max_contexts,
            bonus=max(0, workspace.effective_max_contexts - tier.max_contexts_per_workspace),
        ),
        QuotaUsage(
            id="members",
            used=counts.members,
            limit=workspace.effective_max_members,
            bonus=max(0, workspace.effective_max_members - tier.max_members_per_workspace),
        ),
        QuotaUsage(
            id="connectors",
            used=counts.connectors,
            limit=workspace.effective_max_connectors,
            bonus=max(0, workspace.effective_max_connectors - tier.max_connectors),
        ),
        QuotaUsage(
            id="resource_tokens",
            used=tokens.token_count,
            limit=workspace.effective_max_resource_tokens,
        ),
        QuotaUsage(
            id="resource_capacity",
            used=tokens.capacity,
            limit=workspace.effective_max_resource_tokens * 10000,
            period="hour",
        ),
        QuotaUsage(
            id="analysis",
            used=analyses,
            limit=workspace.effective_analysis_runs_per_day,
            bonus=max(0, workspace.effective_analysis_runs_per_day - tier.analysis_runs_per_day),
            period="day",
        ),
        QuotaUsage(
            id="content_chunks",
            used=counts.content_chunks,
            limit=content_chunk_context_limit(workspace.plan_name)
            if content_chunks_gated()
            else None,
        ),
        QuotaUsage(
            id="llm_selector",
            used=counts.llm_selector,
            limit=llm_selector_context_limit(workspace.plan_name) if llm_selector_gated() else None,
        ),
    ]
    for mode in ("mcp", "rest", "public"):
        for period, usage_suffix, plan_suffix in (
            ("day", "today", "day"),
            ("week", "this_week", "week"),
        ):
            limit = getattr(plan, f"{mode}_calls_per_{plan_suffix}")
            base = getattr(tier, f"{mode}_calls_per_{plan_suffix}")
            meters.append(
                QuotaUsage(
                    id=f"{mode}_{period}",
                    used=getattr(usage, f"{mode}_calls_{usage_suffix}"),
                    limit=limit,
                    bonus=max(0, limit - base),
                    period=period,
                )
            )
    if usage.sleep_contexts is not None:
        sleep = usage.sleep_contexts
        meters.append(
            QuotaUsage(id="sleep", used=sleep.used, limit=sleep.limit, bonus=sleep.addon_bonus)
        )
    meters.append(
        QuotaUsage(
            id="workspaces",
            used=usage.workspaces.used,
            limit=usage.workspaces.limit,
            scope="account",
        )
    )
    meters.append(
        QuotaUsage(
            id="bound_key_rate",
            used=None,
            limit=tier.bound_public_calls_per_minute,
            period="minute",
            scope="api_key",
        )
    )
    settings = get_settings()
    if settings.enable_byok and settings.enable_cost_display:
        # Match the cost API: money aggregates include private-context activity.
        # Ordinary workspace members still receive the non-monetary quota meters,
        # and so does an admin whose seat the plan suspended (FEAT-001): the
        # cost API refuses that admin too, but this endpoint does not.
        try:
            await PermissionService(db).check_workspace_admin(user_id, workspace_id)
        except (AuthorizationError, FeatureNotAvailableError):
            return meters

        from services.embedding_spend_cap_service import EmbeddingSpendCapService

        daily, monthly = await EmbeddingSpendCapService(db).get_current_spend(workspace_id)
        for period, spent, cap in (
            ("day", daily, workspace.effective_embedding_daily_cap_usd),
            ("month", monthly, workspace.effective_embedding_monthly_cap_usd),
        ):
            meters.append(
                QuotaUsage(
                    id=f"embedding_{period}",
                    used=float(spent),
                    limit=float(cap) if cap is not None else None,
                    unit="usd",
                    period=period,
                )
            )
    return meters
