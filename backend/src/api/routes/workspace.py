"""Workspace API Routes.

Issue #115 - Workspace-level Multi-tenancy Support

Provides workspace-level statistics and management endpoints.
Currently implements aggregated stats across all user's contexts.

Performance Note:
    Uses single JOIN + GROUP BY query to avoid N+1 problem.
    Query count: 2 (contexts + aggregated stats) regardless of context count.
"""

import logging
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.routes.usage import (
    CurrentUsage,
    DailyUsage,
    EndpointUsage,
    PlanLimits,
    UsageBreakdownResponse,
    UsageCurrentResponse,
    UsageHistoryResponse,
    _build_sleep_contexts_usage,
    _build_workspaces_usage,
    calculate_usage_status,
)
from auth.dependencies import get_user_from_api_key_or_session
from db.base import get_db
from models.auth import Context, User, Workspace, WorkspaceMember
from models.auth import UsageStats as UsageStatsModel
from models.memory import Memory
from utils.datetime import to_utc_iso, utcnow
from utils.exceptions import AuthenticationError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/workspace", tags=["workspace"])


# ============================================================================
# Response Models
# ============================================================================


class PrivateContextAggregation(BaseModel):
    """Aggregated statistics for inaccessible private contexts.

    Single Collection Migration: storage_mb removed (memory count only).
    """

    context_count: int
    memory_count: int


class ContextStats(BaseModel):
    """Statistics for a single context.

    Single Collection Migration: storage_mb removed (memory count only).
    """

    context_id: str
    context_name: str
    created_by: str | None
    created_by_name: str | None
    memory_count: int
    is_private: bool = False  # Issue #165: Privacy flag for UI display
    # #1496: saved but not searchable — a failed embedding never reaches
    # Qdrant, and BM25 lives there too, so recall misses these in both modes.
    # `stalled` is the subset nothing will retry on its own.
    unsearchable_count: int = 0
    stalled_count: int = 0


class WorkspaceStatsResponse(BaseModel):
    """Aggregated statistics across all user's contexts.

    Issue #165: Privacy-aware response with aggregation for inaccessible contexts.
    Single Collection Migration: total_storage_mb removed (memory count only).
    """

    total_memories: int  # ALL contexts (for rate limit purposes)
    context_count: int  # ALL contexts
    contexts: list[ContextStats]  # Only accessible contexts
    private_aggregation: PrivateContextAggregation | None = None  # Inaccessible private contexts
    plan_name: str  # Issue #149: Plan tier display
    # #1496: totals across the contexts this caller can see. Additive with
    # defaults so an older frontend is unaffected.
    unsearchable_memories: int = 0
    stalled_memories: int = 0


# ============================================================================
# Endpoints
# ============================================================================


