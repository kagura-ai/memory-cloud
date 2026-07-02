"""Configuration Management Routes.

Manage application configuration values.
Issue #45: Web UI Endpoint Implementation
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import AdminUser, APIKeyOrSessionUser
from config.settings import get_settings
from db.base import get_db
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
        # Note: All tunable Neural Memory parameters (learning_rate, top_m_edges,
        # scoring weights, gradient_clipping, etc.) are managed via /admin/neural-config (Issue #107)
    }


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


# ============================================================================
# Configuration Schema (Issue #53)
# ============================================================================


def get_config_schema() -> dict[str, ConfigKeySchema]:
    """Get configuration schema with metadata for all settings.

    Returns metadata for frontend display improvements (Issue #54).
    NOTE: This is read-only metadata - actual config updates not implemented.
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
        # Feature Flags
        "ENABLE_NEURAL_MEMORY": ConfigKeySchema(
            key="ENABLE_NEURAL_MEMORY",
            type="boolean",
            category="system",
            description="Enable Neural Memory system (Hebbian Learning + Activation Spreading)",
            default_value=True,
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
            requires_restart=False,
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
            requires_restart=False,
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
            requires_restart=False,
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
            description="Enable AI reranking for search results (Cohere or Voyage provider)",
            default_value=False,
            requires_restart=False,
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
    }


# ============================================================================
# Endpoints
# ============================================================================


@router.get("", response_model=ConfigListResponse)
async def get_all_config(
    user: APIKeyOrSessionUser,
    db: AsyncSession = Depends(get_db),
    mask_sensitive: bool = True,
):
    """Get all configuration values.

    Args:
        user: Authenticated user
        db: Database session
        mask_sensitive: Whether to mask sensitive values

    Returns:
        List of configuration values
    """
    try:
        import os

        from sqlalchemy import select

        from models.config import ConfigOverride

        settings = get_settings()
        categories = get_config_categories()

        # Load DB overrides
        overrides_result = await db.execute(select(ConfigOverride))
        db_overrides = {o.key: o.value for o in overrides_result.scalars().all()}

        configs = []

        for category, keys in categories.items():
            for key in keys:
                # DB override takes priority over env var
                if key in db_overrides:
                    value = db_overrides[key]
                else:
                    value = getattr(settings, key.lower(), os.getenv(key))

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

        logger.info(f"config_list_retrieved: user={user['user_id']}, count={len(configs)}")

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
    return {"categories": get_config_categories()}


@router.put("/{key}")
async def update_config(
    key: str,
    request: ConfigUpdateRequest,
    admin: AdminUser,
    db: AsyncSession = Depends(get_db),
):
    """Update a single configuration value.

    Admin-only endpoint. Updates are stored in database (for runtime config)
    or require .env.cloud file modification (for env vars).

    Args:
        key: Configuration key to update
        request: New value
        admin: Authenticated admin user
        db: Database session

    Returns:
        Success message with updated value
    """
    try:
        from sqlalchemy import select

        from models.config import ConfigOverride

        # Upsert: update if exists, create if not
        result = await db.execute(select(ConfigOverride).where(ConfigOverride.key == key))
        override = result.scalar_one_or_none()

        value_str = str(request.value) if request.value is not None else ""

        if override:
            override.value = value_str
            override.updated_by = admin.get("email", "unknown")
        else:
            override = ConfigOverride(
                key=key, value=value_str, updated_by=admin.get("email", "unknown")
            )
            db.add(override)

        await db.commit()

        logger.info(f"config_updated: key={key}, admin={admin.get('email', 'unknown')}")

        return {
            "message": f"Configuration '{key}' updated successfully",
            "key": key,
            "value": mask_sensitive_value(key, request.value),
        }

    except Exception as e:
        await db.rollback()
        logger.error(f"update_config_failed: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update configuration",
        ) from e


@router.post("/batch")
async def batch_update_config(
    request: ConfigBatchRequest,
    admin: AdminUser,
    db: AsyncSession = Depends(get_db),
):
    """Batch update multiple configuration values.

    Admin-only endpoint.

    Args:
        request: Dictionary of key-value pairs to update
        admin: Authenticated admin user
        db: Database session

    Returns:
        Success message with update count
    """
    try:
        from sqlalchemy import select

        from models.config import ConfigOverride

        for key, value in request.updates.items():
            result = await db.execute(select(ConfigOverride).where(ConfigOverride.key == key))
            override = result.scalar_one_or_none()
            value_str = str(value) if value is not None else ""

            if override:
                override.value = value_str
                override.updated_by = admin.get("email", "unknown")
            else:
                db.add(
                    ConfigOverride(
                        key=key, value=value_str, updated_by=admin.get("email", "unknown")
                    )
                )

        await db.commit()

        logger.info(
            f"config_batch_updated: count={len(request.updates)}, admin={admin.get('email', 'unknown')}"
        )

        return {
            "message": f"{len(request.updates)} configuration values updated",
            "updated_keys": list(request.updates.keys()),
        }

    except Exception as e:
        await db.rollback()
        logger.error(f"batch_update_config_failed: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to batch update configuration",
        ) from e


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
