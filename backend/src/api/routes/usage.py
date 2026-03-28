"""Usage Statistics API Routes.

Provides endpoints for quota management and usage tracking.
Issue #48 - Usage Statistics - Plan Limits & Usage Tracking
"""

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
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


# ============================================================================
# Response Models
# ============================================================================


class PlanLimits(BaseModel):
    """Plan limits and quotas."""

    plan_name: str = Field(..., description="Plan name (free/pro/enterprise)")
    memory_limit: int = Field(..., description="Maximum memories allowed")
    daily_api_limit: int = Field(..., description="Daily API call limit")
    weekly_api_limit: int = Field(..., description="Weekly API call limit")


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
        settings = get_settings()

        # Get user plan (or default to free)
        result = await db.execute(select(UserPlan).where(UserPlan.user_id == user_id))
        plan = result.scalar_one_or_none()

        if not plan:
            # Create default free plan from environment variables
            plan = UserPlan(
                user_id=user_id,
                plan_name="free",
                memory_limit=settings.default_plan_memory_limit,
                daily_api_limit=settings.default_plan_daily_api_limit,
                weekly_api_limit=settings.default_plan_weekly_api_limit,
            )
            db.add(plan)
            await db.commit()

        # Get current memory count
        memory_result = await db.execute(
            select(func.count(Memory.id)).where(Memory.user_id == user_id)
        )
        memory_count = memory_result.scalar() or 0

        # Get API calls today (Issue #238: Separate MCP/REST/Public)
        today = utcnow().date()
        today_result = await db.execute(
            select(func.count(UsageStats.id)).where(
                UsageStats.user_id == user_id, UsageStats.date == today
            )
        )
        api_calls_today = today_result.scalar() or 0

        # Get MCP calls today
        mcp_today_result = await db.execute(
            select(func.count(UsageStats.id)).where(
                UsageStats.user_id == user_id,
                UsageStats.date == today,
                UsageStats.endpoint.like("mcp:%"),
            )
        )
        mcp_calls_today = mcp_today_result.scalar() or 0

        # Get Public REST calls today
        public_today_result = await db.execute(
            select(func.count(UsageStats.id)).where(
                UsageStats.user_id == user_id,
                UsageStats.date == today,
                UsageStats.endpoint.like("/api/v1/public/%"),
            )
        )
        public_calls_today = public_today_result.scalar() or 0

        # Get REST calls today (non-public)
        rest_today_result = await db.execute(
            select(func.count(UsageStats.id)).where(
                UsageStats.user_id == user_id,
                UsageStats.date == today,
                UsageStats.endpoint.like("/api/v1/%"),
                UsageStats.endpoint.notlike("/api/v1/public/%"),
            )
        )
        rest_calls_today = rest_today_result.scalar() or 0

        # Get API calls this week (last 7 days)
        week_ago = utcnow().date() - timedelta(days=7)
        week_result = await db.execute(
            select(func.count(UsageStats.id)).where(
                UsageStats.user_id == user_id, UsageStats.date >= week_ago
            )
        )
        api_calls_week = week_result.scalar() or 0

        # Get MCP calls this week
        mcp_week_result = await db.execute(
            select(func.count(UsageStats.id)).where(
                UsageStats.user_id == user_id,
                UsageStats.date >= week_ago,
                UsageStats.endpoint.like("mcp:%"),
            )
        )
        mcp_calls_week = mcp_week_result.scalar() or 0

        # Get Public REST calls this week
        public_week_result = await db.execute(
            select(func.count(UsageStats.id)).where(
                UsageStats.user_id == user_id,
                UsageStats.date >= week_ago,
                UsageStats.endpoint.like("/api/v1/public/%"),
            )
        )
        public_calls_week = public_week_result.scalar() or 0

        # Get REST calls this week (non-public)
        rest_week_result = await db.execute(
            select(func.count(UsageStats.id)).where(
                UsageStats.user_id == user_id,
                UsageStats.date >= week_ago,
                UsageStats.endpoint.like("/api/v1/%"),
                UsageStats.endpoint.notlike("/api/v1/public/%"),
            )
        )
        rest_calls_week = rest_week_result.scalar() or 0

        # Build response
        response = UsageCurrentResponse(
            plan=PlanLimits(
                plan_name=plan.plan_name,
                memory_limit=plan.memory_limit,
                daily_api_limit=plan.daily_api_limit,
                weekly_api_limit=plan.weekly_api_limit,
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

        # Calculate period
        end_date = utcnow().date()
        start_date = end_date - timedelta(days=days)

        # Get daily usage
        result = await db.execute(
            select(
                UsageStats.date,
                func.count(UsageStats.id).label("count"),
            )
            .where(
                UsageStats.user_id == user_id,
                UsageStats.date >= start_date,
                UsageStats.date <= end_date,
            )
            .group_by(UsageStats.date)
            .order_by(UsageStats.date)
        )

        daily_stats_dict = {row.date: row.count for row in result.all()}

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

        # Calculate period
        start_date = utcnow().date() - timedelta(days=days)

        # Get usage by endpoint
        result = await db.execute(
            select(
                UsageStats.endpoint,
                func.count(UsageStats.id).label("count"),
            )
            .where(UsageStats.user_id == user_id, UsageStats.date >= start_date)
            .group_by(UsageStats.endpoint)
            .order_by(func.count(UsageStats.id).desc())
        )

        endpoint_stats = result.all()
        total_requests = sum(row.count for row in endpoint_stats)

        by_endpoint = [
            EndpointUsage(
                endpoint=row.endpoint,
                count=row.count,
                percentage=round((row.count / total_requests * 100), 2)
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
