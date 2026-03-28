"""Rate Limiting Configuration.

Issue #251: Rate limiting for REST API endpoints.

Provides tier-based rate limits and per-endpoint overrides for burst protection.
"""

from dataclasses import dataclass

from config.plan_tiers import PlanName


@dataclass(frozen=True)
class RateLimitConfig:
    """Rate limit configuration for a plan tier.

    Attributes:
        requests_per_minute: Maximum requests per minute (burst protection)
    """

    requests_per_minute: int


# ============================================================================
# Tier-based Rate Limits
# ============================================================================

TIER_RATE_LIMITS = {
    PlanName.FREE: RateLimitConfig(requests_per_minute=100),
    PlanName.BASIC: RateLimitConfig(requests_per_minute=300),
    PlanName.PRO: RateLimitConfig(requests_per_minute=1000),
}


# ============================================================================
# Per-Endpoint Overrides (Stricter Limits)
# ============================================================================

# Endpoints with stricter rate limits (regardless of plan tier)
# Note: Matching is prefix-based (path.startswith). Order matters when paths
# share a prefix — more specific paths must come first.
ENDPOINT_RATE_LIMITS = {
    # Issue #176: Sensitive credential endpoints
    "/api/v1/config/api-keys": 10,  # API key CRUD & regeneration
    "/api/v1/oauth/token": 10,  # Token exchange - brute force protection
    "/api/v1/oauth/authorize": 10,  # Authorization - brute force protection
    "/api/v1/oauth/introspect": 10,  # Token introspection
    "/api/v1/oauth/clients": 10,  # OAuth client registration
    "/api/v1/auth/": 10,  # Auth endpoints (login, callback, logout)
    "/api/v1/contexts": 50,  # Context management (CRUD)
}


# ============================================================================
# Excluded Paths (No Rate Limiting)
# ============================================================================

RATE_LIMIT_EXCLUDED_PATHS = {
    "/health",
    "/",
    "/docs",
    "/openapi.json",
    "/redoc",
}


# ============================================================================
# Helper Functions
# ============================================================================


def get_rate_limit_for_endpoint(path: str, plan: PlanName) -> int:
    """Get rate limit for endpoint (requests per minute).

    Checks endpoint-specific overrides first, then falls back to tier-based limits.

    Args:
        path: Request path (e.g., "/api/v1/auth/me")
        plan: User's plan tier

    Returns:
        Rate limit (requests per minute)

    Example:
        >>> get_rate_limit_for_endpoint("/api/v1/auth/me", PlanName.PRO)
        10  # Auth override (stricter than Pro's 1000/min)

        >>> get_rate_limit_for_endpoint("/api/v1/memory/remember", PlanName.FREE)
        100  # Tier-based limit
    """
    # Check endpoint overrides first (stricter limits)
    for endpoint_prefix, limit in ENDPOINT_RATE_LIMITS.items():
        if path.startswith(endpoint_prefix):
            return limit

    # Fall back to tier-based limit
    return TIER_RATE_LIMITS[plan].requests_per_minute


def should_rate_limit_path(path: str) -> bool:
    """Check if path should be rate limited.

    Args:
        path: Request path

    Returns:
        True if rate limiting should be applied

    Example:
        >>> should_rate_limit_path("/health")
        False

        >>> should_rate_limit_path("/api/v1/memory/remember")
        True
    """
    # Check if path is excluded
    if path in RATE_LIMIT_EXCLUDED_PATHS:
        return False

    # Check if path starts with excluded prefix
    if path.startswith("/static"):
        return False

    return True
