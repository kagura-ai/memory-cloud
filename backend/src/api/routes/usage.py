"""Usage Statistics API Routes.

Provides endpoints for quota management and usage tracking.
Issue #48 - Usage Statistics - Plan Limits & Usage Tracking
"""

from datetime import timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import SessionUser
from config.settings import get_settings
from db.base import get_db
from models.auth import UsageStats, UserPlan
from models.memory import Memory
from utils.datetime import utcnow
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/usage", tags=["usage"])


def _build_usage_filter(user_id: str, workspace_id=None):
    """Build workspace-scoped or user-scoped usage filter.

    Issue #50: When workspace_id is available, filter by both user and workspace.
    Falls back to user-only filter when no workspace selected.
    """
    if workspace_id:
        return and_(
            UsageStats.user_id == user_id,
            UsageStats.workspace_id == workspace_id,
        )
    return UsageStats.user_id == user_id


# ============================================================================
# Response Models
# ============================================================================


class PlanLimits(BaseModel):
    """Plan limits and quotas.

    Issue #198: ``daily_api_limit`` / ``weekly_api_limit`` are the
    sums of every API tier (MCP + REST + Public) so the dashboard's
    "API Calls Today" card stays meaningful when looking at the
    aggregate. Use the per-tier fields below for accurate breakdowns
    that match ``plan_tiers.py``. Public callers and the rate limiter
    should always read the per-tier fields, never the legacy combined
    sums.
    """

    plan_name: str = Field(..., description="Plan name (free/pro/enterprise)")
    memory_limit: int = Field(..., description="Maximum memories allowed")
    daily_total_limit: int = Field(
        ...,
        description="Combined daily limit (MCP + REST + Public)",
    )
    weekly_total_limit: int = Field(
        ...,
        description="Combined weekly limit (MCP + REST + Public)",
    )
    mcp_calls_per_day: int = Field(default=0, description="MCP API daily limit")
    mcp_calls_per_week: int = Field(default=0, description="MCP API weekly limit")
    rest_calls_per_day: int = Field(default=0, description="REST API daily limit")
    rest_calls_per_week: int = Field(default=0, description="REST API weekly limit")
    public_calls_per_day: int = Field(default=0, description="Public REST API daily limit")
    public_calls_per_week: int = Field(default=0, description="Public REST API weekly limit")


class AnalysisUsage(BaseModel):
    """Memory broadlistening daily quota usage (Issue #496).

    Mirrors the response detail shape of the 429 quota-exceeded body
    so the dashboard, the new-analysis modal (#497), and the gate
    rejection all read the same field names.
    """

    used_today: int = Field(0, description="Analyses started today (all statuses count)")
    limit_today: int = Field(0, description="Plan + addon daily limit")
    addon_bonus: int = Field(0, description="Addon-supplied bonus (extra_analysis_runs)")
    remaining_today: int = Field(0, description="max(0, limit_today - used_today)")
    resets_at: str = Field(
        ...,
        description=(
            "ISO-8601 timestamp of the next reset, in the caller's timezone "
            "(midnight of the next day). Always populated — the builder "
            "falls back to UTC when User.timezone is unset."
        ),
    )


class SleepContextsUsage(BaseModel):
    """Sleep-enabled contexts quota usage (Issue #560).

    This is the dashboard READ shape — ``used`` / ``limit`` / ``remaining``
    for showing "X / Y" in the UI. The 429 quota-exceeded body raised by
    ``ContextService._assert_sleep_quota_or_raise`` uses a parallel-but-
    distinct shape (``current`` / ``requested`` / ``limit`` / ``addon_bonus``)
    optimized for the action-rejection case ("you tried to enable one more,
    here's the new total"). Both surfaces share ``limit`` and ``addon_bonus``;
    the read surface adds ``remaining`` (= ``max(0, limit - used)``) for direct
    display, and the error surface adds ``requested`` (= ``current + 1``) for
    "how many would there be." Kept distinct so neither has to carry fields it
    does not need.
    """

    used: int = Field(0, description="Contexts with sleep_mode != 'skip' in this workspace")
    limit: int = Field(0, description="Plan + addon effective limit")
    addon_bonus: int = Field(0, description="Addon-supplied bonus (extra_sleep_contexts)")
    remaining: int = Field(0, description="max(0, limit - used)")


