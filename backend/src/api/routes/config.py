"""Configuration Management Routes.

Read-only view of the application configuration values.
Issue #45: Web UI Endpoint Implementation
Issue #1580: every key is env-backed — the console renders the effective value
and the write routes refuse (nothing at runtime reads ``config_overrides``).
"""

from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from auth.dependencies import AdminUser, APIKeyOrSessionUser
from config.settings import Settings, get_settings
from utils.exceptions import ConfigReadOnlyError
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/config", tags=["config"])


# ============================================================================
# Schemas
# ============================================================================


class ConfigValue(BaseModel):
    """Single configuration value."""

    key: str
    value: Any
    category: str
    description: str | None = None
    is_sensitive: bool = False
    # Issue #1580: the value is the effective one and cannot be changed here.
    read_only: bool = True


class ConfigListResponse(BaseModel):
    """Configuration list response."""

    configs: list[ConfigValue]
    total: int


class ConfigUpdateRequest(BaseModel):
    """Update configuration value."""

    value: Any


class ConfigBatchRequest(BaseModel):
    """Batch update configuration."""

    updates: dict[str, Any]


class ConfigValidateRequest(BaseModel):
    """Validate configuration."""

    key: str
    value: Any


class ConfigKeySchema(BaseModel):
    """Configuration key metadata schema.

    Issue #53 - Provide metadata for frontend display improvements.
    """

    key: str
    type: str  # "string", "number", "boolean", "enum"
    category: str
    description: str
    default_value: Any

    # ENUM専用
    enum_values: list[str] | None = None
    enum_descriptions: dict[str, str] | None = None

    # 数値専用
    min_value: float | None = None
    max_value: float | None = None

    # 表示用メタデータ
    is_sensitive: bool = False
    requires_restart: bool = False
    impact: str | None = None  # 設定の影響範囲
    examples: list[str] | None = None
    recommended: str | None = None
    documentation_url: str | None = None


# ============================================================================
# Configuration Categories
# ============================================================================


def get_config_categories() -> dict[str, list[str]]:
    """Get configuration categories and their keys."""
    return {
        "embedding": [
            "EMBEDDING_PROVIDER",
            "EMBEDDING_MODEL",
            "EMBEDDING_DIMENSIONS",
        ],
        "search": [
            "ENABLE_RERANKING",
        ],
        "system": [
            "ENVIRONMENT",
            "LOG_LEVEL",
            "CORS_ORIGINS",
            # Neural Memory Feature Flags (architectural switches)
            "ENABLE_NEURAL_MEMORY",
            "TRACK_CO_ACTIVATION",
            "ENABLE_DECAY",
            # Note: User sharding is always enabled at database schema level.
            # See neural_memory_edges table (migration 011) - user_id is mandatory.
            # This architectural decision ensures GDPR compliance.
            "ENABLE_TRUST_MODULATION",
        ],
        # Hosted-mode settings (Issue #1580): the deployment posture an operator
        # needs to inspect. Served to admins only — see get_admin_only_categories().
        "hosted": [
            "ENABLE_BYOK",
            "RESOLVE_STORED_BYOK_KEYS",
            "ENABLE_COST_DISPLAY",
            "ENABLE_PLAN_PAGE",
            "MANAGED_LLM_PROVIDER",
            "MANAGED_LLM_MODEL",
            "DEFAULT_USE_RERANK",
            "DEFAULT_RERANKER_PROVIDER",
            "DEFAULT_RERANKER_MODEL",
            "RERANK_BASE_URL",
            "RERANK_MODEL",
        ],
        # Note: All tunable Neural Memory parameters (learning_rate, top_m_edges,
        # scoring weights, gradient_clipping, etc.) are managed via /admin/neural-config (Issue #107)
    }


def get_admin_only_categories() -> set[str]:
    """Get the categories GET /config serves to admins only.

    GET /config is open to any authenticated caller. The hosted-mode category
    names the managed LLM and an internal reranker URL, which are the
    operator's business (same posture as the #991 ``ollama_base_url`` drop).
    """
    return {"hosted"}


def get_visible_categories(user: dict) -> dict[str, list[str]]:
    """Get the configuration categories ``user`` may see.

    Args:
        user: Authenticated user

    Returns:
        All categories for an admin; without the admin-only ones otherwise
    """
    categories = get_config_categories()
    if user.get("role") != "admin":
        for category in get_admin_only_categories():
            categories.pop(category, None)
    return categories


