"""Plan Tier Definitions for Kagura Memory Cloud.

Issue #149: Plan tier enforcement (Free/Basic/Pro)

Defines quota limits and feature access for each plan tier.
"""

from dataclasses import dataclass, field
from enum import StrEnum

# Constants
UNLIMITED_CONTEXTS = 999999  # Effectively unlimited
PROTECTED_KEYS = frozenset(["OPENAI_API_KEY"])  # Keys that cannot be deleted


class PlanName(StrEnum):
    """Plan tier names enum for type safety.

    Issue #196: Enum abstraction to prevent typos and improve IDE support.
    """

    FREE = "free"
    BASIC = "basic"
    PRO = "pro"


@dataclass(frozen=True)
class PlanTier:
    """Plan tier configuration.

    Issue #276 (updated by Issue #661 / #675): user-owned workspace count
    is no longer driven by this dataclass — the cap is per-user via
    ``users.workspace_slot_bonus`` (``cap = 1 + bonus``). The plan tier
    controls per-workspace features (memory_limit, api_quota, etc.) only.
    Joined workspaces (via invite) are uncapped.

    Attributes:
        name: Plan tier name ('free', 'basic', 'pro')
        display_name: Human-readable name
        price_monthly: Monthly price in USD
        max_contexts_per_workspace: Maximum contexts per workspace
        max_members_per_workspace: Maximum members per workspace (Issue #229)
        max_resource_tokens: Maximum active resource tokens (Issue #242)
        max_connectors: Maximum ai-worker chat-ingest connectors per
            workspace (Issue #850, F6-a of #755). A SEPARATE seat cap from
            ``max_resource_tokens`` — connector-owned resource tokens minted
            by the F6-b setup flow bypass the ``max_resource_tokens`` Pro+
            gate and are governed by this seat count instead. 0 disables
            connector creation for the plan.
        memory_limit: Maximum memories per workspace
        daily_api_limit: Maximum API calls per day (legacy, kept for backward compatibility)
        weekly_api_limit: Maximum API calls per week (legacy, kept for backward compatibility)
        mcp_calls_per_day: MCP API calls per day (Issue #238)
        mcp_calls_per_week: MCP API calls per week (Issue #238)
        rest_calls_per_day: REST API calls per day (Issue #238)
        rest_calls_per_week: REST API calls per week (Issue #238)
        public_calls_per_day: Public REST API calls per day (Issue #238)
        public_calls_per_week: Public REST API calls per week (Issue #238)
        bound_public_calls_per_minute: Per-minute quota for each public-bound
            API key (Issue #626). Anonymous public access keeps its existing
            shared 50/min/context bucket; bound keys get their own per-key
            bucket sized by this field. 0 disables bound-key creation for
            the plan.
        allows_shared_contexts: Whether plan allows shared (non-private) contexts (Issue #271)
        features: Set of enabled features
    """

    name: str
    display_name: str
    price_monthly: int
    max_contexts_per_workspace: int
    max_members_per_workspace: int
    memory_limit: int
    daily_api_limit: int  # Legacy field, use mcp_calls_per_day instead
    weekly_api_limit: int  # Legacy field, use mcp_calls_per_week instead
    max_resource_tokens: int = 0  # Issue #242: Active resource tokens limit
    max_connectors: int = 0  # Issue #850 (F6-a of #755): ai-worker connector seat cap
    mcp_calls_per_day: int = 0  # Issue #238: MCP API quota
    mcp_calls_per_week: int = 0  # Issue #238: MCP API quota
    rest_calls_per_day: int = 0  # Issue #238: REST API quota
    rest_calls_per_week: int = 0  # Issue #238: REST API quota
    public_calls_per_day: int = 0  # Issue #238: Public REST API quota
    public_calls_per_week: int = 0  # Issue #238: Public REST API quota
    bound_public_calls_per_minute: int = 0  # Issue #626: per-key bucket for bound public keys
    analysis_runs_per_day: int = 0  # Issue #494: Memory Broadlistening analyses/day
    storage_limit_bytes: int = 0  # Issue #485: File-storage hard cap per workspace
    sleep_enabled_contexts_limit: int = 0  # Issue #560: Sleep-mode contexts cap (PRO-only)
    # Issue #709: Per-workspace BYOK embedding spend cap (USD). ``None`` means
    # "no tier-default cap" — uncapped unless an admin sets a per-workspace
    # override. ``Workspace.embedding_*_cap_usd`` (when set) takes precedence
    # over these tier defaults; see ``Workspace.effective_embedding_*_cap_usd``.
    embedding_daily_cap_usd: float | None = None
    embedding_monthly_cap_usd: float | None = None
    # Issue #661's ``max_owned_workspaces`` field was removed in #675 — the
    # user-level workspace cap is now derived from ``users.workspace_slot_bonus``
    # (``cap = 1 + bonus``), independent of the workspace's plan tier.
    allows_shared_contexts: bool = False  # Issue #271: Shared context feature (Pro only)
    features: frozenset[str] = field(default_factory=frozenset)


