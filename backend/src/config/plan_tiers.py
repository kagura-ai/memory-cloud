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
    # Issue #1548: XL ("Pro Max"). The key is the contract with the billing
    # service and is immutable once shipped. Spelled without an underscore so
    # the env override prefix stays unambiguous: ``PLAN_PRO_MAX_CONTEXTS``
    # already means "pro tier, max_contexts" (a ``pro_max`` key would yield
    # ``PLAN_PRO_MAX_MAX_CONTEXTS`` next to it).
    PROMAX = "promax"


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
    analysis_runs_per_day: int = 0  # Issue #494: Memory Analysis runs/day
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
    # Free plan includes OAuth (App Credentials); secret_store is
    # available on every tier (#1128 — zero-knowledge, all tiers).
    features=frozenset({"api_keys", "oauth", "secret_store"}),
)

PLAN_BASIC = PlanTier(
    name="basic",
    display_name="M",
    price_monthly=10,
    max_contexts_per_workspace=3,  # Limited to 3 contexts
    max_members_per_workspace=1,  # Issue #229: Owner only
    # serve-only: existing objects; creation is feature-gated (#1551)
    max_resource_tokens=3,  # Issue #242: Max 3 active tokens
    # serve-only: existing objects; creation is feature-gated (#1551)
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
    # Issue #1030: paid tiers (M/L) get platform-managed embeddings — they may
    # embed on the platform key without BYOK (bounded by the #709/#1033 cap).
    features=frozenset({"api_keys", "reranking", "oauth", "managed_embeddings", "secret_store"}),
)

PLAN_PRO = PlanTier(
    name="pro",
    display_name="L",
    price_monthly=100,
    max_contexts_per_workspace=20,  # Issue #164: Set reasonable limit
    max_members_per_workspace=10,  # Issue #229: 10 members max for Pro plan
    # serve-only: existing objects; creation is feature-gated (#1551)
    max_resource_tokens=30,  # Issue #242: Max 30 active tokens
    # serve-only: existing objects; creation is feature-gated (#1551)
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
    # serve-only: existing objects; creation is feature-gated (#1551)
    public_calls_per_day=1000,
    public_calls_per_week=5000,
    # serve-only: existing objects; creation is feature-gated (#1551)
    bound_public_calls_per_minute=100,  # Issue #626: per-key bucket
    analysis_runs_per_day=3,  # Issue #494: Memory Analysis (Pro only; FREE/BASIC=0)
    storage_limit_bytes=10 * 1024 * 1024 * 1024,  # Issue #485: 10 GiB
    sleep_enabled_contexts_limit=3,  # Issue #560: Sleep mode (Pro only; FREE/BASIC=0)
    embedding_daily_cap_usd=10.0,  # Issue #709: PRO ceiling, conservative
    embedding_monthly_cap_usd=300.0,  # Issue #709
    # Issue #1551: ``public_contexts`` moved to XL. Existing L public contexts
    # keep serving on the caps above; only *making* a context public is gated.
    features=frozenset(
        {
            "api_keys",
            "reranking",
            "oauth",
            "team_invitations",  # Issue #165: Team collaboration
            "shared_contexts",  # Issue #165: Shared contexts with role-based access
            "memory_analysis",  # Issue #496: Memory Analysis
            "managed_embeddings",  # Issue #1030: platform-managed embeddings (M/L)
            "secret_store",  # Issue #1128: zero-knowledge secret store (all tiers)
        }
    ),
)

PLAN_PROMAX = PlanTier(
    name="promax",
    display_name="XL",
    # Legacy USD field served by the plan endpoints. Pricing does not live in
    # this repo (#1096 / #1141) — the plan key is the whole contract with the
    # billing service — so XL carries a placeholder, not a price.
    price_monthly=0,
    max_contexts_per_workspace=1000,  # Issue #1547 matrix
    max_members_per_workspace=50,  # Issue #1547 matrix
    # Issue #1551: final XL seat counts — the only tier that may *create*
    # resource tokens / connectors, so these are the real creation caps.
    max_resource_tokens=150,
    max_connectors=50,
    allows_shared_contexts=True,
    memory_limit=100000,  # Issue #1547 matrix
    daily_api_limit=50000,  # Legacy (backward compatibility)
    weekly_api_limit=250000,  # Legacy (backward compatibility)
    mcp_calls_per_day=250000,
    mcp_calls_per_week=1250000,
    rest_calls_per_day=25000,
    rest_calls_per_week=125000,
    public_calls_per_day=5000,
    public_calls_per_week=25000,
    bound_public_calls_per_minute=500,
    analysis_runs_per_day=15,
    storage_limit_bytes=50 * 1024 * 1024 * 1024,  # 50 GiB
    sleep_enabled_contexts_limit=15,
    embedding_daily_cap_usd=50.0,
    embedding_monthly_cap_usd=1500.0,
    # Issue #1551: XL-only "may create" features. ``resources`` gates
    # setup_resource / resource-token creation, ``connectors`` gates
    # setup_connector, ``public_contexts`` gates set_public and bound public
    # keys. Lower tiers keep serving what they already have.
    features=PLAN_PRO.features | {"resources", "connectors", "public_contexts"},
)