class WorkspacesUsage(BaseModel):
    """Owned-workspace cap usage (Issue #661).

    User-level — the cap is per-user, not per-workspace. The dashboard
    surfaces this so users can see "X / N workspaces owned" before
    hitting the cap on workspace creation.

    Unlike ``AnalysisUsage`` / ``SleepContextsUsage``, this field is
    populated regardless of which workspace the caller currently has
    selected — it is the user's own quota across all their owned
    workspaces.

    No ``addon_bonus`` field as of Issue #661 because there is no
    per-user addon SKU. If ``addon_workspace_bonus`` (or equivalent)
    is introduced — see the Out-of-scope section of #661 — add
    ``addon_bonus`` here to match ``SleepContextsUsage`` /
    ``AnalysisUsage`` shape.
    """

    used: int = Field(0, description="Owned workspaces (deleted_at IS NULL)")
    limit: int = Field(0, description="Effective cap: 1 (base) + users.workspace_slot_bonus (#675)")
    remaining: int = Field(0, description="max(0, limit - used)")


class CurrentUsage(BaseModel):
    """Current usage statistics."""

    memory_count: int = Field(..., description="Current memory count")
    api_calls_today: int = Field(..., description="API calls today (all APIs)")
    api_calls_this_week: int = Field(..., description="API calls this week (all APIs)")
    mcp_calls_today: int = Field(0, description="MCP calls today")
    mcp_calls_this_week: int = Field(0, description="MCP calls this week")
    rest_calls_today: int = Field(0, description="REST API calls today (non-public)")
    rest_calls_this_week: int = Field(0, description="REST API calls this week (non-public)")
    public_calls_today: int = Field(0, description="Public REST API calls today")
    public_calls_this_week: int = Field(0, description="Public REST API calls this week")
    analysis: AnalysisUsage | None = Field(
        None,
        description=(
            "Memory broadlistening daily quota stats (Issue #496). "
            "NULL when the caller has no current workspace selected."
        ),
    )
    sleep_contexts: SleepContextsUsage | None = Field(
        None,
        description=(
            "Sleep-enabled contexts quota stats (Issue #560). "
            "NULL when the caller has no current workspace selected."
        ),
    )
    workspaces: WorkspacesUsage = Field(
        ...,
        description=(
            "Owned-workspace cap usage for the caller (Issue #661). "
            "User-level — always populated, independently of current workspace."
        ),
    )


class UsageStatus(BaseModel):
    """Usage status with percentage."""

    current: int | float = Field(..., description="Current value")
    limit: int | float = Field(..., description="Limit value")
    percentage: float = Field(..., description="Usage percentage (0-100+)")
    is_warning: bool = Field(..., description="True if >= 80%")
    is_critical: bool = Field(..., description="True if >= 95%")
    is_exceeded: bool = Field(..., description="True if >= 100%")


class UsageCurrentResponse(BaseModel):
    """Current usage vs limits response."""

    plan: PlanLimits
    usage: CurrentUsage
    memory_usage: UsageStatus
    daily_api_usage: UsageStatus
    weekly_api_usage: UsageStatus


class DailyUsage(BaseModel):
    """Daily usage statistics."""

    date: str = Field(..., description="Date in ISO format (YYYY-MM-DD)")
    count: int = Field(..., ge=0, description="Number of requests on this date")


class UsageHistoryResponse(BaseModel):
    """Historical usage data."""

    daily_stats: list[DailyUsage] = Field(..., description="Daily usage breakdown")
    total_requests: int = Field(..., ge=0, description="Total requests in period")
    period_start: str = Field(..., description="Start date (YYYY-MM-DD)")
    period_end: str = Field(..., description="End date (YYYY-MM-DD)")


class EndpointUsage(BaseModel):
    """Usage by endpoint."""

    endpoint: str = Field(..., description="API endpoint or MCP tool")
    count: int = Field(..., ge=0, description="Number of requests")
    percentage: float = Field(..., description="Percentage of total requests")


class UsageBreakdownResponse(BaseModel):
    """Usage breakdown by endpoint."""

    by_endpoint: list[EndpointUsage] = Field(..., description="Usage by endpoint")
    total_requests: int = Field(..., ge=0, description="Total requests")
    period_days: int = Field(..., description="Number of days in period")