def get_sensitive_keys() -> set[str]:
    """Get list of sensitive configuration keys."""
    return {
        "JWT_SECRET_KEY",
        "GOOGLE_CLIENT_SECRET",
        "POSTGRES_PASSWORD",
        "QDRANT_API_KEY",
        "REDIS_PASSWORD",
    }


def mask_sensitive_value(key: str, value: Any) -> Any:
    """Mask sensitive configuration values."""
    if key in get_sensitive_keys():
        if isinstance(value, str) and len(value) > 8:
            return value[:4] + "*" * (len(value) - 8) + value[-4:]
        return "***MASKED***"
    return value


def get_url_keys() -> set[str]:
    """Get list of URL-valued keys that may embed credentials."""
    return {"RERANK_BASE_URL"}


def mask_url_credentials(value: Any) -> Any:
    """Mask the userinfo part of a URL (``https://user:pw@host`` → ``https://***@host``).

    Fails closed: everything between the scheme and the LAST ``@`` is masked,
    so a malformed URL cannot leak a password through a parsing quirk.
    """
    if not isinstance(value, str) or "@" not in value:
        return value
    scheme, sep, rest = value.partition("://")
    if not sep:
        scheme, rest = "", value
    return f"{scheme}{sep}***@{rest.rpartition('@')[2]}"


def get_effective_value(key: str, settings: Settings) -> Any:
    """Resolve the value the running process actually uses for ``key``.

    Args:
        key: Configuration key (env var name)
        settings: Application settings

    Returns:
        The effective value, with URL credentials masked
    """
    attr = key.lower()
    if attr in Settings.model_fields:
        value = getattr(settings, attr)
    else:
        # TRACK_CO_ACTIVATION / ENABLE_DECAY / ENABLE_TRUST_MODULATION are not
        # Settings fields: the neural layer reads them from env with its own
        # defaults, so ask it rather than echoing a possibly-unset env var.
        from neural.config import NeuralMemoryConfig

        value = getattr(NeuralMemoryConfig.from_env(), attr)

    if key in get_url_keys():
        value = mask_url_credentials(value)
    return value


# ============================================================================
# Configuration Schema (Issue #53)
# ============================================================================


