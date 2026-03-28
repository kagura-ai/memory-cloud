"""Memory repository for data access operations."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import and_, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.memory import Memory
from repositories.base import BaseRepository
from utils.datetime import utcnow
from utils.exceptions import NotFoundException
from utils.logger import get_logger

logger = get_logger(__name__)


class MemoryRepository(BaseRepository[Memory]):
    """Memory repository for PostgreSQL operations."""

    def __init__(self, db: AsyncSession):
        """Initialize repository.

        Args:
            db: Database session
        """
        self.db = db

    async def get(self, id: UUID) -> Memory | None:
        """Get memory by ID.

        Args:
            id: Memory UUID

        Returns:
            Memory or None
        """
        result = await self.db.execute(select(Memory).where(Memory.id == id))
        return result.scalar_one_or_none()

    async def list(
        self, skip: int = 0, limit: int = 100, filters: dict | None = None
    ) -> list[Memory]:
        """List memories with filters.

        Args:
            skip: Offset
            limit: Max results
            filters: Optional filters (user_id, scope, type, etc.)

        Returns:
            List of memories
        """
        query = select(Memory)

        # Apply filters
        if filters:
            conditions = []

            if "user_id" in filters:
                conditions.append(Memory.user_id == filters["user_id"])

            if "scope" in filters:
                conditions.append(Memory.scope == filters["scope"])

            if "type" in filters:
                conditions.append(Memory.type == filters["type"])

            if conditions:
                query = query.where(and_(*conditions))

        # Order by created_at DESC
        query = query.order_by(desc(Memory.created_at))

        # Pagination
        query = query.offset(skip).limit(limit)

        result = await self.db.execute(query)
        return list(result.scalars().all())

    async def create(self, memory: Memory) -> Memory:
        """Create new memory.

        Args:
            memory: Memory entity

        Returns:
            Created memory
        """
        self.db.add(memory)
        await self.db.flush()
        await self.db.refresh(memory)

        logger.info("memory_created", memory_id=str(memory.id), user_id=memory.user_id)

        return memory

    async def update(self, id: UUID, memory: Memory) -> Memory:
        """Update memory.

        Args:
            id: Memory ID
            memory: Updated memory data

        Returns:
            Updated memory

        Raises:
            NotFoundException: If memory not found
        """
        existing = await self.get(id)
        if not existing:
            raise NotFoundException("Memory", str(id))

        # Update fields
        for key, value in memory.__dict__.items():
            if not key.startswith("_") and key != "id":
                setattr(existing, key, value)

        existing.updated_at = utcnow()
        await self.db.flush()
        await self.db.refresh(existing)

        logger.info("memory_updated", memory_id=str(id))

        return existing

    async def delete(self, id: UUID) -> bool:
        """Delete memory.

        Args:
            id: Memory ID

        Returns:
            True if deleted
        """
        memory = await self.get(id)
        if not memory:
            return False

        await self.db.delete(memory)
        await self.db.flush()

        logger.info("memory_deleted", memory_id=str(id))

        return True

    async def count(self, filters: dict | None = None) -> int:
        """Count memories.

        Args:
            filters: Optional filters

        Returns:
            Memory count
        """
        query = select(func.count(Memory.id))

        if filters:
            conditions = []

            if "user_id" in filters:
                conditions.append(Memory.user_id == filters["user_id"])

            if "scope" in filters:
                conditions.append(Memory.scope == filters["scope"])

            if conditions:
                query = query.where(and_(*conditions))

        result = await self.db.execute(query)
        return result.scalar_one()

    # Memory-specific methods

    async def get_by_user(
        self, user_id: str, scope: str | None = None, limit: int = 100
    ) -> list[Memory]:
        """Get memories by user.

        Args:
            user_id: User ID
            scope: Optional scope filter (working/persistent)
            limit: Max results

        Returns:
            List of memories
        """
        filters = {"user_id": user_id}
        if scope:
            filters["scope"] = scope

        return await self.list(limit=limit, filters=filters)

    async def update_access_stats(self, memory_id: UUID, client: str) -> None:
        """Update access statistics.

        Args:
            memory_id: Memory ID
            client: Client name

        Updates:
            - access_count += 1
            - last_used_at = now
            - accessed_by_clients append client
        """
        memory = await self.get(memory_id)
        if not memory:
            return

        memory.access_count = (memory.access_count or 0) + 1
        memory.last_used_at = utcnow()

        # Add client to accessed_by_clients
        if not memory.accessed_by_clients:
            memory.accessed_by_clients = []

        if client not in memory.accessed_by_clients:
            memory.accessed_by_clients.append(client)

        await self.db.flush()

        logger.debug(
            "access_stats_updated",
            memory_id=str(memory_id),
            access_count=memory.access_count,
        )

    async def promote_to_persistent(self, memory_id: UUID) -> None:
        """Promote working memory to persistent.

        Args:
            memory_id: Memory ID
        """
        memory = await self.get(memory_id)
        if not memory:
            return

        if memory.scope == "persistent":
            return  # Already persistent

        memory.scope = "persistent"
        memory.promoted_at = utcnow()
        await self.db.flush()

        logger.info("memory_promoted_to_persistent", memory_id=str(memory_id))

    async def get_old_working_memories(self, user_id: str, age_days: int = 30) -> list[Memory]:
        """Get old working memories for cleanup.

        Args:
            user_id: User ID
            age_days: Minimum age in days (default: 30)

        Returns:
            List of old working memories
        """
        from datetime import timedelta

        cutoff_date = utcnow() - timedelta(days=age_days)

        result = await self.db.execute(
            select(Memory)
            .where(
                Memory.user_id == user_id,
                Memory.scope == "working",
                Memory.created_at < cutoff_date,
            )
            .order_by(Memory.importance, Memory.last_used_at)
        )

        return list(result.scalars().all())
