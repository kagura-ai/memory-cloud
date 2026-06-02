"""Graph repository for data access operations."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.memory import GraphMemory
from repositories.base import BaseRepository
from utils.datetime import utcnow
from utils.exceptions import NotFoundException
from utils.logger import get_logger

logger = get_logger(__name__)


class GraphRepository(BaseRepository[GraphMemory]):
    """Graph repository for PostgreSQL operations.

    Manages graph_memory table which stores NetworkX graphs as JSON.
    One graph per user for GDPR compliance.
    """

    def __init__(self, db: AsyncSession):
        """Initialize repository.

        Args:
            db: Database session
        """
        self.db = db

    async def get(self, id: int | str) -> GraphMemory | None:
        """Get graph by ID.

        Args:
            id: Graph ID (primary key)

        Returns:
            GraphMemory or None
        """
        if isinstance(id, str):
            id = int(id)

        result = await self.db.execute(select(GraphMemory).where(GraphMemory.id == id))
        return result.scalar_one_or_none()

    async def get_by_user_id(self, user_id: str) -> GraphMemory | None:
        """Get graph by user ID.

        Args:
            user_id: User ID (unique constraint)

        Returns:
            GraphMemory or None
        """
        result = await self.db.execute(select(GraphMemory).where(GraphMemory.user_id == user_id))
        return result.scalar_one_or_none()

    async def list(
        self, skip: int = 0, limit: int = 100, filters: dict | None = None
    ) -> list[GraphMemory]:
        """List graphs with pagination.

        Args:
            skip: Offset
            limit: Max results
            filters: Optional filters

        Returns:
            List of graphs
        """
        query = select(GraphMemory)

        # Pagination
        query = query.offset(skip).limit(limit)

        result = await self.db.execute(query)
        return list(result.scalars().all())

    async def create(self, graph: GraphMemory) -> GraphMemory:
        """Create new graph.

        Args:
            graph: GraphMemory entity

        Returns:
            Created graph
        """
        self.db.add(graph)
        await self.db.flush()
        await self.db.refresh(graph)

        logger.info("graph_created", graph_id=graph.id, user_id=graph.user_id)

        return graph

    async def update(self, id: int | str, graph: GraphMemory) -> GraphMemory:
        """Update graph.

        Args:
            id: Graph ID
            graph: Updated graph data

        Returns:
            Updated graph

        Raises:
            NotFoundException: If graph not found
        """
        if isinstance(id, str):
            id = int(id)

        existing = await self.get(id)
        if not existing:
            raise NotFoundException(f"Graph with id={id} not found")

        # Update fields
        existing.graph_data = graph.graph_data
        existing.updated_at = utcnow()

        if graph.last_decay_at:
            existing.last_decay_at = graph.last_decay_at

        if graph.last_consolidation_at:
            existing.last_consolidation_at = graph.last_consolidation_at

        await self.db.flush()
        await self.db.refresh(existing)

        logger.info("graph_updated", graph_id=id, user_id=existing.user_id)

        return existing

    async def update_graph_data(
        self,
        user_id: str,
        graph_data: dict[str, Any],
    ) -> GraphMemory:
        """Update graph data for user.

        Convenience method for updating graph JSON.

        Args:
            user_id: User ID
            graph_data: NetworkX node_link_data JSON

        Returns:
            Updated GraphMemory

        Raises:
            NotFoundException: If graph not found
        """
        existing = await self.get_by_user_id(user_id)
        if not existing:
            raise NotFoundException(f"Graph for user_id={user_id} not found")

        # Update graph data
        existing.graph_data = graph_data
        existing.updated_at = utcnow()

        await self.db.flush()
        await self.db.refresh(existing)

        logger.info("graph_data_updated", user_id=user_id)

        return existing

    async def delete(self, id: int | str) -> bool:
        """Delete graph.

        Args:
            id: Graph ID

        Returns:
            True if deleted, False if not found
        """
        if isinstance(id, str):
            id = int(id)

        graph = await self.get(id)
        if not graph:
            return False

        await self.db.delete(graph)
        await self.db.flush()

        logger.info("graph_deleted", graph_id=id, user_id=graph.user_id)

        return True

    async def delete_by_user_id(self, user_id: str) -> bool:
        """Delete graph by user ID.

        Args:
            user_id: User ID

        Returns:
            True if deleted, False if not found
        """
        graph = await self.get_by_user_id(user_id)
        if not graph:
            return False

        await self.db.delete(graph)
        await self.db.flush()

        logger.info("graph_deleted_by_user", user_id=user_id)

        return True

    async def count(self, filters: dict | None = None) -> int:
        """Count graphs.

        Args:
            filters: Optional filters

        Returns:
            Graph count
        """
        query = select(GraphMemory)

        result = await self.db.execute(query)
        graphs = result.scalars().all()
        return len(list(graphs))

    async def get_or_create(self, user_id: str) -> GraphMemory:
        """Get existing graph or create new one for user.

        Args:
            user_id: User ID

        Returns:
            GraphMemory (existing or newly created)
        """
        existing = await self.get_by_user_id(user_id)
        if existing:
            return existing

        # Create new empty graph
        new_graph = GraphMemory(
            user_id=user_id,
            graph_data={"directed": True, "multigraph": False, "nodes": [], "links": []},
        )

        return await self.create(new_graph)