def get_config_schema() -> dict[str, ConfigKeySchema]:
    """Get configuration schema with metadata for all settings.

    Returns metadata for frontend display improvements (Issue #54).
    NOTE: This is read-only metadata. Every key served by GET /config is
    env-backed, so it truthfully carries ``requires_restart=True`` (Issue #1580).
    """
    return {
        # System Settings
        "LOG_LEVEL": ConfigKeySchema(
            key="LOG_LEVEL",
            type="enum",
            category="system",
            description="Logging verbosity level",
            default_value="INFO",
            enum_values=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
            enum_descriptions={
                "DEBUG": "Detailed debug information (development only)",
                "INFO": "General informational messages (recommended)",
                "WARNING": "Warning messages for potential issues",
                "ERROR": "Error messages only",
                "CRITICAL": "Critical errors only",
            },
            requires_restart=True,
            impact="Controls log output detail. DEBUG generates large log files.",
            examples=["INFO", "DEBUG"],
            recommended="INFO (production), DEBUG (development)",
        ),
        "ENVIRONMENT": ConfigKeySchema(
            key="ENVIRONMENT",
            type="enum",
            category="system",
            description="Deployment environment",
            default_value="development",
            enum_values=["development", "staging", "production"],
            enum_descriptions={
                "development": "Local development with debug features",
                "staging": "Pre-production testing environment",
                "production": "Live production environment",
            },
            requires_restart=True,
            impact="Affects logging, error handling, and security settings",
            examples=["development", "production"],
            recommended="Match your deployment environment",
        ),
        "CORS_ORIGINS": ConfigKeySchema(
            key="CORS_ORIGINS",
            type="string",
            category="system",
            description="CORS allowed origins (comma-separated)",
            default_value="http://localhost:3000,http://localhost:8080",
            requires_restart=True,
            impact="Browsers on origins outside this list cannot call the API",
            examples=["https://app.example.com"],
            recommended="Only the origins that serve your web UI",
        ),
        # Feature Flags
        "ENABLE_NEURAL_MEMORY": ConfigKeySchema(
            key="ENABLE_NEURAL_MEMORY",
            type="boolean",
            category="system",
            description="Enable Neural Memory system (Hebbian Learning + Activation Spreading)",
            default_value=False,
            requires_restart=True,
            impact="Enables automatic memory association learning and graph-based recall enhancement",
            examples=["true", "false"],
            recommended="true (enables advanced memory features)",
        ),
        # Neural Memory - Hebbian Learning (Issue #20参照)
        "LEARNING_RATE": ConfigKeySchema(
            key="LEARNING_RATE",
            type="number",
            category="neural_memory",
            description="Hebbian learning rate (η) for synaptic weight updates",
            default_value=0.05,
            min_value=0.0,
            max_value=1.0,
            requires_restart=False,
            impact="Controls how quickly the neural graph learns new associations. Higher values adapt faster but may be unstable.",
            examples=["0.01", "0.05", "0.1"],
            recommended="0.05 (balanced learning speed and stability)",
            documentation_url="https://github.com/kagura-ai/memory-cloud/issues/20",
        ),
        "DECAY_LAMBDA": ConfigKeySchema(
            key="DECAY_LAMBDA",
            type="number",
            category="neural_memory",
            description="L2 weight decay coefficient (λ) for regularization",
            default_value=0.01,
            min_value=0.0,
            max_value=1.0,
            requires_restart=False,
            impact="Prevents weight explosion. Higher values cause faster decay of unused connections.",
            examples=["0.001", "0.01", "0.1"],
            recommended="0.01 (moderate regularization)",
            documentation_url="https://github.com/kagura-ai/memory-cloud/issues/20",
        ),
        "USAGE_WARNING_THRESHOLD": ConfigKeySchema(
            key="USAGE_WARNING_THRESHOLD",
            type="number",
            category="usage",
            description="Usage warning threshold (0.0-1.0)",
            default_value=0.80,
            min_value=0.0,
            max_value=1.0,
            requires_restart=False,
            impact="When to show usage warnings (80% = show warning at 80% usage)",
            examples=["0.80", "0.90"],
            recommended="0.80 (80%)",
        ),
        # Neural Memory - Additional Parameters (Issue #20)
        "WEIGHT_MAX": ConfigKeySchema(
            key="WEIGHT_MAX",
            type="number",
            category="neural_memory",
            description="Maximum synaptic weight value (w_max)",
            default_value=3.0,
            min_value=1.0,
            max_value=10.0,
            requires_restart=False,
            impact="Caps maximum connection strength. Higher values allow stronger associations.",
            examples=["3.0", "5.0"],
            recommended="3.0 (prevents weight saturation)",
            documentation_url="https://github.com/kagura-ai/memory-cloud/issues/20",
        ),
        "SPREAD_HOPS": ConfigKeySchema(
            key="SPREAD_HOPS",
            type="number",
            category="neural_memory",
            description="Activation spreading hop count",
            default_value=1,
            min_value=1,
            max_value=3,
            requires_restart=False,
            impact="How many graph hops to propagate activation. Higher = broader recall, slower.",
            examples=["1", "2", "3"],
            recommended="1 (fast), 2 (balanced), 3 (comprehensive)",
            documentation_url="https://github.com/kagura-ai/memory-cloud/issues/20",
        ),
        "ALPHA": ConfigKeySchema(
            key="ALPHA",
            type="number",
            category="neural_memory",
            description="Unified Scoring: Semantic similarity weight (α)",
            default_value=0.55,
            min_value=0.0,
            max_value=1.0,
            requires_restart=False,
            impact="Weight for embedding-based semantic similarity in final score. Higher = prioritize semantic match.",
            examples=["0.50", "0.55", "0.60"],
            recommended="0.55 (balanced with graph)",
            documentation_url="https://github.com/kagura-ai/memory-cloud/issues/20",
        ),
        "BETA": ConfigKeySchema(
            key="BETA",
            type="number",
            category="neural_memory",
            description="Unified Scoring: Graph association weight (β)",
            default_value=0.20,
            min_value=0.0,
            max_value=1.0,
            requires_restart=False,
            impact="Weight for neural graph connections. Higher = prioritize related memories.",
            examples=["0.15", "0.20", "0.25"],
            recommended="0.20 (moderate graph influence)",
            documentation_url="https://github.com/kagura-ai/memory-cloud/issues/20",
        ),
        "GAMMA": ConfigKeySchema(
            key="GAMMA",
            type="number",
            category="neural_memory",
            description="Unified Scoring: Recency weight (γ)",
            default_value=0.10,
            min_value=0.0,
            max_value=1.0,
            requires_restart=False,
            impact="Weight for recency in scoring. Higher = prioritize recent memories.",
            examples=["0.05", "0.10", "0.15"],
            recommended="0.10 (moderate recency boost)",
            documentation_url="https://github.com/kagura-ai/memory-cloud/issues/20",
        ),
        "DELTA": ConfigKeySchema(
            key="DELTA",
            type="number",
            category="neural_memory",
            description="Unified Scoring: Importance weight (δ)",
            default_value=0.10,
            min_value=0.0,
            max_value=1.0,
            requires_restart=False,
            impact="Weight for importance score. Higher = prioritize high-importance memories.",
            examples=["0.05", "0.10", "0.15"],
            recommended="0.10 (balanced importance)",
            documentation_url="https://github.com/kagura-ai/memory-cloud/issues/20",
        ),
        "EPSILON": ConfigKeySchema(
            key="EPSILON",
            type="number",
            category="neural_memory",
            description="Unified Scoring: Trust/confidence weight (ε)",
            default_value=0.05,
            min_value=0.0,
            max_value=1.0,
            requires_restart=False,
            impact="Weight for trust modulation. Higher = prioritize verified memories.",
            examples=["0.03", "0.05", "0.10"],
            recommended="0.05 (light trust boost)",
            documentation_url="https://github.com/kagura-ai/memory-cloud/issues/20",
        ),
        "ZETA": ConfigKeySchema(
            key="ZETA",
            type="number",
            category="neural_memory",
            description="Unified Scoring: Redundancy penalty weight (ζ) - MMR",
            default_value=0.25,
            min_value=0.0,
            max_value=1.0,
            requires_restart=False,
            impact="Weight for diversity (Maximal Marginal Relevance). Higher = more diverse results.",
            examples=["0.20", "0.25", "0.30"],
            recommended="0.25 (balanced diversity)",
            documentation_url="https://github.com/kagura-ai/memory-cloud/issues/20",
        ),
        "TOP_M_EDGES": ConfigKeySchema(
            key="TOP_M_EDGES",
            type="number",
            category="neural_memory",
            description="Keep top-M strongest edges per node",
            default_value=32,
            min_value=8,
            max_value=128,
            requires_restart=False,
            impact="Limits edges per node. Higher = more connections but slower graph operations.",
            examples=["16", "32", "64"],
            recommended="32 (balanced performance)",
            documentation_url="https://github.com/kagura-ai/memory-cloud/issues/20",
        ),
        "SPREAD_DECAY": ConfigKeySchema(
            key="SPREAD_DECAY",
            type="number",
            category="neural_memory",
            description="Decay factor for each activation spreading hop",
            default_value=0.6,
            min_value=0.0,
            max_value=1.0,
            requires_restart=False,
            impact="How much activation decreases per hop. Lower = faster decay, more focused recall.",
            examples=["0.5", "0.6", "0.7"],
            recommended="0.6 (moderate spread)",
            documentation_url="https://github.com/kagura-ai/memory-cloud/issues/20",
        ),
        "SPREAD_THRESHOLD": ConfigKeySchema(
            key="SPREAD_THRESHOLD",
            type="number",
            category="neural_memory",
            description="Minimum activation to continue spreading",
            default_value=0.01,
            min_value=0.0,
            max_value=1.0,
            requires_restart=False,
            impact="Stops spreading when activation falls below threshold. Higher = fewer hops.",
            examples=["0.01", "0.05", "0.1"],
            recommended="0.01 (thorough exploration)",
            documentation_url="https://github.com/kagura-ai/memory-cloud/issues/20",
        ),
        "RECENCY_TAU_DAYS": ConfigKeySchema(
            key="RECENCY_TAU_DAYS",
            type="number",
            category="neural_memory",
            description="Time constant for recency decay (days)",
            default_value=14.0,
            min_value=1.0,
            max_value=365.0,
            requires_restart=False,
            impact="How quickly recency score decays. Higher = slower decay, longer-lasting recency boost.",
            examples=["7.0", "14.0", "30.0"],
            recommended="14.0 (2 weeks half-life)",
            documentation_url="https://github.com/kagura-ai/memory-cloud/issues/20",
        ),
        "IMPORTANCE_EMA_ALPHA": ConfigKeySchema(
            key="IMPORTANCE_EMA_ALPHA",
            type="number",
            category="neural_memory",
            description="EMA smoothing for importance updates",
            default_value=0.3,
            min_value=0.0,
            max_value=1.0,
            requires_restart=False,
            impact="Exponential moving average smoothing. Higher = faster adaptation to new importance.",
            examples=["0.2", "0.3", "0.5"],
            recommended="0.3 (smooth updates)",
            documentation_url="https://github.com/kagura-ai/memory-cloud/issues/20",
        ),
        "TRACK_CO_ACTIVATION": ConfigKeySchema(
            key="TRACK_CO_ACTIVATION",
            type="boolean",
            category="system",
            description="Enable co-activation tracking",
            default_value=True,
            requires_restart=True,
            impact="Learns which memories are accessed together. Enables context-aware recall.",
            examples=["true", "false"],
            recommended="true (enables association learning)",
            documentation_url="https://github.com/kagura-ai/memory-cloud/issues/20",
        ),
        "CO_ACTIVATION_WINDOW": ConfigKeySchema(
            key="CO_ACTIVATION_WINDOW",
            type="number",
            category="neural_memory",
            description="Time window for same-session co-activation (seconds)",
            default_value=300,
            min_value=60,
            max_value=3600,
            requires_restart=False,
            impact="Memories accessed within this window are considered related. Higher = looser association.",
            examples=["180", "300", "600"],
            recommended="300 (5 minutes)",
            documentation_url="https://github.com/kagura-ai/memory-cloud/issues/20",
        ),
        "MIN_CO_ACTIVATION_COUNT": ConfigKeySchema(
            key="MIN_CO_ACTIVATION_COUNT",
            type="number",
            category="neural_memory",
            description="Minimum co-activation count to create/strengthen edge",
            default_value=2,
            min_value=1,
            max_value=10,
            requires_restart=False,
            impact="How many times memories must be accessed together to form a connection.",
            examples=["2", "3", "5"],
            recommended="2 (quick learning)",
            documentation_url="https://github.com/kagura-ai/memory-cloud/issues/20",
        ),
        "ENABLE_DECAY": ConfigKeySchema(
            key="ENABLE_DECAY",
            type="boolean",
            category="system",
            description="Enable automatic edge weight decay",
            default_value=True,
            requires_restart=True,
            impact="Gradually weakens unused connections (forgetting). Prevents stale associations.",
            examples=["true", "false"],
            recommended="true (enables forgetting)",
            documentation_url="https://github.com/kagura-ai/memory-cloud/issues/20",
        ),
        "DECAY_BACKGROUND_INTERVAL": ConfigKeySchema(
            key="DECAY_BACKGROUND_INTERVAL",
            type="number",
            category="neural_memory",
            description="Background decay task interval (seconds)",
            default_value=3600,
            min_value=300,
            max_value=86400,
            requires_restart=False,
            impact="How often to run weight decay. Lower = fresher decay but more CPU.",
            examples=["1800", "3600", "7200"],
            recommended="3600 (hourly)",
            documentation_url="https://github.com/kagura-ai/memory-cloud/issues/20",
        ),
        "DECAY_RATE": ConfigKeySchema(
            key="DECAY_RATE",
            type="number",
            category="neural_memory",
            description="Exponential decay rate",
            default_value=0.001,
            min_value=0.0,
            max_value=1.0,
            requires_restart=False,
            impact="Rate of weight decay over time. Higher = faster forgetting.",
            examples=["0.001", "0.01", "0.1"],
            recommended="0.001 (slow forgetting)",
            documentation_url="https://github.com/kagura-ai/memory-cloud/issues/20",
        ),
        "PRUNE_THRESHOLD": ConfigKeySchema(
            key="PRUNE_THRESHOLD",
            type="number",
            category="neural_memory",
            description="Remove edges below this weight",
            default_value=0.05,
            min_value=0.0,
            max_value=1.0,
            requires_restart=False,
            impact="Edges weaker than this are deleted. Higher = more aggressive pruning.",
            examples=["0.05", "0.1", "0.2"],
            recommended="0.05 (moderate pruning)",
            documentation_url="https://github.com/kagura-ai/memory-cloud/issues/20",
        ),
        "CONSOLIDATION_USE_COUNT_MIN": ConfigKeySchema(
            key="CONSOLIDATION_USE_COUNT_MIN",
            type="number",
            category="neural_memory",
            description="Minimum use_count for Working → Persistent promotion",
            default_value=3,
            min_value=1,
            max_value=20,
            requires_restart=False,
            impact="How many times a memory must be accessed to become persistent.",
            examples=["2", "3", "5"],
            recommended="3 (moderate promotion)",
            documentation_url="https://github.com/kagura-ai/memory-cloud/issues/20",
        ),
        "CONSOLIDATION_IMPORTANCE_MIN": ConfigKeySchema(
            key="CONSOLIDATION_IMPORTANCE_MIN",
            type="number",
            category="neural_memory",
            description="Minimum importance for promotion",
            default_value=0.65,
            min_value=0.0,
            max_value=1.0,
            requires_restart=False,
            impact="Importance threshold for promotion. Higher = only high-value memories persist.",
            examples=["0.5", "0.65", "0.8"],
            recommended="0.65 (quality filter)",
            documentation_url="https://github.com/kagura-ai/memory-cloud/issues/20",
        ),
        "CONSOLIDATION_DIVERSITY_MIN": ConfigKeySchema(
            key="CONSOLIDATION_DIVERSITY_MIN",
            type="number",
            category="neural_memory",
            description="Minimum diversity for promotion",
            default_value=0.2,
            min_value=0.0,
            max_value=1.0,
            requires_restart=False,
            impact="Diversity threshold to avoid redundant persistent memories.",
            examples=["0.1", "0.2", "0.3"],
            recommended="0.2 (avoid duplicates)",
            documentation_url="https://github.com/kagura-ai/memory-cloud/issues/20",
        ),
        "ENABLE_TRUST_MODULATION": ConfigKeySchema(
            key="ENABLE_TRUST_MODULATION",
            type="boolean",
            category="system",
            description="Modulate learning by confidence",
            default_value=True,
            requires_restart=True,
            impact="Adjusts learning rate based on confidence. Low confidence = slower learning.",
            examples=["true", "false"],
            recommended="true (stable learning)",
            documentation_url="https://github.com/kagura-ai/memory-cloud/issues/20",
        ),
        # Note: GRADIENT_CLIPPING, BATCH_UPDATE_SIZE, ASYNC_UPDATE_DELAY_MS,
        # MAX_CANDIDATES_K and other neural parameters moved to /admin/neural-config (Issue #107)
        # Search Configuration
        "ENABLE_RERANKING": ConfigKeySchema(
            key="ENABLE_RERANKING",
            type="boolean",
            category="search",
            description=(
                "Enable AI reranking for search results (BYOK Voyage/Cohere, or the "
                "self_hosted reranker). When false no context reranks (#1572)."
            ),
            default_value=True,
            requires_restart=True,
            impact="Improves search accuracy with AI reranking. Adds latency and cost per query.",
            examples=["true", "false"],
            recommended="true (if reranker API key is configured)",
        ),
        # Embedding Configuration
        "EMBEDDING_PROVIDER": ConfigKeySchema(
            key="EMBEDDING_PROVIDER",
            type="enum",
            category="embedding",
            description="Embedding model provider",
            default_value="openai",
            enum_values=["openai", "cohere", "huggingface", "self_hosted"],
            enum_descriptions={
                "openai": "OpenAI text-embedding-3-small (recommended)",
                "cohere": "Cohere embed-multilingual-v3.0",
                "huggingface": "Hugging Face embedding models",
                "self_hosted": "Self-hosted OpenAI-compatible (Ollama, vLLM)",
            },
            requires_restart=True,
            impact="Changes embedding provider for all new memories",
            examples=["openai"],
            recommended="openai (best quality)",
        ),
        "EMBEDDING_MODEL": ConfigKeySchema(
            key="EMBEDDING_MODEL",
            type="string",
            category="embedding",
            description="Embedding model name",
            default_value="text-embedding-3-small",
            requires_restart=True,
            impact="Specific model to use for embeddings. Must match provider.",
            examples=["text-embedding-3-small", "text-embedding-3-large"],
            recommended="text-embedding-3-small (cost-effective)",
        ),
        "EMBEDDING_DIMENSIONS": ConfigKeySchema(
            key="EMBEDDING_DIMENSIONS",
            type="number",
            category="embedding",
            description="Embedding vector dimensions",
            default_value=512,
            min_value=128,
            max_value=3072,
            requires_restart=True,
            impact="Vector size. Higher = more accurate but slower and more storage.",
            examples=["512", "1536"],
            recommended="512 (balanced)",
        ),
        # Hosted-mode settings (Issue #1580)
        "ENABLE_BYOK": ConfigKeySchema(
            key="ENABLE_BYOK",
            type="boolean",
            category="hosted",
            description="Enable BYOK (bring-your-own-key) provisioning (#1167)",
            default_value=True,
            requires_restart=True,
            impact=(
                "When false the external-keys write paths, the workspace cost dashboard and "
                "the OpenAI key-status probe return 404 and the web UI hides their nav entries."
            ),
            examples=["true", "false"],
        ),
        "RESOLVE_STORED_BYOK_KEYS": ConfigKeySchema(
            key="RESOLVE_STORED_BYOK_KEYS",
            type="boolean",
            category="hosted",
            description="Resolve stored BYOK keys in the LLM / embedding / reranker services (#1569)",
            default_value=True,
            requires_restart=True,
            impact=(
                "When false the services ignore stored external API keys and use the platform "
                "credential only. Requires ENABLE_BYOK=false."
            ),
            examples=["true", "false"],
        ),
        "ENABLE_COST_DISPLAY": ConfigKeySchema(
            key="ENABLE_COST_DISPLAY",
            type="boolean",
            category="hosted",
            description="Show money to workspace users (#1571)",
            default_value=True,
            requires_restart=True,
            impact=(
                "When false the workspace cost dashboard answers 404, analysis cost fields are "
                "null and the web UI hides the cost surfaces. The admin cost view is unaffected."
            ),
            examples=["true", "false"],
        ),
        "ENABLE_PLAN_PAGE": ConfigKeySchema(
            key="ENABLE_PLAN_PAGE",
            type="boolean",
            category="hosted",
            description="Enable the workspace Plan page and its sidebar entry (#1145)",
            default_value=False,
            requires_restart=True,
            impact="Only makes sense where billing is wired up.",
            examples=["true", "false"],
        ),
        "MANAGED_LLM_PROVIDER": ConfigKeySchema(
            key="MANAGED_LLM_PROVIDER",
            type="string",
            category="hosted",
            description="Provider of the platform-managed LLM lane (#1569)",
            default_value="",
            requires_restart=True,
            impact=(
                "Memory Analysis runs on this provider for plans with the managed_llm feature "
                "and no BYOK key; Sleep's judge defaults to it unless SLEEP_LLM_* is set. "
                "Empty = no managed lane."
            ),
            examples=["openai", "anthropic", "gemini", "self_hosted"],
        ),
        "MANAGED_LLM_MODEL": ConfigKeySchema(
            key="MANAGED_LLM_MODEL",
            type="string",
            category="hosted",
            description="Model id sent to MANAGED_LLM_PROVIDER (#1569)",
            default_value="",
            requires_restart=True,
            impact="Required when MANAGED_LLM_PROVIDER is set.",
        ),
        "DEFAULT_USE_RERANK": ConfigKeySchema(
            key="DEFAULT_USE_RERANK",
            type="boolean",
            category="hosted",
            description="use_rerank written to new context search configs (#1572)",
            default_value=False,
            requires_restart=True,
            impact="Applies to contexts created from now on; existing contexts are never rewritten.",
            examples=["true", "false"],
        ),
        "DEFAULT_RERANKER_PROVIDER": ConfigKeySchema(
            key="DEFAULT_RERANKER_PROVIDER",
            type="enum",
            category="hosted",
            description="Reranker provider written to new context search configs (#1572)",
            default_value="voyage",
            enum_values=["voyage", "cohere", "self_hosted"],
            enum_descriptions={
                "voyage": "Voyage AI (needs a BYOK key per workspace)",
                "cohere": "Cohere (needs a BYOK key per workspace)",
                "self_hosted": "Keyless (RERANK_BASE_URL or SELF_HOSTED_BASE_URL)",
            },
            requires_restart=True,
            impact="Applies to contexts created from now on; existing contexts are never rewritten.",
        ),
        "DEFAULT_RERANKER_MODEL": ConfigKeySchema(
            key="DEFAULT_RERANKER_MODEL",
            type="string",
            category="hosted",
            description="reranker_model written to new context search configs (#1572)",
            default_value="",
            requires_restart=True,
            impact="Empty = the provider's default model.",
        ),
        "RERANK_BASE_URL": ConfigKeySchema(
            key="RERANK_BASE_URL",
            type="string",
            category="hosted",
            description="OpenAI/Jina-style /v1/rerank endpoint for the self_hosted reranker",
            default_value="",
            requires_restart=True,
            impact=(
                "When set the self_hosted reranker posts one batched /v1/rerank request here. "
                "Empty = prompt scoring on SELF_HOSTED_BASE_URL. Embedded credentials are masked."
            ),
        ),
        "RERANK_MODEL": ConfigKeySchema(
            key="RERANK_MODEL",
            type="string",
            category="hosted",
            description="Served model name for the /v1/rerank endpoint (RERANK_BASE_URL)",
            default_value="qwen3-reranker-0.6b",
            requires_restart=True,
            impact="Only consulted when RERANK_BASE_URL is set.",
        ),
    }