@router.get("/stats", response_model=WorkspaceStatsResponse)
async def get_workspace_stats(
    user: dict = Depends(get_user_from_api_key_or_session),
    db: AsyncSession = Depends(get_db),
) -> WorkspaceStatsResponse:
    """Get aggregated statistics across all user's contexts.

    Returns total memory count and per-context breakdown.

    Args:
        user: Authenticated user from session or API key
        db: Database session

    Returns:
        WorkspaceStatsResponse with aggregated stats

    Example:
        GET /api/v1/workspace/stats
        Response: {
            "total_memories": 150,
            "context_count": 3,
            "contexts": [
                {"context_id": "...", "context_name": "default", "memory_count": 100},
                ...
            ],
            "plan_name": "pro"
        }
    """
    user_id = user.get("user_id")
    if not user_id:
        raise AuthenticationError("User ID not found in session")

    try:
        # Issue #65: Single JOIN replaces 2 sequential queries
        user_workspace_result = await db.execute(
            select(User, Workspace)
            .join(Workspace, User.current_workspace_id == Workspace.id)
            .where(User.user_id == user_id)
        )
        row = user_workspace_result.one_or_none()

        if not row:
            return WorkspaceStatsResponse(
                total_memories=0,
                context_count=0,
                contexts=[],
                plan_name="free",
            )

        db_user, workspace = row

        contexts_result = await db.execute(
            select(Context)
            .where(
                Context.workspace_id == db_user.current_workspace_id, Context.deleted_at.is_(None)
            )
            .order_by(Context.created_at)
        )
        contexts_list = contexts_result.scalars().all()

        if not contexts_list:
            return WorkspaceStatsResponse(
                total_memories=0,
                context_count=0,
                contexts=[],
                plan_name=workspace.plan_name,
            )

        # Issue #165: Check if user is workspace owner
        is_workspace_owner = workspace.owner_user_id == user_id

        # Query 2: Get memory stats for all contexts (privacy-aware)
        # Issue #204: Extracted to service layer for testability
        from services.workspace_service import WorkspaceService

        workspace_service = WorkspaceService(db)
        stats_by_collection = await workspace_service.get_collection_memory_stats(
            user_id=user_id,
            contexts=contexts_list,
            is_workspace_owner=is_workspace_owner,
        )
        # #1496: same privacy rules, by construction (shared _visibility_conditions).
        unsearchable_by_context = await workspace_service.get_unsearchable_memory_stats(
            user_id=user_id,
            contexts=contexts_list,
            is_workspace_owner=is_workspace_owner,
        )

        # Separate accessible and inaccessible contexts
        accessible_contexts: list[ContextStats] = []
        inaccessible_count = 0
        inaccessible_memories = 0
        total_memories = 0
        # #1496: accumulated over ACCESSIBLE contexts only — reporting a total
        # that included contexts the caller cannot see would be a side channel.
        total_unsearchable = 0
        total_stalled = 0

        # Critical Fix: Avoid N+1 query - batch fetch all context creators
        creator_ids = {ctx.created_by for ctx in contexts_list if ctx.created_by}
        if creator_ids:
            creators_result = await db.execute(select(User).where(User.user_id.in_(creator_ids)))
            creators_by_id = {u.user_id: u for u in creators_result.scalars()}
        else:
            creators_by_id = {}

        for context in contexts_list:
            # Single Collection Migration: Use context.id, memory count only
            memory_count, _ = stats_by_collection.get(str(context.id), (0, 0))
            unsearchable, stalled = unsearchable_by_context.get(str(context.id), (0, 0))

            # Privacy check (same pattern as /api/v1/contexts)
            is_accessible = (
                is_workspace_owner  # Owner sees everything
                or not context.is_private  # Shared contexts visible to all
                or context.created_by == user_id  # Creator sees own private contexts
            )

            if is_accessible:
                # Fetch creator's name (only for accessible contexts)
                # Use pre-fetched creators dict (no query needed)
                created_by_name = None
                if context.created_by:
                    creator = creators_by_id.get(context.created_by)
                    if creator:
                        created_by_name = creator.name or creator.email

                accessible_contexts.append(
                    ContextStats(
                        context_id=str(context.id),
                        context_name=context.display_name or context.name,
                        created_by=context.created_by,
                        created_by_name=created_by_name,
                        memory_count=memory_count,
                        is_private=context.is_private,
                        unsearchable_count=unsearchable,
                        stalled_count=stalled,
                    )
                )
                total_unsearchable += unsearchable
                total_stalled += stalled
            else:
                # Aggregate inaccessible private contexts
                inaccessible_count += 1
                inaccessible_memories += memory_count

            # Totals include ALL contexts (for rate limits)
            total_memories += memory_count

        # Build aggregation for inaccessible contexts
        private_aggregation = None
        if inaccessible_count > 0:
            private_aggregation = PrivateContextAggregation(
                context_count=inaccessible_count,
                memory_count=inaccessible_memories,
            )

        logger.info(
            "workspace_stats_retrieved",
            extra={
                "user_id": user_id,
                "total_memories": total_memories,
                "context_count": len(contexts_list),
                "accessible_count": len(accessible_contexts),
                "inaccessible_count": inaccessible_count,
            },
        )

        return WorkspaceStatsResponse(
            total_memories=total_memories,  # ALL contexts
            context_count=len(contexts_list),  # ALL contexts
            contexts=accessible_contexts,  # Only accessible
            private_aggregation=private_aggregation,  # Aggregated inaccessible
            plan_name=workspace.plan_name,  # Issue #149
            unsearchable_memories=total_unsearchable,  # #1496, accessible only
            stalled_memories=total_stalled,  # #1496
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(
            "workspace_stats_failed",
            extra={"user_id": user_id, "error_type": type(e).__name__},
        )
        raise HTTPException(
            status_code=500,
            detail="Failed to retrieve workspace statistics. Please try again later.",
        ) from e


# ============================================================================
# Workspace Usage Endpoints (Issue #146+)
# ============================================================================


@router.get("/usage/current", response_model=UsageCurrentResponse)
async def get_workspace_usage_current(
    user: dict = Depends(get_user_from_api_key_or_session),
    db: AsyncSession = Depends(get_db),
):
    """Get workspace-wide usage vs plan limits.

    Aggregates usage across all workspace members.

    Returns:
        Workspace usage statistics with plan limits and warning flags

    Raises:
        HTTPException: 400 if no workspace selected, 500 on error
    """
    from utils import db_transaction

    workspace_id = user.get("current_workspace_id")
    if not workspace_id:
        raise HTTPException(
            status_code=400,
            detail="No workspace selected. Please select an workspace first.",
        )

    async with db_transaction(db, "get_workspace_usage_current", "Failed to get workspace usage"):
        # Get workspace with plan limits
        workspace_result = await db.execute(select(Workspace).where(Workspace.id == workspace_id))
        workspace = workspace_result.scalar_one_or_none()

        if not workspace:
            raise HTTPException(status_code=404, detail="Workspace not found")

        # Calculate effective limits (base + addon bonuses) via shared service.
        # Issue #198: include weekly + per-tier values; the legacy "* 7"
        # heuristic disagreed with the rate limiter (PRO weekly: hardcoded
        # 385000 vs actual 275000) and the combined daily was misleading
        # because it lumped MCP + REST + Public into one number.
        from services.effective_quota_service import EffectiveQuotaService

        try:
            effective_quotas = await EffectiveQuotaService(db).get_effective_quotas(workspace_id)
        except ValueError:
            # Shouldn't happen — workspace was validated above. Fallback to
            # the model's own properties for the quota fields this endpoint
            # reads, without claiming full parity with the service contract
            # (max_members / max_contexts are intentionally omitted because
            # this route does not consume them).
            effective_quotas = {
                "memory_limit": workspace.effective_memory_limit,
                "mcp_calls_per_day": workspace.effective_mcp_calls_per_day,
                "mcp_calls_per_week": workspace.effective_mcp_calls_per_week,
                "rest_calls_per_day": workspace.effective_rest_calls_per_day,
                "rest_calls_per_week": workspace.effective_rest_calls_per_week,
                "public_calls_per_day": workspace.effective_public_calls_per_day,
                "public_calls_per_week": workspace.effective_public_calls_per_week,
            }

        effective_memory_limit = effective_quotas["memory_limit"]
        effective_mcp_daily = effective_quotas["mcp_calls_per_day"]
        effective_mcp_weekly = effective_quotas["mcp_calls_per_week"]
        effective_rest_daily = effective_quotas["rest_calls_per_day"]
        effective_rest_weekly = effective_quotas["rest_calls_per_week"]
        effective_public_daily = effective_quotas["public_calls_per_day"]
        effective_public_weekly = effective_quotas["public_calls_per_week"]
        # Combined sums kept for backward compatibility with the dashboard's
        # aggregate "API Calls" cards. These now reflect the real plan-tier
        # weekly caps, not daily * 7.
        effective_daily_api_limit = (
            effective_mcp_daily + effective_rest_daily + effective_public_daily
        )
        effective_weekly_api_limit = (
            effective_mcp_weekly + effective_rest_weekly + effective_public_weekly
        )

        # Issue #65: workspace_id scoping is sufficient — no need to fetch member_ids
        memory_count_result = await db.execute(
            select(func.count(Memory.id)).where(
                Memory.workspace_id == workspace_id,
                Memory.deleted_at.is_(None),
            )
        )
        memory_count = memory_count_result.scalar() or 0

        # Issue #65: Single conditional aggregation query replaces 8 sequential COUNTs
        today = utcnow().date()
        week_ago = today - timedelta(days=7)

        is_today = UsageStatsModel.date == today
        is_mcp = UsageStatsModel.endpoint.like("mcp:%")
        is_public = UsageStatsModel.endpoint.like("/api/v1/public/%")
        is_rest = and_(
            UsageStatsModel.endpoint.like("/api/v1/%"),
            UsageStatsModel.endpoint.notlike("/api/v1/public/%"),
        )

        usage_result = await db.execute(
            select(
                func.count(UsageStatsModel.id).filter(is_today).label("total_today"),
                func.count(UsageStatsModel.id).filter(and_(is_today, is_mcp)).label("mcp_today"),
                func.count(UsageStatsModel.id)
                .filter(and_(is_today, is_public))
                .label("public_today"),
                func.count(UsageStatsModel.id).filter(and_(is_today, is_rest)).label("rest_today"),
                func.count(UsageStatsModel.id).label("total_week"),
                func.count(UsageStatsModel.id).filter(is_mcp).label("mcp_week"),
                func.count(UsageStatsModel.id).filter(is_public).label("public_week"),
                func.count(UsageStatsModel.id).filter(is_rest).label("rest_week"),
            ).where(
                UsageStatsModel.workspace_id == workspace_id,
                UsageStatsModel.date >= week_ago,
            )
        )
        usage = usage_result.one()
        api_calls_today = usage.total_today
        mcp_calls_today = usage.mcp_today
        public_calls_today = usage.public_today
        rest_calls_today = usage.rest_today
        api_calls_week = usage.total_week
        mcp_calls_week = usage.mcp_week
        public_calls_week = usage.public_week
        rest_calls_week = usage.rest_week

        # Build response with aggregated data and effective limits (base + addons)
        return UsageCurrentResponse(
            plan=PlanLimits(
                plan_name=workspace.plan_name,
                memory_limit=effective_memory_limit,
                daily_total_limit=effective_daily_api_limit,
                weekly_total_limit=effective_weekly_api_limit,
                mcp_calls_per_day=effective_mcp_daily,
                mcp_calls_per_week=effective_mcp_weekly,
                rest_calls_per_day=effective_rest_daily,
                rest_calls_per_week=effective_rest_weekly,
                public_calls_per_day=effective_public_daily,
                public_calls_per_week=effective_public_weekly,
            ),
            usage=CurrentUsage(
                memory_count=memory_count,
                api_calls_today=api_calls_today,
                api_calls_this_week=api_calls_week,
                mcp_calls_today=mcp_calls_today,
                mcp_calls_this_week=mcp_calls_week,
                rest_calls_today=rest_calls_today,
                rest_calls_this_week=rest_calls_week,
                public_calls_today=public_calls_today,
                public_calls_this_week=public_calls_week,
                sleep_contexts=await _build_sleep_contexts_usage(
                    db, workspace_id, effective_quotas=effective_quotas
                ),
                # Issue #661: user-level owned-workspace cap. The /workspace/usage/current
                # endpoint is workspace-scoped overall but ``workspaces`` is required on
                # ``CurrentUsage`` (always populated, user-scoped). Use the caller's user_id.
                workspaces=await _build_workspaces_usage(db, user["user_id"]),
            ),
            memory_usage=calculate_usage_status(memory_count, effective_memory_limit),
            daily_api_usage=calculate_usage_status(api_calls_today, effective_daily_api_limit),
            weekly_api_usage=calculate_usage_status(api_calls_week, effective_weekly_api_limit),
        )


@router.get("/usage/history", response_model=UsageHistoryResponse)
async def get_workspace_usage_history(
    days: int = Query(7, ge=1, le=90),
    user: dict = Depends(get_user_from_api_key_or_session),
    db: AsyncSession = Depends(get_db),
):
    """Get workspace-wide historical usage data.

    Aggregates daily API call counts across all workspace members.

    Args:
        days: Number of days to retrieve (default: 7, max: 90)

    Returns:
        Historical usage data by day

    Raises:
        HTTPException: 400 if no workspace selected, 500 on error
    """
    from utils import db_transaction

    workspace_id = user.get("current_workspace_id")
    if not workspace_id:
        raise HTTPException(
            status_code=400,
            detail="No workspace selected. Please select an workspace first.",
        )

    async with db_transaction(
        db, "get_workspace_usage_history", "Failed to get workspace usage history"
    ):
        # Calculate date range
        end_date = utcnow().date()
        start_date = end_date - timedelta(days=days - 1)

        # Aggregate API calls by date across all members
        daily_stats_result = await db.execute(
            select(
                UsageStatsModel.date,
                func.count(UsageStatsModel.id).label("event_count"),
            )
            .where(
                UsageStatsModel.workspace_id == workspace_id,
                UsageStatsModel.date >= start_date,
                UsageStatsModel.date <= end_date,
            )
            .group_by(UsageStatsModel.date)
            .order_by(UsageStatsModel.date)
        )

        # Build daily stats list
        daily_stats = [
            DailyUsage(date=row.date.isoformat(), count=row.event_count)
            for row in daily_stats_result.all()
        ]

        total_requests = sum(stat.count for stat in daily_stats)

        return UsageHistoryResponse(
            daily_stats=daily_stats,
            total_requests=total_requests,
            period_start=start_date.isoformat(),
            period_end=end_date.isoformat(),
        )


@router.get("/usage/breakdown", response_model=UsageBreakdownResponse)
async def get_workspace_usage_breakdown(
    days: int = Query(30, ge=1, le=90),
    user: dict = Depends(get_user_from_api_key_or_session),
    db: AsyncSession = Depends(get_db),
):
    """Get workspace-wide usage breakdown by endpoint.

    Aggregates endpoint usage across all workspace members.

    Args:
        days: Number of days to analyze (default: 30, max: 90)

    Returns:
        Usage breakdown by endpoint with percentages

    Raises:
        HTTPException: 400 if no workspace selected, 500 on error
    """
    from utils import db_transaction

    workspace_id = user.get("current_workspace_id")
    if not workspace_id:
        raise HTTPException(
            status_code=400,
            detail="No workspace selected. Please select an workspace first.",
        )

    async with db_transaction(
        db, "get_workspace_usage_breakdown", "Failed to get workspace usage breakdown"
    ):
        # Calculate date range
        end_date = utcnow().date()
        start_date = end_date - timedelta(days=days - 1)

        # Aggregate usage by endpoint across all members
        breakdown_result = await db.execute(
            select(
                UsageStatsModel.endpoint,
                func.count(UsageStatsModel.id).label("event_count"),
            )
            .where(
                UsageStatsModel.workspace_id == workspace_id,
                UsageStatsModel.date >= start_date,
                UsageStatsModel.date <= end_date,
            )
            .group_by(UsageStatsModel.endpoint)
            .order_by(func.count(UsageStatsModel.id).desc())
        )

        endpoint_stats = breakdown_result.all()
        total_requests = sum(row.event_count for row in endpoint_stats)

        # Build endpoint usage list with percentages
        by_endpoint = [
            EndpointUsage(
                endpoint=row.endpoint,
                count=row.event_count,
                percentage=round((row.event_count / total_requests * 100), 2)
                if total_requests > 0
                else 0.0,
            )
            for row in endpoint_stats
        ]

        return UsageBreakdownResponse(
            by_endpoint=by_endpoint,
            total_requests=total_requests,
            period_days=days,
        )


# ============================================================================
# Per-member usage (Issue #331)
# ============================================================================


class MemberUsageEntry(BaseModel):
    """Usage stats for a single workspace member."""

    user_id: str
    name: str | None
    email: str | None
    memory_count: int
    api_calls_today: int
    api_calls_week: int


class MemberUsageResponse(BaseModel):
    """Per-member usage breakdown for workspace."""

    members: list[MemberUsageEntry]
    total_members: int


@router.get("/usage/members", response_model=MemberUsageResponse)
async def get_workspace_member_usage(
    user: dict = Depends(get_user_from_api_key_or_session),
    db: AsyncSession = Depends(get_db),
):
    """Get per-member usage breakdown for the current workspace.

    Shows memory count and API calls per member.
    """
    from utils import db_transaction

    workspace_id = user.get("current_workspace_id")
    if not workspace_id:
        raise HTTPException(status_code=400, detail="No workspace selected")

    async with db_transaction(db, "get_member_usage", "Failed to get member usage"):
        # Get all members with user info
        members_result = await db.execute(
            select(WorkspaceMember.user_id, User.name, User.email)
            .join(User, WorkspaceMember.user_id == User.user_id)
            .where(WorkspaceMember.workspace_id == workspace_id)
        )
        members = members_result.all()

        if not members:
            return MemberUsageResponse(members=[], total_members=0)

        member_ids = [m.user_id for m in members]
        today = utcnow().date()
        week_ago = today - timedelta(days=7)

        # Memory counts per user (batch query)
        memory_counts_result = await db.execute(
            select(Memory.user_id, func.count(Memory.id))
            .where(
                Memory.user_id.in_(member_ids),
                Memory.workspace_id == workspace_id,
                Memory.deleted_at.is_(None),
            )
            .group_by(Memory.user_id)
        )
        memory_counts: dict[str, int] = {row[0]: row[1] for row in memory_counts_result.all()}

        # API calls today per user (batch query)
        api_today_result = await db.execute(
            select(UsageStatsModel.user_id, func.count(UsageStatsModel.id))
            .where(
                UsageStatsModel.workspace_id == workspace_id,
                UsageStatsModel.date == today,
            )
            .group_by(UsageStatsModel.user_id)
        )
        api_today: dict[str, int] = {row[0]: row[1] for row in api_today_result.all()}

        # API calls this week per user (batch query)
        api_week_result = await db.execute(
            select(UsageStatsModel.user_id, func.count(UsageStatsModel.id))
            .where(
                UsageStatsModel.workspace_id == workspace_id,
                UsageStatsModel.date >= week_ago,
            )
            .group_by(UsageStatsModel.user_id)
        )
        api_week: dict[str, int] = {row[0]: row[1] for row in api_week_result.all()}

        entries = [
            MemberUsageEntry(
                user_id=m.user_id,
                name=m.name,
                email=m.email,
                memory_count=memory_counts.get(m.user_id, 0),
                api_calls_today=api_today.get(m.user_id, 0),
                api_calls_week=api_week.get(m.user_id, 0),
            )
            for m in members
        ]

        # Sort by memory count descending
        entries.sort(key=lambda e: e.memory_count, reverse=True)

        return MemberUsageResponse(members=entries, total_members=len(entries))


# ============================================================================
# Embedding Queue Status (Issue #93)
# ============================================================================


class FailedMemoryInfo(BaseModel):
    """Info about a failed embedding memory."""

    id: str
    summary: str
    embedding_error: str | None
    created_at: str
    updated_at: str | None


class EmbeddingStatusResponse(BaseModel):
    """Embedding queue status response."""

    total: int
    by_status: dict[str, int]
    failed_memories: list[FailedMemoryInfo]


@router.get("/embedding-status", response_model=EmbeddingStatusResponse)
async def get_embedding_status(
    user: dict = Depends(get_user_from_api_key_or_session),
    db: AsyncSession = Depends(get_db),
    context_id: str | None = Query(None, description="Filter by context ID (UUID)"),
) -> EmbeddingStatusResponse:
    """Get embedding processing queue status.

    Issue #93: Visibility into embedding pipeline status.
    Returns counts by status and details of any failed memories.
    """
    from uuid import UUID as PyUUID

    # Validate context_id is a valid UUID if provided
    if context_id:
        try:
            PyUUID(context_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail="Invalid context_id format") from e

    workspace_id = user.get("current_workspace_id")
    if not workspace_id:
        raise HTTPException(status_code=400, detail="No active workspace")

    # Use indexed Memory.workspace_id directly (no member_ids round-trip)
    conditions = [
        Memory.workspace_id == workspace_id,
        Memory.deleted_at.is_(None),
    ]
    if context_id:
        conditions.append(Memory.context_id == context_id)

    # #1496: scope to the contexts this caller may actually see.
    #
    # Every other stats path in this file applies this rule (see the privacy
    # check in the workspace-stats loop above); this one did not, and it
    # returns up to 50 failed memories WITH their summaries. A workspace member
    # could therefore read the first 200 characters of another member's PRIVATE
    # context contents by asking for the embedding queue.
    #
    # Same three-way rule as /api/v1/contexts: an owner sees everything, shared
    # contexts are visible to all members, and a private context is visible
    # only to whoever created it.
    user_id = user.get("user_id")
    owner_result = await db.execute(
        select(Workspace.owner_user_id).where(Workspace.id == workspace_id)
    )
    if owner_result.scalar_one_or_none() != user_id:
        accessible = select(Context.id).where(
            Context.workspace_id == workspace_id,
            or_(Context.is_private.is_(False), Context.created_by == user_id),
        )
        conditions.append(Memory.context_id.in_(accessible))

    # Count by status
    status_stmt = (
        select(
            Memory.embedding_status,
            func.count(Memory.id).label("memory_count"),
        )
        .where(*conditions)
        .group_by(Memory.embedding_status)
    )
    status_result = await db.execute(status_stmt)
    by_status: dict[str, int] = {}
    total = 0
    for row in status_result.all():
        by_status[row[0]] = row[1]
        total += row[1]

    # Get failed memory details (max 50)
    failed_memories: list[FailedMemoryInfo] = []
    if by_status.get("failed", 0) > 0:
        failed_stmt = (
            select(Memory)
            .where(
                *conditions,
                Memory.embedding_status == "failed",
            )
            .order_by(Memory.updated_at.desc())
            .limit(50)
        )
        failed_result = await db.execute(failed_stmt)
        for mem in failed_result.scalars().all():
            failed_memories.append(
                FailedMemoryInfo(
                    id=str(mem.id),
                    summary=mem.summary[:200] if mem.summary else "",
                    embedding_error=mem.embedding_error,
                    created_at=to_utc_iso(mem.created_at) or "",
                    updated_at=to_utc_iso(mem.updated_at),
                )
            )

    return EmbeddingStatusResponse(
        total=total,
        by_status=by_status,
        failed_memories=failed_memories,
    )
