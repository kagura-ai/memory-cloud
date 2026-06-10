"""Neural Config Admin API Routes.

Admin-only endpoints for managing Neural Memory configuration.
Issue #107: Move neural config from env vars to database with admin UI
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import require_admin
from db.base import get_db
from models.api_base import TZAwareBaseModel
from models.neural import NeuralConfig
from neural.config import NeuralMemoryConfig
from utils import db_transaction, get_user_id
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/admin/neural-config", tags=["neural-config"])


# ============================================================================
# Schemas
# ============================================================================


class NeuralConfigItem(TZAwareBaseModel):
    """Neural config item response."""

    key: str
    value: str
    value_type: str
    category: str
    description: str | None
    min_value: float | None
    max_value: float | None
    updated_at: datetime


class NeuralConfigListResponse(BaseModel):
    """List of neural config items."""

    configs: list[NeuralConfigItem]
    categories: list[str]
    total: int


class NeuralConfigUpdateRequest(BaseModel):
    """Update neural config request."""

    value: str


class NeuralConfigUpdateResponse(BaseModel):
    """Update neural config response."""

    key: str
    old_value: str
    new_value: str
    message: str


class NeuralConfigResetResponse(BaseModel):
    """Reset neural config response."""

    message: str
    reset_count: int


# ============================================================================
# Default Values (for reset)
# ============================================================================

DEFAULT_VALUES: dict[str, str] = {
    # Hebbian Learning
    "top_m_edges": "8",
    "learning_rate": "0.05",
    "decay_lambda": "0.01",
    "weight_max": "3.0",
    # Activation Spreading
    "spread_hops": "1",
    "spread_decay": "0.6",
    "spread_threshold": "0.01",
    # Scoring Weights
    "alpha": "0.55",
    "beta": "0.20",
    "gamma": "0.10",
    "delta": "0.10",
    "epsilon": "0.05",
    "zeta": "0.25",
    # Temporal
    "recency_tau_days": "14.0",
    "importance_ema_alpha": "0.3",
    # Decay
    "decay_rate": "0.001",  # DEPRECATED (Issue #970): unused; see hebbian_decay_half_life_days
    "hebbian_decay_half_life_days": "14.0",  # Issue #970: Hebbian edge half-life (days)
    "prune_threshold": "0.01",
    "decay_background_interval": "3600",
    # Co-Activation
    "co_activation_window": "300",
    "min_co_activation_count": "2",
    # Consolidation
    "consolidation_use_count_min": "3",
    "consolidation_importance_min": "0.65",
    "consolidation_diversity_min": "0.2",
    # Performance
    "batch_update_size": "100",
    "max_candidates_k": "64",
    "async_update_delay_ms": "2000",
    # Security
    "gradient_clipping": "0.5",
    # Sleep Maintenance (DB-configurable params only)
    "sleep_llm_provider": "openai",
    "sleep_llm_model": "gpt-5-nano",
    "sleep_max_memories_per_run": "200",
    "sleep_max_llm_calls_per_run": "50",
    "sleep_dedup_enabled": "true",
    "sleep_dedup_similarity_threshold": "0.92",
    "sleep_edge_discovery_enabled": "true",
    "sleep_edge_discovery_sample_size": "30",
    "sleep_importance_reeval_enabled": "true",
}


# ============================================================================
# Endpoints
# ============================================================================


@router.get("", response_model=NeuralConfigListResponse)
async def list_neural_config(
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    category: str | None = None,
) -> NeuralConfigListResponse:
    """List all neural config parameters.

    Admin-only endpoint.

    Args:
        admin: Authenticated admin user
        db: Database session
        category: Optional filter by category

    Returns:
        List of neural config items grouped by category
    """
    async with db_transaction(db, "list_neural_config", "Failed to list neural config"):
        query = select(NeuralConfig).order_by(NeuralConfig.category, NeuralConfig.key)

        if category:
            query = query.where(NeuralConfig.category == category)

        result = await db.execute(query)
        configs = list(result.scalars().all())

        # Get unique categories
        categories_result = await db.execute(
            select(NeuralConfig.category).distinct().order_by(NeuralConfig.category)
        )
        categories = [row[0] for row in categories_result.all()]

        config_items = [
            NeuralConfigItem(
                key=c.key,
                value=c.value,
                value_type=c.value_type,
                category=c.category,
                description=c.description,
                min_value=c.min_value,
                max_value=c.max_value,
                updated_at=c.updated_at,
            )
            for c in configs
        ]

        logger.info("neural_config_listed", count=len(configs), admin=get_user_id(admin))

        return NeuralConfigListResponse(
            configs=config_items,
            categories=categories,
            total=len(configs),
        )


@router.get("/{key}", response_model=NeuralConfigItem)
async def get_neural_config(
    key: str,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> NeuralConfigItem:
    """Get a specific neural config parameter.

    Admin-only endpoint.
    """
    async with db_transaction(db, "get_neural_config", "Failed to get neural config"):
        result = await db.execute(select(NeuralConfig).where(NeuralConfig.key == key))
        config = result.scalar_one_or_none()

        if not config:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Config key '{key}' not found",
            )

        return NeuralConfigItem(
            key=config.key,
            value=config.value,
            value_type=config.value_type,
            category=config.category,
            description=config.description,
            min_value=config.min_value,
            max_value=config.max_value,
            updated_at=config.updated_at,
        )


@router.put("/{key}", response_model=NeuralConfigUpdateResponse)
async def update_neural_config(
    key: str,
    request: NeuralConfigUpdateRequest,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> NeuralConfigUpdateResponse:
    """Update a neural config parameter.

    Admin-only endpoint. Validates value against min/max constraints.
    """
    admin_id = get_user_id(admin)

    async with db_transaction(db, "update_neural_config", "Failed to update neural config"):
        result = await db.execute(select(NeuralConfig).where(NeuralConfig.key == key))
        config = result.scalar_one_or_none()

        if not config:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Config key '{key}' not found",
            )

        # Validate new value
        is_valid, error = config.validate_value(request.value)
        if not is_valid:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error,
            )

        old_value = config.value
        config.value = request.value

        try:
            await db.commit()
        except IntegrityError as e:
            await db.rollback()
            logger.error("neural_config_update_integrity_error", key=key, error=str(e))
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Concurrent update conflict for config key '{key}'. Please retry.",
            ) from None

        # Invalidate cache so new config is loaded
        NeuralMemoryConfig.invalidate_cache()

        logger.info(
            "neural_config_updated",
            key=key,
            old_value=old_value,
            new_value=request.value,
            admin=admin_id,
        )

        return NeuralConfigUpdateResponse(
            key=key,
            old_value=old_value,
            new_value=request.value,
            message=f"Config '{key}' updated successfully",
        )


@router.post("/reset", response_model=NeuralConfigResetResponse)
async def reset_neural_config(
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> NeuralConfigResetResponse:
    """Reset all neural config parameters to default values.

    Admin-only endpoint.
    """
    admin_id = get_user_id(admin)

    async with db_transaction(db, "reset_neural_config", "Failed to reset neural config"):
        result = await db.execute(select(NeuralConfig))
        configs = list(result.scalars().all())

        reset_count = 0
        for config in configs:
            if config.key in DEFAULT_VALUES:
                default = DEFAULT_VALUES[config.key]
                if config.value != default:
                    config.value = default
                    reset_count += 1

        try:
            await db.commit()
        except IntegrityError as e:
            await db.rollback()
            logger.error("neural_config_reset_integrity_error", error=str(e))
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Concurrent update conflict during reset. Please retry.",
            ) from None

        # Invalidate cache so new config is loaded
        NeuralMemoryConfig.invalidate_cache()

        logger.info(
            "neural_config_reset",
            reset_count=reset_count,
            admin=admin_id,
        )

        return NeuralConfigResetResponse(
            message=f"Reset {reset_count} config values to defaults",
            reset_count=reset_count,
        )