# ============================================================================
# Helper Functions
# ============================================================================


def calculate_usage_status(current: int | float, limit: int | float) -> UsageStatus:
    """Calculate usage status with warnings.

    Args:
        current: Current usage value
        limit: Limit value

    Returns:
        UsageStatus with percentage and warning flags
    """
    settings = get_settings()
    percentage = (current / limit * 100) if limit > 0 else 0.0

    # Use configurable thresholds
    warning_threshold = settings.usage_warning_threshold * 100
    critical_threshold = settings.usage_critical_threshold * 100

    return UsageStatus(
        current=current,
        limit=limit,
        percentage=round(percentage, 2),
        is_warning=percentage >= warning_threshold,
        is_critical=percentage >= critical_threshold,
        is_exceeded=percentage >= 100.0,
    )


# ============================================================================
# Endpoints
# ============================================================================


async def _build_analysis_usage(
    db: AsyncSession,
    user_id: str,
    workspace_id: UUID | None,
    *,
    effective_quotas: dict[str, int] | None = None,
) -> "AnalysisUsage | None":
    """Build the ``analysis`` field of /usage/current (Issue #496).

    Returns None when no workspace is selected (analysis is workspace-
    scoped). Delegates the count + tz-window math to
    ``query_service.get_today_analysis_count`` so the gate and the
    dashboard share one source of truth.

    Args:
        effective_quotas: Pre-computed effective quotas dict (from
            ``EffectiveQuotaService.get_effective_quotas``) if the caller
            already has it. Passing it through avoids a duplicate
            EffectiveQuotaService call from the same request. Issue #570
            removed the GET-time self-heal COMMIT, so the kwarg is now
            purely a DB-roundtrip optimization rather than a correctness
            requirement.
    """
    if not workspace_id:
        return None

    from models.auth import User, Workspace
    from services.analysis import query_service
    from services.analysis.query_service import day_window_utc
    from services.effective_quota_service import EffectiveQuotaService

    tz_name = (
        await db.execute(select(User.timezone).where(User.user_id == user_id))
    ).scalar_one_or_none() or "UTC"

    used_today = await query_service.get_today_analysis_count(
        db, workspace_id=workspace_id, user_timezone=tz_name
    )
    if effective_quotas is None:
        effective_quotas = await EffectiveQuotaService(db).get_effective_quotas(workspace_id)
    limit_today = int(effective_quotas.get("analysis_runs_per_day", 0) or 0)
    addon_bonus = int(
        (
            await db.execute(
                select(Workspace.addon_analysis_bonus).where(Workspace.id == workspace_id)
            )
        ).scalar_one_or_none()
        or 0
    )

    # Format ``resets_at`` in caller's tz so the dashboard's display
    # matches the 429 body format (caller's local midnight).
    from zoneinfo import ZoneInfo

    _, day_end_utc = day_window_utc(tz_name)
    try:
        client_tz = ZoneInfo(tz_name)
    except Exception:
        client_tz = ZoneInfo("UTC")
    resets_at = day_end_utc.replace(tzinfo=ZoneInfo("UTC")).astimezone(client_tz).isoformat()

    return AnalysisUsage(
        used_today=used_today,
        limit_today=limit_today,
        addon_bonus=addon_bonus,
        remaining_today=max(0, limit_today - used_today),
        resets_at=resets_at,
    )


