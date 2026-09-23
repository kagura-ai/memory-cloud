"""Plan Tier Definitions for Kagura Memory Cloud.

Issue #149: Plan tier enforcement (Free/Basic/Pro)

Defines quota limits and feature access for each plan tier.
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from config.constants import GATE_PLAN, GATE_QUOTA
from utils.logger import get_logger

if TYPE_CHECKING:
    from config.settings import Settings

logger = get_logger(__name__)

# Constants
UNLIMITED_CONTEXTS = 999999  # Effectively unlimited
# Issue #1613: CANDIDATES for delete/disable protection, not the rule itself. A
# candidate is protected only while something reads it — see
# services/external_key_protection.is_key_protected.
PROTECTED_KEYS = frozenset(["OPENAI_API_KEY"])


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

    Issue #276 (updated by Issue #661 / #675 / #1550): the user-owned
    workspace cap is per-user — ``cap = 1 + users.workspace_slot_bonus +
    owned_workspace_grant`` — where the grant is taken from the HIGHEST tier
    among the workspaces the user owns (``utils.plan_resolver``). The tier
    therefore grants slots but never caps a user on its own; every other
    field here controls per-workspace features (memory_limit, api_quota,
    etc.). Joined workspaces (via invite) are uncapped.

    Attributes:
        name: Plan tier name ('free', 'basic', 'pro')
        display_name: Human-readable name
        price_monthly: Monthly price in USD
        max_contexts_per_workspace: Maximum contexts per workspace
        max_members_per_workspace: Maximum members per workspace (Issue #229)
        owned_workspace_grant: Extra owned-workspace slots this tier grants
            its owner on top of the per-user base (1) and slot bonus
            (Issue #1550: free 0 / basic 0 / pro 2 / promax 19 → 1/1/3/20).
        max_resource_tokens: Maximum active resource tokens (Issue #242)
        max_connectors: Maximum ai-worker chat-ingest connectors per
            workspace (Issue #850, F6-a of #755). A SEPARATE seat cap from
            ``max_resource_tokens`` — connector-owned resource tokens minted
            by the F6-b setup flow bypass the ``max_resource_tokens`` Pro+
            gate and are governed by this seat count instead. 0 disables
            connector creation for the plan.
        memory_limit: Maximum memories per workspace
        memories_per_day: Memories that may be CREATED per workspace per UTC
            day (Issue #1549, epic #1547). Enforced on every path that writes
            a user-visible memory row (see
            ``QuotaService.check_memories_per_day`` for the exact list);
            updates, Sleep and context merges are not charged. ``0`` is the
            zero-floor value like every other quota field (#569): the tier
            cannot create memories at all — it never means "unlimited".
            Self-hosters who want no cap set a huge value via
            ``PLAN_<KEY>_MEMORIES_PER_DAY``.
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
        allows_shared_contexts: Whether plan allows shared (non-private) contexts
            (Issue #271). Mirrors ``"shared_contexts" in features`` — the
            shared-context gates read this boolean, so a ``features`` override
            (#1559) re-derives it.
        features: Set of enabled features. Replaceable per tier via
            ``PLAN_<KEY>_FEATURES`` (comma-separated, #1559); names must be in
            ``KNOWN_FEATURES``.
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
    memories_per_day: int = 0  # Issue #1549: memories created per UTC day (0 = none, not unlimited)
    # Issue #709: Per-workspace BYOK embedding spend cap (USD). ``None`` means
    # "no tier-default cap" — uncapped unless an admin sets a per-workspace
    # override. ``Workspace.embedding_*_cap_usd`` (when set) takes precedence
    # over these tier defaults; see ``Workspace.effective_embedding_*_cap_usd``.
    embedding_daily_cap_usd: float | None = None
    embedding_monthly_cap_usd: float | None = None
    # Issue #661's ``max_owned_workspaces`` field was removed in #675 (the cap
    # became per-user: ``1 + users.workspace_slot_bonus``). #1550 re-links the
    # tier as a slot GRANT, not a cap: the highest owned tier adds this many
    # slots. A user above the cap keeps every workspace — only create is gated.
    owned_workspace_grant: int = 0
    allows_shared_contexts: bool = False  # Issue #271: Shared context feature (Pro only)
    features: frozenset[str] = field(default_factory=frozenset)


# Plan tier definitions
PLAN_FREE = PlanTier(
    name="free",
    display_name="S",
    price_monthly=0,
    max_contexts_per_workspace=1,
    max_members_per_workspace=1,  # Issue #229: Owner only
    owned_workspace_grant=0,  # Issue #1550: base slot only → owns 1
    max_resource_tokens=0,  # Issue #242: No resource tokens (PRO only)
    max_connectors=0,  # Issue #850: no ai-worker connectors on Free
    memory_limit=1000,
    memories_per_day=50,  # Issue #1549 (#1547 matrix)
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
    owned_workspace_grant=0,  # Issue #1550: same as Free → owns 1
    # serve-only: existing objects; creation is feature-gated (#1551)
    max_resource_tokens=3,  # Issue #242: Max 3 active tokens
    # serve-only: existing objects; creation is feature-gated (#1551)
    max_connectors=3,  # Issue #850 → Spec(2026-06-02): Basic 1→3
    allows_shared_contexts=False,  # Issue #271: Private contexts only (like Free)
    memory_limit=10000,
    memories_per_day=300,  # Issue #1549 (#1547 matrix)
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
    owned_workspace_grant=2,  # Issue #1550: 1 base + 2 → owns 3
    # serve-only: existing objects; creation is feature-gated (#1551)
    max_resource_tokens=30,  # Issue #242: Max 30 active tokens
    # serve-only: existing objects; creation is feature-gated (#1551)
    max_connectors=10,  # Issue #850 → Spec(2026-06-02): Pro 5→10
    allows_shared_contexts=True,  # Issue #271: Shared contexts enabled
    memory_limit=100000,
    memories_per_day=2000,  # Issue #1549 (#1547 matrix)
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
            "managed_llm",  # Issue #1569: Analysis on the platform-managed LLM lane (no BYOK)
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
    owned_workspace_grant=19,  # Issue #1550: 1 base + 19 → owns 20
    # Issue #1551: final XL seat counts — the only tier that may *create*
    # resource tokens / connectors, so these are the real creation caps.
    max_resource_tokens=150,
    max_connectors=50,
    allows_shared_contexts=True,
    memory_limit=100000,  # Issue #1547 matrix
    memories_per_day=10000,  # Issue #1549 (#1547 matrix)
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


# Feature to minimum plan mapping.
#
# A row here is NOT by itself evidence of a runtime gate: it says which tier
# the feature belongs to, not that anything refuses a tier without it. What
# each entry actually does at runtime is declared in ``FEATURE_ENFORCEMENT``
# below (#1648) — read the two together.
FEATURE_MIN_PLANS: dict[str, str] = {
    "api_keys": "free",
    "reranking": "basic",
    "oauth": "free",  # Free plan includes OAuth (updated from basic)
    "team_invitations": "pro",  # Issue #165: Team collaboration requires Pro
    "shared_contexts": "pro",  # Issue #165: Shared contexts require Pro
    "public_contexts": "promax",  # Issue #1551: making a context public is XL-only
    "memory_analysis": "pro",  # Issue #496: Memory Analysis (Pro only; FREE/BASIC=0)
    "managed_embeddings": "basic",  # Issue #1030: platform-managed embeddings (M/L; FREE=BYOK/self-hosted)
    "managed_llm": "pro",  # Issue #1569: Memory Analysis on the platform-managed LLM lane (L/XL)
    "resources": "promax",  # Issue #1551: setup_resource / new resource tokens are XL-only
    "connectors": "promax",  # Issue #1551: setup_connector is XL-only
}

# Every feature name the registry knows (#1559): the code-default tiers'
# ``features`` plus the matrix keys — ``secret_store`` is on every tier and so
# has no matrix row. Computed BEFORE the env overrides run, so it is the
# vocabulary a ``PLAN_<KEY>_FEATURES`` value is validated against.
KNOWN_FEATURES: frozenset[str] = frozenset(FEATURE_MIN_PLANS).union(
    *(tier.features for tier in PLAN_TIERS.values())
)

# ============================================================================
# Feature enforcement modes (#1648)
# ============================================================================


class FeatureEnforcement(StrEnum):
    """How a ``FEATURE_MIN_PLANS`` entry behaves at RUNTIME (#1648).

    ``FEATURE_MIN_PLANS`` above says which tier a feature belongs to. It says
    nothing about whether anything checks — three entries were display-only
    when this enum was added. The mode makes that explicit so a reader (and the
    web UI, which receives it on the tier matrix) can tell a gate from a label.

    Members:
        ENFORCED: A runtime check REFUSES the request on a tier without the
            feature (``FEAT-001`` / ``plan_required`` / a raised error), on
            every deployment. This is the only mode a client may hard-disable
            a control on.
        CONDITIONAL: A runtime check refuses, but only where a deployment
            setting turns it on; with that setting at its default the tier
            without the feature is served anyway. A client must NOT hard-gate
            on this mode — it would refuse what this deployment allows.
        DEGRADES: A runtime check exists but the request still SUCCEEDS with
            reduced behaviour; nothing is refused.
        ADVERTISED: No runtime check at all. The entry exists so the plan
            pages can list the feature; every tier behaves the same.
    """

    ENFORCED = "enforced"
    CONDITIONAL = "conditional"
    DEGRADES = "degrades"
    ADVERTISED = "advertised"


@dataclass(frozen=True)
class FeatureGate:
    """One feature's enforcement mode plus the human reason for it (#1648).

    Attributes:
        mode: The ``FeatureEnforcement`` member.
        note: Where the gate lives (or why there is none), for the reader who
            is deciding whether a UI may hard-disable a control.
    """

    mode: FeatureEnforcement
    note: str


# What each FEATURE_MIN_PLANS entry actually does at runtime (#1648).
#
# Keep in lock-step with FEATURE_MIN_PLANS / KNOWN_FEATURES: every known
# feature needs a row here, and ``tests/config/test_feature_enforcement.py``
# scans ``backend/src`` to fail when a declared mode and the call sites drift
# apart. Changing a mode is a DOCUMENTATION change — add or remove the gate
# in the same PR, never the label alone.
FEATURE_ENFORCEMENT: dict[str, FeatureGate] = {
    "api_keys": FeatureGate(
        FeatureEnforcement.ADVERTISED,
        "No gate: every tier may create API keys. Listed for the plan pages only.",
    ),
    "oauth": FeatureGate(
        FeatureEnforcement.ADVERTISED,
        "No gate: OAuth login/clients work on every tier. Listed for the plan pages only.",
    ),
    "secret_store": FeatureGate(
        FeatureEnforcement.ADVERTISED,
        "No gate, and an invariant every tier must keep (#1128) — the row can never be false.",
    ),
    "reranking": FeatureGate(
        FeatureEnforcement.DEGRADES,
        "services/search_service.py checks with raise_on_denied=False: recall still "
        "returns results, just unreranked, and logs reranking_disabled_by_plan_tier.",
    ),
    "team_invitations": FeatureGate(
        FeatureEnforcement.ENFORCED,
        "api/routes/invitations.py refuses to create an invitation (#165).",
    ),
    "shared_contexts": FeatureGate(
        FeatureEnforcement.ENFORCED,
        "services/context_service.py and api/routes/contexts.py refuse a non-private "
        "visibility (#165).",
    ),
    "public_contexts": FeatureGate(
        FeatureEnforcement.ENFORCED,
        "api/routes/contexts.py, api/routes/member_credentials.py and the set_public "
        "MCP tool refuse to publish a context or mint a bound public key (#1551).",
    ),
    "memory_analysis": FeatureGate(
        FeatureEnforcement.ENFORCED,
        "auth/analysis_gates.py refuses the analysis run with 403 (#496).",
    ),
    "managed_embeddings": FeatureGate(
        FeatureEnforcement.CONDITIONAL,
        "services/embedding_service.py refuses the platform-key fallback (#1030), but "
        "only where EMBEDDING_PLATFORM_FALLBACK_REQUIRES_MANAGED_PLAN is on — it "
        "defaults to off (platform_fallback_allowed returns True for every tier), so a "
        "default deployment embeds on the platform key regardless of tier.",
    ),
    "managed_llm": FeatureGate(
        FeatureEnforcement.ENFORCED,
        "services/analysis/llm_lane.py raises VAL-001 when neither BYOK nor this "
        "feature resolves a lane (#1569).",
    ),
    "resources": FeatureGate(
        FeatureEnforcement.ENFORCED,
        "api/routes/resource_tokens.py and the setup_resource MCP tool refuse "
        "provisioning (#1551).",
    ),
    "connectors": FeatureGate(
        FeatureEnforcement.ENFORCED,
        "services/connector_provisioning.py refuses setup_connector (#1551).",
    ),
}


def _parse_features_override(plan_name: str, raw: str) -> frozenset[str]:
    """Parse and validate one ``PLAN_<KEY>_FEATURES`` value (#1559).

    Comma-separated and whitespace-tolerant; the result REPLACES the tier's
    ``features``. Refused at import (``ValueError``) when a name is not in
    ``KNOWN_FEATURES`` or the set breaks a registry invariant: every tier
    carries ``secret_store`` (#1128), and ``resources`` implies
    ``public_contexts`` because ``setup_resource`` inserts a *public* context.

    Args:
        plan_name: Tier key (``free`` / ``basic`` / ``pro`` / ``promax``).
        raw: The env value as read by Settings.

    Returns:
        The validated feature set.

    Raises:
        ValueError: Unknown feature name(s) or a violated invariant, naming
            the env var so the operator can find the line to fix.
    """
    env_name = f"PLAN_{plan_name.upper()}_FEATURES"
    features = frozenset(name.strip() for name in raw.split(",") if name.strip())
    unknown = sorted(features - KNOWN_FEATURES)
    if unknown:
        raise ValueError(
            f"{env_name}: unknown feature(s) {', '.join(unknown)}. "
            f"Known features: {', '.join(sorted(KNOWN_FEATURES))}"
        )
    if "secret_store" not in features:
        raise ValueError(
            f"{env_name} must include 'secret_store' — the zero-knowledge secret "
            "store is available on every tier (#1128)."
        )
    if "resources" in features and "public_contexts" not in features:
        raise ValueError(
            f"{env_name}: 'resources' requires 'public_contexts' — setup_resource "
            "creates a public context, so a tier that may create resources must "
            "also be allowed to make contexts public."
        )
    return features


def _derive_feature_min_plans(tiers: dict[str, PlanTier]) -> dict[str, str]:
    """Lowest tier in ``PLAN_ORDER`` that carries each feature of ``tiers``.

    A feature on no tier gets no entry, so ``get_required_plan_for_feature``
    raises for it, ``required_plan_name`` returns ``None`` and the refusal
    text falls back to "higher".
    """
    min_plans: dict[str, str] = {}
    for plan_name in PLAN_ORDER:
        for feature in sorted(tiers[plan_name].features):
            min_plans.setdefault(feature, plan_name)
    return min_plans


def _apply_settings_overrides(settings: "Settings | None" = None) -> None:
    """Apply environment variable overrides to plan tiers.

    Reads override values from Settings and creates new PlanTier instances
    with overridden values where configured. Called once at module load time.

    This enables OSS deployments to customize plan limits via environment
    variables without modifying source code. ``PLAN_<KEY>_FEATURES`` (#1559)
    replaces a tier's feature set; when any tier's features are overridden,
    ``FEATURE_MIN_PLANS`` is re-derived from the EFFECTIVE tiers so the
    refusal text names the right minimum tier.

    Args:
        settings: Settings to read from. ``None`` (the import-time call) uses
            the process-global instance; tests pass one built in-process.

    Raises:
        ValueError: A ``PLAN_<KEY>_FEATURES`` value names an unknown feature
            or violates a registry invariant (see ``_parse_features_override``).
    """
    if settings is None:
        from config.settings import get_settings

        settings = get_settings()

    override_map: dict[str, dict[str, int | float | str | None]] = {
        PlanName.FREE: {
            "max_contexts_per_workspace": settings.plan_free_max_contexts,
            "memory_limit": settings.plan_free_memory_limit,
            "mcp_calls_per_day": settings.plan_free_mcp_calls_per_day,
            "storage_limit_bytes": settings.plan_free_storage_limit_bytes,
            "sleep_enabled_contexts_limit": settings.plan_free_sleep_enabled_contexts_limit,
            "memories_per_day": settings.plan_free_memories_per_day,
            "embedding_daily_cap_usd": settings.plan_free_embedding_daily_cap_usd,
            "embedding_monthly_cap_usd": settings.plan_free_embedding_monthly_cap_usd,
            "owned_workspace_grant": settings.plan_free_owned_workspace_grant,
            "display_name": settings.plan_free_display_name,
            "features": settings.plan_free_features,
        },
        PlanName.BASIC: {
            "max_contexts_per_workspace": settings.plan_basic_max_contexts,
            "memory_limit": settings.plan_basic_memory_limit,
            "mcp_calls_per_day": settings.plan_basic_mcp_calls_per_day,
            "storage_limit_bytes": settings.plan_basic_storage_limit_bytes,
            "sleep_enabled_contexts_limit": settings.plan_basic_sleep_enabled_contexts_limit,
            "memories_per_day": settings.plan_basic_memories_per_day,
            "embedding_daily_cap_usd": settings.plan_basic_embedding_daily_cap_usd,
            "embedding_monthly_cap_usd": settings.plan_basic_embedding_monthly_cap_usd,
            "owned_workspace_grant": settings.plan_basic_owned_workspace_grant,
            "display_name": settings.plan_basic_display_name,
            "features": settings.plan_basic_features,
        },
        PlanName.PRO: {
            "max_contexts_per_workspace": settings.plan_pro_max_contexts,
            "memory_limit": settings.plan_pro_memory_limit,
            "mcp_calls_per_day": settings.plan_pro_mcp_calls_per_day,
            "storage_limit_bytes": settings.plan_pro_storage_limit_bytes,
            "sleep_enabled_contexts_limit": settings.plan_pro_sleep_enabled_contexts_limit,
            "memories_per_day": settings.plan_pro_memories_per_day,
            "embedding_daily_cap_usd": settings.plan_pro_embedding_daily_cap_usd,
            "embedding_monthly_cap_usd": settings.plan_pro_embedding_monthly_cap_usd,
            "owned_workspace_grant": settings.plan_pro_owned_workspace_grant,
            "display_name": settings.plan_pro_display_name,
            "features": settings.plan_pro_features,
        },
        PlanName.PROMAX: {
            "max_contexts_per_workspace": settings.plan_promax_max_contexts,
            "memory_limit": settings.plan_promax_memory_limit,
            "mcp_calls_per_day": settings.plan_promax_mcp_calls_per_day,
            "storage_limit_bytes": settings.plan_promax_storage_limit_bytes,
            "sleep_enabled_contexts_limit": settings.plan_promax_sleep_enabled_contexts_limit,
            "memories_per_day": settings.plan_promax_memories_per_day,
            "embedding_daily_cap_usd": settings.plan_promax_embedding_daily_cap_usd,
            "embedding_monthly_cap_usd": settings.plan_promax_embedding_monthly_cap_usd,
            "owned_workspace_grant": settings.plan_promax_owned_workspace_grant,
            "display_name": settings.plan_promax_display_name,
            "features": settings.plan_promax_features,
        },
    }

    # Validate every tier's features BEFORE mutating the registry, so a
    # rejected override leaves the process with the untouched code defaults
    # (the ValueError aborts the import either way).
    features_by_plan: dict[str, frozenset[str]] = {}
    for plan_name, overrides in override_map.items():
        raw_features = overrides.pop("features")
        if isinstance(raw_features, str) and raw_features.strip():
            features_by_plan[plan_name] = _parse_features_override(plan_name, raw_features)

    for plan_name, overrides in override_map.items():
        active_overrides: dict[str, object] = {k: v for k, v in overrides.items() if v is not None}
        if plan_name in features_by_plan:
            features = features_by_plan[plan_name]
            active_overrides["features"] = features
            # The shared-context gates read this boolean, not the feature set —
            # keep the two in lock-step.
            active_overrides["allows_shared_contexts"] = "shared_contexts" in features
        if active_overrides:
            from dataclasses import asdict

            original = PLAN_TIERS[plan_name]
            original_dict = asdict(original)
            original_dict.update(active_overrides)
            # Reconstruct frozen dataclass with overrides
            PLAN_TIERS[plan_name] = PlanTier(**original_dict)

    if features_by_plan:
        # In place: ``feature_denied_message`` / ``get_required_plan_for_feature``
        # read the module global, and the matrix must follow the EFFECTIVE tiers.
        FEATURE_MIN_PLANS.clear()
        FEATURE_MIN_PLANS.update(_derive_feature_min_plans(PLAN_TIERS))

    logger.info(
        "plan_tier_features_effective",
        overridden=sorted(features_by_plan, key=plan_rank),
        **{plan_name: sorted(PLAN_TIERS[plan_name].features) for plan_name in PLAN_ORDER},
    )


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


def required_plan_name(feature: str) -> str | None:
    """Minimum plan tier name for ``feature``, or ``None`` when no tier has it.

    Non-raising twin of ``get_required_plan_for_feature`` for the gates that
    put the tier name in an error envelope (#1559): a ``PLAN_<KEY>_FEATURES``
    override that drops a feature from every tier leaves it without a
    ``FEATURE_MIN_PLANS`` row, and a refusal must never turn into a 500.

    Args:
        feature: Feature name

    Returns:
        Minimum plan tier name, or ``None`` if no tier carries the feature.
    """
    return FEATURE_MIN_PLANS.get(feature)


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


def feature_enforcement(feature: str) -> FeatureEnforcement:
    """Runtime enforcement mode of ``feature`` (#1648).

    Args:
        feature: Feature name.

    Returns:
        The declared ``FeatureEnforcement``. An unknown name reads as
        ``ADVERTISED`` — the fail-soft answer, because a caller that cannot
        prove a gate exists must not hard-disable a control over it.
    """
    gate = FEATURE_ENFORCEMENT.get(feature)
    return gate.mode if gate else FeatureEnforcement.ADVERTISED


def feature_enforcement_modes() -> dict[str, str]:
    """Every known feature's enforcement mode as plain strings (#1648).

    The shape the plan endpoints put on the wire. Tier-independent: the mode
    is a property of the CODE, so it does not follow a ``PLAN_<KEY>_FEATURES``
    override the way ``FEATURE_MIN_PLANS`` does.

    Returns:
        ``{feature: "enforced" | "conditional" | "degrades" | "advertised"}``,
        key-sorted.
    """
    return {name: gate.mode.value for name, gate in sorted(FEATURE_ENFORCEMENT.items())}


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


def plan_display_name(plan_name: str | None) -> str:
    """Display name of ``plan_name`` — the label the UI shows for that tier.

    ``None`` / empty reads as free: legacy rows pre-dating the ``plan_name``
    backfill. A key the registry does not know is returned unchanged rather
    than relabelled as a tier the workspace is not on.
    """
    tier = PLAN_TIERS.get(plan_name or PlanName.FREE)
    return tier.display_name if tier else str(plan_name)


def feature_denied_message(plan_name: str | None, feature: str) -> str:
    """Refusal text for a plan that lacks ``feature`` (#1551).

    Names the minimum tier from the registry — never a hardcoded "Pro" — so a
    display-name override or a re-mapped feature flows through every gate.
    Both tiers read as display names (#1583): "basic plan … L plan" mixed the
    raw key with the label the UI shows.
    """
    return (
        f"Feature '{feature}' not available on {plan_display_name(plan_name)} plan. "
        f"Upgrade to {required_plan_display_name(feature)} plan to access this feature."
    )


# ============================================================================
# Gate details builders (#1644)
# ============================================================================


def lowest_tier_with_limit(attr: str, above: int) -> str | None:
    """Lowest tier in ``PLAN_ORDER`` whose numeric ``attr`` exceeds ``above``.

    The numeric twin of ``required_plan_name`` (#1644): caps like contexts,
    members and resource tokens are not registry FEATURES, so the "which tier
    fixes this?" answer has to be derived from the tier rows.

    Args:
        attr: ``PlanTier`` field name holding the cap, e.g.
            ``"max_contexts_per_workspace"``.
        above: The cap the caller is refusing at. A tier qualifies only when
            its own value is strictly greater.

    Returns:
        The tier key, or ``None`` when no tier raises the cap — the refusal
        then carries no upgrade path, which is the correct answer for an
        env-driven cap (e.g. ``max_agents_per_workspace``).
    """
    for name in PLAN_ORDER:
        if int(getattr(PLAN_TIERS[name], attr, 0) or 0) > above:
            return name
    return None


def feature_gate_details(plan_name: str | None, feature: str) -> dict[str, Any]:
    """The ``details`` block for a ``FEAT-001`` plan refusal (#1644).

    Both halves ship: ``required_plan`` is the registry KEY a client decides
    with, ``required_plan_display`` the label a non-UI client renders. The
    display is ``None`` — never the ``"higher"`` prose fallback
    ``required_plan_display_name`` returns — because ``"higher"`` is a
    sentence fragment, not a tier label.

    Args:
        plan_name: The workspace's plan key; ``None`` when there is no row.
        feature: Registry feature key, e.g. ``"team_invitations"``.

    Returns:
        ``gate`` / ``feature`` / ``required_plan`` / ``required_plan_display``
        / ``current_plan``, ready to splat into the exception.
    """
    required = required_plan_name(feature)
    tier = PLAN_TIERS.get(required) if required else None
    return {
        "gate": GATE_PLAN,
        "feature": feature,
        "required_plan": required,
        "required_plan_display": tier.display_name if tier else None,
        "current_plan": plan_name,
    }


def quota_gate_details(
    plan_name: str | None,
    quota_type: str,
    *,
    current: int,
    limit: int,
    required_plan: str | None = None,
    feature: str | None = None,
    resets_at: str | None = None,
) -> dict[str, Any]:
    """The ``details`` block for a ``QUOTA-001`` refusal (#1644).

    ``current`` / ``limit`` are the canonical count names. They are ADDED
    beside whatever per-site legacy names the refusal already carried
    (``used_today``, ``owned_count``, ``max_connectors``, ...); nothing is
    renamed, so an older client keeps reading what it always read.

    Args:
        plan_name: The workspace's plan key.
        quota_type: A member of ``constants.QUOTA_TYPES``.
        current: Count already used, as an int.
        limit: The cap that was hit, as an int.
        required_plan: Tier key that raises the cap, usually from
            ``lowest_tier_with_limit``. ``None`` when no tier does — the
            refusal then advertises no upgrade.
        feature: Registry feature key when the cap belongs to a named
            feature.
        resets_at: ISO-8601 instant; time-windowed quotas only.

    Returns:
        The details mapping, ready to splat into ``QuotaExceededError``.
    """
    required_tier = PLAN_TIERS.get(required_plan) if required_plan else None
    return {
        "gate": GATE_QUOTA,
        "quota_type": quota_type,
        "current": int(current),
        "limit": int(limit),
        "required_plan": required_plan,
        "required_plan_display": required_tier.display_name if required_tier else None,
        "current_plan": plan_name,
        "feature": feature,
        "resets_at": resets_at,
    }
