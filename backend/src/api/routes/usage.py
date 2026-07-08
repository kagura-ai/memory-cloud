"""Usage statistics shared models + helpers.

Issue #48 introduced user-scoped ``GET /usage/{current,history,breakdown}``
endpoints here. Issue #810 removed those route handlers — they were orphaned
after #668 dropped per-user plans and were superseded by the workspace-scoped
``/workspace/usage/*`` twins (``api/routes/workspace.py``). This module is now
a library: the response models and ``calculate_usage_status`` /
``_build_sleep_contexts_usage`` / ``_build_workspaces_usage`` helpers that
``workspace.py`` imports. It registers no routes.
"""

from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import get_settings
from utils.logger import get_logger

logger = get_logger(__name__)


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
    """Memory Analysis daily quota usage (Issue #496).

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
    mcp_calls_today: int = Field(default=0, description="MCP calls today")
    mcp_calls_this_week: int = Field(default=0, description="MCP calls this week")
    rest_calls_today: int = Field(default=0, description="REST API calls today (non-public)")
    rest_calls_this_week: int = Field(
        default=0, description="REST API calls this week (non-public)"
    )
    public_calls_today: int = Field(default=0, description="Public REST API calls today")
    public_calls_this_week: int = Field(default=0, description="Public REST API calls this week")
    analysis: AnalysisUsage | None = Field(
        default=None,
        description=(
            "Memory Analysis daily quota stats (Issue #496). "
            "NULL when the caller has no current workspace selected."
        ),
    )
    sleep_contexts: SleepContextsUsage | None = Field(
        default=None,
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
