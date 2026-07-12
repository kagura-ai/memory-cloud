"""Repository for Context Search Configuration.

This module provides database access for context-level search and reranker settings.
Issue #130: Context-scoped Search & Reranker Settings UI
Issue #1220: Per-context router calibration store (stage 4)
"""

from datetime import datetime  # noqa: TC003 - runtime annotation in upsert()
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from models.config import (
    ROUTER_CALIBRATION_SOURCE_FROZEN,
    ContextSearchConfig,
    RouterCalibration,
)
from models.schemas import ContextSearchConfigUpdate
from utils.logger import get_logger

logger = get_logger(__name__)


class ContextSearchConfigRepository:
    """Repository for managing context search configurations.

    Provides CRUD operations for context-specific hybrid search and reranker settings.
    """

    def __init__(self, db: AsyncSession):
        """Initialize repository with database session.

        Args:
            db: Async SQLAlchemy session
        """
        self.db = db

    async def get_by_context(self, context_id: UUID) -> ContextSearchConfig | None:
        """Get search configuration for a context.

        Args:
            context_id: Context UUID

        Returns:
            ContextSearchConfig if exists, None otherwise
        """
        result = await self.db.execute(
            select(ContextSearchConfig).where(ContextSearchConfig.context_id == context_id)
        )
        config = result.scalar_one_or_none()

        if config:
            logger.debug(
                "config_retrieved",
                context_id=str(context_id),
                semantic_weight=float(config.semantic_weight),
                fetch_factor=config.fetch_factor,
            )
        else:
            logger.debug("config_not_found", context_id=str(context_id))

        return config

    async def create_or_get(self, context_id: UUID) -> ContextSearchConfig:
        """Get existing config or create default if not exists.

        This method is idempotent - it will not create duplicates.

        Args:
            context_id: Context UUID

        Returns:
            ContextSearchConfig (existing or newly created)
        """
        # Try to get existing config
        config = await self.get_by_context(context_id)
        if config:
            return config

        # Create default config
        logger.info("config_creating_default", context_id=str(context_id))
        config = ContextSearchConfig(context_id=context_id)
        self.db.add(config)
        await self.db.commit()
        await self.db.refresh(config)

        logger.info(
            "config_created",
            context_id=str(context_id),
            semantic_weight=float(config.semantic_weight),
            bm25_weight=float(config.bm25_weight),
            fetch_factor=config.fetch_factor,
        )

        return config

    async def update(
        self,
        context_id: UUID,
        update_data: ContextSearchConfigUpdate,
    ) -> ContextSearchConfig:
        """Update search configuration for a context.

        Args:
            context_id: Context UUID
            update_data: Update request data

        Returns:
            Updated ContextSearchConfig

        Raises:
            ValueError: If config not found for context
        """
        config = await self.get_by_context(context_id)
        if not config:
            error_msg = f"Config not found for context {context_id}"
            logger.error("config_update_failed", context_id=str(context_id), error=error_msg)
            raise ValueError(error_msg)

        # Update fields
        update_dict = update_data.model_dump(exclude_unset=True)
        for key, value in update_dict.items():
            setattr(config, key, value)

        await self.db.commit()
        await self.db.refresh(config)

        logger.info(
            "config_updated",
            context_id=str(context_id),
            updated_fields=list(update_dict.keys()),
            semantic_weight=float(config.semantic_weight),
            bm25_weight=float(config.bm25_weight),
            fetch_factor=config.fetch_factor,
            use_rerank=config.use_rerank,
        )

        return config

    async def reset_to_default(self, context_id: UUID) -> ContextSearchConfig:
        """Reset configuration to default values.

        Args:
            context_id: Context UUID

        Returns:
            Reset ContextSearchConfig

        Raises:
            ValueError: If config not found for context
        """
        logger.info("config_resetting", context_id=str(context_id))

        defaults = ContextSearchConfigUpdate(
            semantic_weight=0.6,
            bm25_weight=0.4,
            fetch_factor=3,
            use_rerank=False,
            reranker_provider="voyage",
            reranker_model="rerank-2",
            # #1207: reset converges reinforce to the documented defaults too.
            # These must be passed explicitly — update() applies
            # exclude_unset, so omitting them would leave stored values
            # behind and "reset to defaults" would not mean defaults.
            reinforce_enabled=True,
            reinforce_max_boost=0.15,
            reinforce_require_host_arbitration=False,
        )

        config = await self.update(context_id, defaults)

        logger.info("config_reset_complete", context_id=str(context_id))
        return config

    async def delete(self, context_id: UUID) -> bool:
        """Delete search configuration for a context.

        Note: In normal operation, configs are auto-deleted via CASCADE when context is deleted.
        This method is provided for manual cleanup if needed.

        Args:
            context_id: Context UUID

        Returns:
            True if deleted, False if not found
        """
        config = await self.get_by_context(context_id)
        if not config:
            logger.debug("config_delete_not_found", context_id=str(context_id))
            return False

        await self.db.delete(config)
        await self.db.commit()

        logger.info("config_deleted", context_id=str(context_id))
        return True