# ============================================================================
# Endpoints
# ============================================================================


@router.get("", response_model=ConfigListResponse)
async def get_all_config(
    user: APIKeyOrSessionUser,
    mask_sensitive: bool = True,
):
    """Get all configuration values.

    Every value is the EFFECTIVE one the running process uses (Issue #1580);
    ``config_overrides`` rows are never consulted — nothing reads them at
    runtime, so showing one would display a value that is not in effect.
    Admin-only categories are omitted for non-admin callers.

    Args:
        user: Authenticated user
        mask_sensitive: Whether to mask sensitive values (URL credentials are
            masked regardless)

    Returns:
        List of configuration values
    """
    try:
        settings = get_settings()
        categories = get_visible_categories(user)

        configs = []

        for category, keys in categories.items():
            for key in keys:
                value = get_effective_value(key, settings)

                # Mask sensitive values
                if mask_sensitive:
                    value = mask_sensitive_value(key, value)

                configs.append(
                    ConfigValue(
                        key=key,
                        value=value,
                        category=category,
                        is_sensitive=(key in get_sensitive_keys()),
                    )
                )

        logger.info("config_list_retrieved", user_id=user["user_id"], count=len(configs))

        return ConfigListResponse(configs=configs, total=len(configs))

    except Exception as e:
        logger.error(f"get_all_config_failed: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve configuration",
        ) from e


