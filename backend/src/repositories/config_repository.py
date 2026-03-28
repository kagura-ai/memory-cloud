"""Repository for Context Search Configuration.

This module provides database access for context-level search and reranker settings.
Issue #130: Context-scoped Search & Reranker Settings UI
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.config import ContextSearchConfig
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