async def _build_sleep_contexts_usage(
    db: AsyncSession,
    workspace_id: UUID | None,
    *,
    effective_quotas: dict[str, int] | None = None,
) -> "SleepContextsUsage | None":
    """Build the ``sleep_contexts`` field of /usage/current (Issue #560).

    Returns None when no workspace is selected (sleep quota is workspace-
    scoped). Counts contexts with ``sleep_mode != 'skip'`` in the workspace
    and reads the effective limit (plan tier + addon) from EffectiveQuotaService
    so the dashboard and the 429 quota body share one source of truth.

    Args:
        effective_quotas: Pre-computed effective quotas dict from
            ``EffectiveQuotaService.get_effective_quotas`` if the caller
            already has it (e.g. ``workspace.py:get_workspace_usage_current``
            calls the service for other fields). Passing the dict avoids a
            duplicate DB roundtrip from the same request. Issue #570 removed
            the GET-time self-heal COMMIT in EffectiveQuotaService, so the
            kwarg is now a DB-roundtrip optimization rather than a
            correctness requirement. When ``None``, the helper fetches
            fresh.
    """
    if not workspace_id:
        return None

    from models.auth import Context, Workspace
    from services.effective_quota_service import EffectiveQuotaService

    if effective_quotas is None:
        effective_quotas = await EffectiveQuotaService(db).get_effective_quotas(workspace_id)
    limit = int(effective_quotas.get("sleep_enabled_contexts_limit", 0) or 0)
    raw_addon_bonus = int(
        (
            await db.execute(
                select(Workspace.addon_sleep_contexts_bonus).where(Workspace.id == workspace_id)
            )
        ).scalar_one_or_none()
        or 0
    )
    # Normalize: when the effective limit is 0 (FREE/BASIC tier),
    # ``addon_sleep_contexts_bonus`` is ignored by the runtime gate
    # (``Workspace.effective_sleep_enabled_contexts_limit`` clamps to 0
    # regardless of the addon column — see
    # ``backend/src/models/auth.py:effective_sleep_enabled_contexts_limit``).
    # Surface 0 here so misconfigured rows (manual SQL insert, future
    # Stripe SKU bug) don't make the dashboard claim "Includes +N from
    # addon" while the addon has no effect on the cap.
    addon_bonus = raw_addon_bonus if limit > 0 else 0

    # Exclude soft-deleted contexts so the dashboard usage line matches the
    # quota check in ContextService._assert_sleep_quota_or_raise (which also
    # filters Context.deleted_at IS NULL). Without this filter, deleting a
    # sleep-enabled context would not free up its slot in the displayed count.
    used = int(
        (
            await db.execute(
                select(func.count(Context.id)).where(
                    Context.workspace_id == workspace_id,
                    Context.deleted_at.is_(None),
                    Context.sleep_mode != "skip",
                )
            )
        ).scalar_one()
    )

    return SleepContextsUsage(
        used=used,
        limit=limit,
        addon_bonus=addon_bonus,
        remaining=max(0, limit - used),
    )


async def _build_workspaces_usage(db: AsyncSession, user_id: str) -> "WorkspacesUsage":
    """Build the ``workspaces`` field of /usage/current (#674 sub-A, #675).

    User-level cap: ``get_user_workspace_cap_summary`` returns the
    owned count and the user's ``workspace_slot_bonus`` in a single
    SELECT (JOIN of users + workspaces). The same helper is used by
    ``QuotaService.check_workspace_creation_allowed`` so the gate and
    the dashboard read consistent state.

    Effective cap = ``1 (base) + workspace_slot_bonus``.

    Always returns a populated ``WorkspacesUsage`` — the cap is
    user-scoped so there is no "no current workspace" null case here.
    """
    from utils.plan_resolver import get_user_workspace_cap_summary

    owned_count, cap = await get_user_workspace_cap_summary(db, user_id)

    return WorkspacesUsage(
        used=owned_count,
        limit=cap,
        remaining=max(0, cap - owned_count),
    )