class RouterCalibrationRepository:
    """Repository for per-bucket router arm performance (#1220 stage 4).

    Rows with ``context_id IS NULL`` are the fleet defaults (frozen-corpus
    gate runs); per-context rows let managed-cloud tuning diverge without
    touching the self-host defaults.
    """

    def __init__(self, db: AsyncSession):
        """Initialize repository with database session.

        Args:
            db: Async SQLAlchemy session
        """
        self.db = db

    async def upsert(
        self,
        *,
        bucket: str,
        arm: str,
        p_at_5: float,
        mrr_at_10: float,
        n_queries: int,
        sampled_at: datetime,
        context_id: UUID | None = None,
        source: str = ROUTER_CALIBRATION_SOURCE_FROZEN,
    ) -> RouterCalibration:
        """Insert or refresh one (scope, bucket, arm, source) measurement.

        ON CONFLICT targets the partial-unique index matching the scope
        (global vs per-context), so re-running a gate refreshes the row
        instead of failing or duplicating.

        Returns:
            The stored RouterCalibration row.
        """
        stmt = (
            pg_insert(RouterCalibration)
            .values(
                context_id=context_id,
                bucket=bucket,
                arm=arm,
                p_at_5=p_at_5,
                mrr_at_10=mrr_at_10,
                n_queries=n_queries,
                source=source,
                sampled_at=sampled_at,
            )
            .on_conflict_do_update(
                index_elements=(
                    ["bucket", "arm", "source"]
                    if context_id is None
                    else ["context_id", "bucket", "arm", "source"]
                ),
                index_where=text(
                    "context_id IS NULL" if context_id is None else "context_id IS NOT NULL"
                ),
                set_={
                    "p_at_5": p_at_5,
                    "mrr_at_10": mrr_at_10,
                    "n_queries": n_queries,
                    "sampled_at": sampled_at,
                },
            )
            .returning(RouterCalibration)
        )
        result = await self.db.execute(stmt)
        row = result.scalar_one()
        logger.debug(
            "router_calibration_upserted",
            context_id=str(context_id) if context_id else None,
            bucket=bucket,
            arm=arm,
            source=source,
        )
        return row

    async def get_for_context(self, context_id: UUID | None) -> list[RouterCalibration]:
        """Calibration rows for a context, falling back to the fleet defaults.

        Returns the context's own rows when any exist, else the global
        (``context_id IS NULL``) rows. Never mixes scopes — a partially
        calibrated context should read as its own coherent measurement set.
        """
        if context_id is not None:
            result = await self.db.execute(
                select(RouterCalibration).where(RouterCalibration.context_id == context_id)
            )
            rows = list(result.scalars().all())
            if rows:
                return rows
        result = await self.db.execute(
            select(RouterCalibration).where(RouterCalibration.context_id.is_(None))
        )
        return list(result.scalars().all())