@router.get("/categories")
async def get_categories(
    user: APIKeyOrSessionUser,
):
    """Get configuration categories.

    Args:
        user: Authenticated user

    Returns:
        Configuration categories
    """
    return {"categories": get_visible_categories(user)}


_READ_ONLY_RESPONSE: dict[int | str, dict[str, Any]] = {
    409: {
        "description": (
            "Always: configuration is read-only (`CFG-002`). Values are set via "
            "environment variables and applied on restart/redeploy."
        )
    }
}


@router.put("/{key}", responses=_READ_ONLY_RESPONSE)
async def update_config(
    key: str,
    request: ConfigUpdateRequest,
    admin: AdminUser,
):
    """Refuse to update a configuration value (Issue #1580).

    Admin-only endpoint, kept so existing clients get a clear answer. Every key
    is env-backed: it is set via environment variables and applied on
    restart/redeploy, and no runtime consumer reads a stored override.

    Args:
        key: Configuration key the caller tried to update
        request: Rejected value
        admin: Authenticated admin user

    Raises:
        ConfigReadOnlyError: Always (409 ``CFG-002``)
    """
    logger.info("config_write_refused", keys=[key], admin_user_id=admin.get("user_id"))
    raise ConfigReadOnlyError(keys=[key])


@router.post("/batch", responses=_READ_ONLY_RESPONSE)
async def batch_update_config(
    request: ConfigBatchRequest,
    admin: AdminUser,
):
    """Refuse to batch update configuration values (Issue #1580).

    Admin-only endpoint. The whole request is refused — there are no partial
    writes. See ``update_config``.

    Args:
        request: Rejected key-value pairs
        admin: Authenticated admin user

    Raises:
        ConfigReadOnlyError: Always (409 ``CFG-002``)
    """
    keys = sorted(request.updates)
    logger.info("config_write_refused", keys=keys, admin_user_id=admin.get("user_id"))
    raise ConfigReadOnlyError(keys=keys)