# Plan tier definitions
PLAN_FREE = PlanTier(
    name="free",
    display_name="S",
    price_monthly=0,
    max_contexts_per_workspace=1,
    max_members_per_workspace=1,  # Issue #229: Owner only
    max_resource_tokens=0,  # Issue #242: No resource tokens (PRO only)
    max_connectors=0,  # Issue #850: no ai-worker connectors on Free
    memory_limit=1000,
    daily_api_limit=100,  # Legacy (backward compatibility)
    weekly_api_limit=500,  # Legacy (backward compatibility)
    # Issue #238: Separated API quotas
    mcp_calls_per_day=1000,
    mcp_calls_per_week=5000,
    rest_calls_per_day=0,  # Free plan: no REST API access
    rest_calls_per_week=0,
    public_calls_per_day=0,  # Free plan: no public contexts
    public_calls_per_week=0,
    storage_limit_bytes=100 * 1024 * 1024,  # Issue #485: 100 MB
    embedding_daily_cap_usd=0.50,  # Issue #709: conservative drain-attack guard
    embedding_monthly_cap_usd=15.0,  # Issue #709
    allows_shared_contexts=False,  # Issue #271: Private contexts only
    features=frozenset({"api_keys", "oauth"}),  # Free plan includes OAuth (App Credentials)
)

PLAN_BASIC = PlanTier(
    name="basic",
    display_name="M",
    price_monthly=10,
    max_contexts_per_workspace=3,  # Limited to 3 contexts
    max_members_per_workspace=1,  # Issue #229: Owner only
    max_resource_tokens=3,  # Issue #242: Max 3 active tokens
    max_connectors=3,  # Issue #850 → Spec(2026-06-02): Basic 1→3
    allows_shared_contexts=False,  # Issue #271: Private contexts only (like Free)
    memory_limit=10000,
    daily_api_limit=2000,  # Legacy (backward compatibility)
    weekly_api_limit=10000,  # Legacy (backward compatibility)
    # Issue #238: Separated API quotas
    mcp_calls_per_day=10000,
    mcp_calls_per_week=50000,
    rest_calls_per_day=1000,
    rest_calls_per_week=5000,
    public_calls_per_day=0,  # Basic plan: no public contexts (PRO only)
    public_calls_per_week=0,
    storage_limit_bytes=1 * 1024 * 1024 * 1024,  # Issue #485: 1 GiB
    embedding_daily_cap_usd=2.0,  # Issue #709: intermediate paid-tier cap
    embedding_monthly_cap_usd=60.0,  # Issue #709
    features=frozenset({"api_keys", "reranking", "oauth"}),  # Public contexts removed (PRO only)
)

PLAN_PRO = PlanTier(
    name="pro",
    display_name="L",
    price_monthly=100,
    max_contexts_per_workspace=20,  # Issue #164: Set reasonable limit
    max_members_per_workspace=10,  # Issue #229: 10 members max for Pro plan
    max_resource_tokens=30,  # Issue #242: Max 30 active tokens
    max_connectors=10,  # Issue #850 → Spec(2026-06-02): Pro 5→10
    allows_shared_contexts=True,  # Issue #271: Shared contexts enabled
    memory_limit=100000,
    daily_api_limit=10000,  # Legacy (backward compatibility)
    weekly_api_limit=50000,  # Legacy (backward compatibility)
    # Issue #238: Separated API quotas
    mcp_calls_per_day=50000,
    mcp_calls_per_week=250000,
    rest_calls_per_day=5000,
    rest_calls_per_week=25000,
    public_calls_per_day=1000,
    public_calls_per_week=5000,
    bound_public_calls_per_minute=100,  # Issue #626: per-key bucket (PRO only)
    analysis_runs_per_day=3,  # Issue #494: Memory Broadlistening (Pro only; FREE/BASIC=0)
    storage_limit_bytes=10 * 1024 * 1024 * 1024,  # Issue #485: 10 GiB
    sleep_enabled_contexts_limit=3,  # Issue #560: Sleep mode (Pro only; FREE/BASIC=0)
    embedding_daily_cap_usd=10.0,  # Issue #709: PRO ceiling, conservative
    embedding_monthly_cap_usd=300.0,  # Issue #709
    features=frozenset(
        {
            "api_keys",
            "reranking",
            "oauth",
            "memory_agent",
            "team_invitations",  # Issue #165: Team collaboration
            "shared_contexts",  # Issue #165: Shared contexts with role-based access
            "public_contexts",  # Issue #238: Public contexts
            "memory_analysis",  # Issue #496: Memory Broadlistening
        }
    ),
)

# Plan tier registry
PLAN_TIERS: dict[str, PlanTier] = {
    PlanName.FREE: PLAN_FREE,
    PlanName.BASIC: PLAN_BASIC,
    PlanName.PRO: PLAN_PRO,
}