# Plan tier registry. Insertion order IS the upgrade order (free → ... → promax):
# the plan endpoints, downgrade targets and ``plan_rank`` all rely on it.
PLAN_TIERS: dict[str, PlanTier] = {
    PlanName.FREE: PLAN_FREE,
    PlanName.BASIC: PLAN_BASIC,
    PlanName.PRO: PLAN_PRO,
    PlanName.PROMAX: PLAN_PROMAX,
}

# Lowest → highest tier, derived from the registry so a new tier is added in
# exactly one place (#1548). Prefer ``plan_rank`` / ``plan_at_least`` over
# hardcoded name lists or ``== "pro"`` checks that mean "pro or better".
PLAN_ORDER: tuple[str, ...] = tuple(PLAN_TIERS)


def plan_rank(plan_name: str | None) -> int:
    """Position of ``plan_name`` in the upgrade order (0 = lowest tier).

    Unknown or missing names rank as the lowest tier — the fail-closed
    fallback every existing ``... if name in order else 0`` site used.
    """
    try:
        return PLAN_ORDER.index(plan_name)  # type: ignore[arg-type]
    except ValueError:
        return 0


def plan_at_least(plan_name: str | None, minimum: str) -> bool:
    """True when ``plan_name`` is ``minimum`` or a higher tier."""
    return plan_rank(plan_name) >= plan_rank(minimum)


# Feature to minimum plan mapping
FEATURE_MIN_PLANS: dict[str, str] = {
    "api_keys": "free",
    "reranking": "basic",
    "oauth": "free",  # Free plan includes OAuth (updated from basic)
    "team_invitations": "pro",  # Issue #165: Team collaboration requires Pro
    "shared_contexts": "pro",  # Issue #165: Shared contexts require Pro
    "public_contexts": "promax",  # Issue #1551: making a context public is XL-only
    "memory_analysis": "pro",  # Issue #496: Memory Analysis (Pro only; FREE/BASIC=0)
    "managed_embeddings": "basic",  # Issue #1030: platform-managed embeddings (M/L; FREE=BYOK/self-hosted)
    "resources": "promax",  # Issue #1551: setup_resource / new resource tokens are XL-only
    "connectors": "promax",  # Issue #1551: setup_connector is XL-only
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
        PlanName.PROMAX: {
            "max_contexts_per_workspace": settings.plan_promax_max_contexts,
            "memory_limit": settings.plan_promax_memory_limit,
            "mcp_calls_per_day": settings.plan_promax_mcp_calls_per_day,
            "storage_limit_bytes": settings.plan_promax_storage_limit_bytes,
            "sleep_enabled_contexts_limit": settings.plan_promax_sleep_enabled_contexts_limit,
            "embedding_daily_cap_usd": settings.plan_promax_embedding_daily_cap_usd,
            "embedding_monthly_cap_usd": settings.plan_promax_embedding_monthly_cap_usd,
            "display_name": settings.plan_promax_display_name,
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


def required_plan_display_name(feature: str) -> str:
    """Display name of the lowest tier that includes ``feature``.

    Falls back to ``"higher"`` for an unknown feature so refusal text never
    500s on a typo — the fallback ``QuotaService.check_feature_access`` has
    always used.
    """
    try:
        return PLAN_TIERS[get_required_plan_for_feature(feature)].display_name
    except (ValueError, KeyError):
        return "higher"


def feature_denied_message(plan_name: str | None, feature: str) -> str:
    """Refusal text for a plan that lacks ``feature`` (#1551).

    Names the minimum tier from the registry — never a hardcoded "Pro" — so a
    display-name override or a re-mapped feature flows through every gate.
    ``None`` reads as free: legacy rows pre-dating the ``plan_name`` backfill.
    """
    return (
        f"Feature '{feature}' not available on {plan_name or PlanName.FREE} plan. "
        f"Upgrade to {required_plan_display_name(feature)} plan to access this feature."
    )