@router.post("/validate")
async def validate_config(
    request: ConfigValidateRequest,
    user: APIKeyOrSessionUser,
):
    """Validate a configuration value.

    Args:
        request: Key and value to validate
        user: Authenticated user

    Returns:
        Validation result
    """
    try:
        # Basic validation
        valid = True
        errors = []

        # Validate based on key type
        if request.key == "EMBEDDING_PROVIDER":
            if request.value not in ["openai", "cohere", "huggingface", "self_hosted"]:
                valid = False
                errors.append("Must be one of: openai, cohere, huggingface, self_hosted")

        elif request.key == "EMBEDDING_DIMENSIONS":
            if not isinstance(request.value, int) or request.value <= 0:
                valid = False
                errors.append("Must be a positive integer")

        elif request.key.endswith("_RATE"):
            if (
                not isinstance(request.value, (int, float))
                or request.value < 0
                or request.value > 1
            ):
                valid = False
                errors.append("Must be a number between 0 and 1")

        logger.info(f"config_validated: key={request.key}, valid={valid}, user={user['user_id']}")

        return {"valid": valid, "errors": errors if not valid else []}

    except Exception as e:
        logger.error(f"validate_config_failed: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to validate configuration",
        ) from e


@router.get("/schema")
async def get_schema():
    """Get configuration schema with metadata.

    Returns metadata for all configuration keys including:
    - Type (string/number/boolean/enum)
    - Valid values for ENUM types
    - Min/max ranges for numeric types
    - Descriptions, impacts, examples, recommendations
    - Restart requirements

    Issue #53 - Enable frontend display improvements (read-only)

    Returns:
        Dictionary of configuration key schemas
    """
    try:
        schema = get_config_schema()
        logger.info("config_schema_retrieved", key_count=len(schema))
        return schema

    except Exception as e:
        logger.error(f"get_config_schema_failed: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve configuration schema",
        ) from e
