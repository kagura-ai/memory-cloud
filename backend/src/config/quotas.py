"""Quota and rate limiting configuration.

Defines QPS limits and memory point quotas for different user plans.

Issue #2 - Phase 1: QPS要件定義
Issue #1 - Quota management
"""

from enum import StrEnum


class UserPlan(StrEnum):
    """User plan types."""

    FREE = "free"
    PRO = "pro"
    ENTERPRISE = "enterprise"


# ============================================================================
# Memory Point Quotas (Qdrant points)
# ============================================================================

MEMORY_QUOTAS = {
    UserPlan.FREE: {
        "total_points": 10_000,
        "working_points": 2_000,
        "persistent_points": 8_000,
    },
    UserPlan.PRO: {
        "total_points": 100_000,
        "working_points": 20_000,
        "persistent_points": 80_000,
    },
    UserPlan.ENTERPRISE: {
        "total_points": 1_000_000,
        "working_points": 200_000,
        "persistent_points": 800_000,
    },
}


def get_memory_quota(plan: UserPlan | str) -> dict[str, int]:
    """Get memory quota for plan.

    Args:
        plan: User plan

    Returns:
        Quota dict with total_points, working_points, persistent_points

    Example:
        >>> get_memory_quota(UserPlan.PRO)
        {'total_points': 100000, 'working_points': 20000, 'persistent_points': 80000}
    """
    if isinstance(plan, str):
        plan = UserPlan(plan)

    return MEMORY_QUOTAS[plan]


# ============================================================================
# Rate Limiting (QPS)
# ============================================================================

# Requests per minute
RATE_LIMITS = {
    UserPlan.FREE: {
        "requests_per_minute": 100,
        "requests_per_hour": 5_000,
        "requests_per_day": 100_000,
    },
    UserPlan.PRO: {
        "requests_per_minute": 1_000,
        "requests_per_hour": 50_000,
        "requests_per_day": 1_000_000,
    },
    UserPlan.ENTERPRISE: {
        "requests_per_minute": 10_000,
        "requests_per_hour": 500_000,
        "requests_per_day": 10_000_000,
    },
}


def get_rate_limit(plan: UserPlan | str) -> dict[str, int]:
    """Get rate limit for plan.

    Args:
        plan: User plan

    Returns:
        Rate limit dict with requests_per_minute, etc.

    Example:
        >>> get_rate_limit(UserPlan.PRO)
        {'requests_per_minute': 1000, ...}
    """
    if isinstance(plan, str):
        plan = UserPlan(plan)

    return RATE_LIMITS[plan]


# ============================================================================
# Quota Check Functions
# ============================================================================


async def check_memory_quota(
    user_id: str,
    plan: UserPlan,
    current_points: int,
    scope: str = "total",
) -> bool:
    """Check if user has quota remaining.

    Args:
        user_id: User ID
        plan: User plan
        current_points: Current point count
        scope: "total", "working", or "persistent"

    Returns:
        True if under quota

    Example:
        >>> can_add = await check_memory_quota("user123", UserPlan.PRO, 50000)
        >>> can_add
        True
    """
    quota = get_memory_quota(plan)

    if scope == "total":
        limit = quota["total_points"]
    elif scope == "working":
        limit = quota["working_points"]
    elif scope == "persistent":
        limit = quota["persistent_points"]
    else:
        raise ValueError(f"Invalid scope: {scope}")

    return current_points < limit


async def check_rate_limit(user_id: str, plan: UserPlan, window: str = "minute") -> bool:
    """Check if user is within rate limit.

    Uses Redis counters with TTL.

    Args:
        user_id: User ID
        plan: User plan
        window: "minute", "hour", or "day"

    Returns:
        True if under rate limit

    Raises:
        RateLimitError: If rate limit exceeded

    Example:
        >>> from db.redis import increment_counter
        >>> from utils.exceptions import RateLimitError
        >>>
        >>> # In middleware
        >>> count = await increment_counter(f"rate:{user_id}:minute", ttl=60)
        >>> if not await check_rate_limit(user_id, plan):
        ...     raise RateLimitError()
    """
    from db.redis import get_cache

    limits = get_rate_limit(plan)

    # TTL per window: minute=60s, hour=3600s, day=86400s
    if window == "minute":
        limit = limits["requests_per_minute"]
    elif window == "hour":
        limit = limits["requests_per_hour"]
    elif window == "day":
        limit = limits["requests_per_day"]
    else:
        raise ValueError(f"Invalid window: {window}")

    # Get current count from Redis
    key = f"rate_limit:{user_id}:{window}"
    count_str = await get_cache(key)
    count = int(count_str) if count_str else 0

    return count < limit


# ============================================================================
# Default Quota Settings
# ============================================================================

# デフォルトプラン（新規ユーザー）
DEFAULT_PLAN = UserPlan.FREE

# Admin quota（無制限）
ADMIN_QUOTA = {
    "total_points": float("inf"),
    "requests_per_minute": float("inf"),
}