@router.get("/current", response_model=UsageCurrentResponse)
async def get_current_usage(
    user: SessionUser,
    db: AsyncSession = Depends(get_db),
):
    """Get current usage vs plan limits.

    Returns:
        Current usage statistics with plan limits and warning flags

    Raises:
        HTTPException: 500 if failed to retrieve usage
    """
    try:
        user_id = user["user_id"]
        # Issue #50: Workspace-scoped usage stats
        current_workspace_id = user.get("current_workspace_id")

        usage_filter = _build_usage_filter(user_id, current_workspace_id)

        settings = get_settings()

        # The default UserPlan row is created at user-creation time in
        # auth.roles.RoleManager._ensure_user_postgres (#586). The fallback
        # below covers (a) the race window between user creation and the
        # first /usage/current call and (b) test fixtures that bypass the
        # signup flow.
        result = await db.execute(select(UserPlan).where(UserPlan.user_id == user_id))
        plan = result.scalar_one_or_none()

        if not plan:
            plan = UserPlan.default_for_user(user_id, settings)

        # Get current memory count.
        # Issue #198 (Bug D): exclude soft-deleted rows so this matches the
        # workspace endpoint and the underlying DB count. Without this filter
        # the dashboard's "memories" card double-counted forgotten items.
        memory_result = await db.execute(
            select(func.count(Memory.id)).where(
                Memory.user_id == user_id,
                Memory.deleted_at.is_(None),
            )
        )
        memory_count = memory_result.scalar() or 0

        # Get API calls today (Issue #238: Separate MCP/REST/Public)
        today = utcnow().date()
        today_result = await db.execute(
            select(func.count(UsageStats.id)).where(usage_filter, UsageStats.date == today)
        )
        api_calls_today = today_result.scalar() or 0

        # Get MCP calls today
        mcp_today_result = await db.execute(
            select(func.count(UsageStats.id)).where(
                usage_filter,
                UsageStats.date == today,
                UsageStats.endpoint.like("mcp:%"),
            )
        )
        mcp_calls_today = mcp_today_result.scalar() or 0

        # Get Public REST calls today
        public_today_result = await db.execute(
            select(func.count(UsageStats.id)).where(
                usage_filter,
                UsageStats.date == today,
                UsageStats.endpoint.like("/api/v1/public/%"),
            )
        )
        public_calls_today = public_today_result.scalar() or 0

        # Get REST calls today (non-public)
        rest_today_result = await db.execute(
            select(func.count(UsageStats.id)).where(
                usage_filter,
                UsageStats.date == today,
                UsageStats.endpoint.like("/api/v1/%"),
                UsageStats.endpoint.notlike("/api/v1/public/%"),
            )
        )
        rest_calls_today = rest_today_result.scalar() or 0

        # Get API calls this week (last 7 days)
        week_ago = utcnow().date() - timedelta(days=7)
        week_result = await db.execute(
            select(func.count(UsageStats.id)).where(usage_filter, UsageStats.date >= week_ago)
        )
        api_calls_week = week_result.scalar() or 0

        # Get MCP calls this week
        mcp_week_result = await db.execute(
            select(func.count(UsageStats.id)).where(
                usage_filter,
                UsageStats.date >= week_ago,
                UsageStats.endpoint.like("mcp:%"),
            )
        )
        mcp_calls_week = mcp_week_result.scalar() or 0

        # Get Public REST calls this week
        public_week_result = await db.execute(
            select(func.count(UsageStats.id)).where(
                usage_filter,
                UsageStats.date >= week_ago,
                UsageStats.endpoint.like("/api/v1/public/%"),
            )
        )
        public_calls_week = public_week_result.scalar() or 0

        # Get REST calls this week (non-public)
        rest_week_result = await db.execute(
            select(func.count(UsageStats.id)).where(
                usage_filter,
                UsageStats.date >= week_ago,
                UsageStats.endpoint.like("/api/v1/%"),
                UsageStats.endpoint.notlike("/api/v1/public/%"),
            )
        )
        rest_calls_week = rest_week_result.scalar() or 0

        # Fetch effective quotas ONCE and pass into both helpers so we don't
        # trigger ``EffectiveQuotaService`` twice. Issue #570 removed the
        # GET-time self-heal COMMIT, so this is now a DB-roundtrip optimization
        # rather than a correctness requirement; the kwarg plumbing is kept
        # because both helpers still need the same dict.
        #
        # On ``ValueError`` (workspace not found), pass an EMPTY dict (not
        # ``None``) into the helpers. ``None`` would make the helpers fall
        # back to fetching ``EffectiveQuotaService`` themselves — which would
        # re-raise the same ``ValueError``, defeating the "compute once"
        # invariant. Empty dict makes the helpers' ``.get(key, 0) or 0`` calls
        # land on 0, surfacing a graceful "no quota data" response rather
        # than crashing the dashboard.
        from services.effective_quota_service import EffectiveQuotaService

        effective_quotas_dict: dict[str, int] | None = None
        if current_workspace_id is not None:
            try:
                effective_quotas_dict = await EffectiveQuotaService(db).get_effective_quotas(
                    current_workspace_id
                )
            except ValueError:
                effective_quotas_dict = {}

        analysis_usage = await _build_analysis_usage(
            db, user_id, current_workspace_id, effective_quotas=effective_quotas_dict
        )
        sleep_contexts_usage = await _build_sleep_contexts_usage(
            db, current_workspace_id, effective_quotas=effective_quotas_dict
        )
        # Issue #661: user-level owned-workspace cap (independent of current_workspace_id).
        workspaces_usage = await _build_workspaces_usage(db, user_id)

        # Build response
        response = UsageCurrentResponse(
            plan=PlanLimits(
                plan_name=plan.plan_name,
                memory_limit=plan.memory_limit,
                daily_total_limit=plan.daily_api_limit,
                weekly_total_limit=plan.weekly_api_limit,
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
                analysis=analysis_usage,
                sleep_contexts=sleep_contexts_usage,
                workspaces=workspaces_usage,
            ),
            memory_usage=calculate_usage_status(memory_count, plan.memory_limit),
            daily_api_usage=calculate_usage_status(api_calls_today, plan.daily_api_limit),
            weekly_api_usage=calculate_usage_status(api_calls_week, plan.weekly_api_limit),
        )

        logger.info("current_usage_retrieved", user_id=user_id)
        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_current_usage_failed", error=str(e), user_id=user_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve current usage",
        ) from e