# Feature to minimum plan mapping
FEATURE_MIN_PLANS: dict[str, str] = {
    "api_keys": "free",
    "reranking": "basic",
    "oauth": "free",  # Free plan includes OAuth (updated from basic)
    "memory_agent": "pro",
    "team_invitations": "pro",  # Issue #165: Team collaboration requires Pro
    "shared_contexts": "pro",  # Issue #165: Shared contexts require Pro
    "public_contexts": "pro",  # Issue #242: Public contexts require PRO only
    "memory_analysis": "pro",  # Issue #496: Memory Broadlistening (Pro only; FREE/BASIC=0)
}


def _apply_settings_overrides() -> None:
    """Apply environment variable overrides to plan tiers.

    Reads override values from Settings and creates new PlanTier instances
    with overridden values where configured. Called once at module load time.

    This enables OSS deployments to customize plan limits via environment
    variables without modifying source code.
    """
    from config.settings import get_settings

    settings = get_settings()

    override_map: dict[str, dict[str, int | float | str | None]] = {
        PlanName.FREE: {
            "max_contexts_per_workspace": settings.plan_free_max_contexts,
            "memory_limit": settings.plan_free_memory_limit,
            "mcp_calls_per_day": settings.plan_free_mcp_calls_per_day,
            "storage_limit_bytes": settings.plan_free_storage_limit_bytes,
            "sleep_enabled_contexts_limit": settings.plan_free_sleep_enabled_contexts_limit,
            "embedding_daily_cap_usd": settings.plan_free_embedding_daily_cap_usd,
            "embedding_monthly_cap_usd": settings.plan_free_embedding_monthly_cap_usd,
            "display_name": settings.plan_free_display_name,
        },
        PlanName.BASIC: {
            "max_contexts_per_workspace": settings.plan_basic_max_contexts,
            "memory_limit": settings.plan_basic_memory_limit,
            "mcp_calls_per_day": settings.plan_basic_mcp_calls_per_day,
            "storage_limit_bytes": settings.plan_basic_storage_limit_bytes,
            "sleep_enabled_contexts_limit": settings.plan_basic_sleep_enabled_contexts_limit,
            "embedding_daily_cap_usd": settings.plan_basic_embedding_daily_cap_usd,
            "embedding_monthly_cap_usd": settings.plan_basic_embedding_monthly_cap_usd,
            "display_name": settings.plan_basic_display_name,
        },
        PlanName.PRO: {
            "max_contexts_per_workspace": settings.plan_pro_max_contexts,
            "memory_limit": settings.plan_pro_memory_limit,
            "mcp_calls_per_day": settings.plan_pro_mcp_calls_per_day,
            "storage_limit_bytes": settings.plan_pro_storage_limit_bytes,
            "sleep_enabled_contexts_limit": settings.plan_pro_sleep_enabled_contexts_limit,
            "embedding_daily_cap_usd": settings.plan_pro_embedding_daily_cap_usd,
            "embedding_monthly_cap_usd": settings.plan_pro_embedding_monthly_cap_usd,
            "display_name": settings.plan_pro_display_name,
        },
    }

    for plan_name, overrides in override_map.items():
        active_overrides = {k: v for k, v in overrides.items() if v is not None}
        if active_overrides:
            from dataclasses import asdict

            original = PLAN_TIERS[plan_name]
            original_dict = asdict(original)
            original_dict.update(active_overrides)
            # Reconstruct frozen dataclass with overrides
            PLAN_TIERS[plan_name] = PlanTier(**original_dict)


# Apply overrides from environment variables at import time
_apply_settings_overrides()


def get_plan_tier(plan_name: str) -> PlanTier:
    """Get plan tier by name.

    Args:
        plan_name: Plan tier name

    Returns:
        PlanTier instance (with any environment variable overrides applied)

    Raises:
        ValueError: If plan_name is invalid
    """
    if plan_name not in PLAN_TIERS:
        raise ValueError(
            f"Invalid plan tier: {plan_name}. Must be one of: {', '.join(PLAN_TIERS.keys())}"
        )
    return PLAN_TIERS[plan_name]


def get_required_plan_for_feature(feature: str) -> str:
    """Get minimum required plan for a feature.

    Args:
        feature: Feature name

    Returns:
        Minimum plan tier name

    Raises:
        ValueError: If feature is unknown
    """
    if feature not in FEATURE_MIN_PLANS:
        raise ValueError(f"Unknown feature: {feature}")
    return FEATURE_MIN_PLANS[feature]


def has_feature(plan_name: str, feature: str) -> bool:
    """Check if a plan tier includes a feature.

    Args:
        plan_name: Plan tier name
        feature: Feature name

    Returns:
        True if plan includes feature
    """
    try:
        plan = get_plan_tier(plan_name)
        return feature in plan.features
    except ValueError:
        return False