@router.get("/history", response_model=UsageHistoryResponse)
async def get_usage_history(
    user: SessionUser,
    db: AsyncSession = Depends(get_db),
    days: int = Query(7, ge=1, le=90, description="Number of days to retrieve"),
):
    """Get historical usage data.

    Args:
        days: Number of days to include (1-90, default: 7)

    Returns:
        Daily usage statistics for the specified period

    Raises:
        HTTPException: 500 if failed to retrieve history
    """
    try:
        user_id = user["user_id"]
        current_workspace_id = user.get("current_workspace_id")
        usage_filter = _build_usage_filter(user_id, current_workspace_id)

        # Calculate period
        end_date = utcnow().date()
        start_date = end_date - timedelta(days=days)

        # Get daily usage
        result = await db.execute(
            select(
                UsageStats.date,
                func.count(UsageStats.id).label("event_count"),
            )
            .where(
                usage_filter,
                UsageStats.date >= start_date,
                UsageStats.date <= end_date,
            )
            .group_by(UsageStats.date)
            .order_by(UsageStats.date)
        )

        daily_stats_dict = {row.date: row.event_count for row in result.all()}

        # Fill in missing dates with 0
        daily_stats = []
        current_date = start_date
        while current_date <= end_date:
            daily_stats.append(
                DailyUsage(
                    date=current_date.isoformat(),
                    count=daily_stats_dict.get(current_date, 0),
                )
            )
            current_date += timedelta(days=1)

        total_requests = sum(stat.count for stat in daily_stats)

        response = UsageHistoryResponse(
            daily_stats=daily_stats,
            total_requests=total_requests,
            period_start=start_date.isoformat(),
            period_end=end_date.isoformat(),
        )

        logger.info("usage_history_retrieved", user_id=user_id, days=days)
        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_usage_history_failed", error=str(e), user_id=user_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve usage history",
        ) from e


@router.get("/breakdown", response_model=UsageBreakdownResponse)
async def get_usage_breakdown(
    user: SessionUser,
    db: AsyncSession = Depends(get_db),
    days: int = Query(30, ge=1, le=90, description="Number of days to analyze"),
):
    """Get usage breakdown by endpoint.

    Args:
        days: Number of days to analyze (1-90, default: 30)

    Returns:
        Usage statistics grouped by endpoint

    Raises:
        HTTPException: 500 if failed to retrieve breakdown
    """
    try:
        user_id = user["user_id"]
        current_workspace_id = user.get("current_workspace_id")
        usage_filter = _build_usage_filter(user_id, current_workspace_id)

        # Calculate period
        start_date = utcnow().date() - timedelta(days=days)

        # Get usage by endpoint
        result = await db.execute(
            select(
                UsageStats.endpoint,
                func.count(UsageStats.id).label("event_count"),
            )
            .where(usage_filter, UsageStats.date >= start_date)
            .group_by(UsageStats.endpoint)
            .order_by(func.count(UsageStats.id).desc())
        )

        endpoint_stats = result.all()
        total_requests = sum(row.event_count for row in endpoint_stats)

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

        response = UsageBreakdownResponse(
            by_endpoint=by_endpoint,
            total_requests=total_requests,
            period_days=days,
        )

        logger.info("usage_breakdown_retrieved", user_id=user_id, days=days)
        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_usage_breakdown_failed", error=str(e), user_id=user_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve usage breakdown",
        ) from e
