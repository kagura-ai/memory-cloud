"""Base repository interface for data access abstraction.

Repository Pattern provides a clean separation between business logic
and data access, making the code more testable and maintainable.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Generic, TypeVar
from uuid import UUID

T = TypeVar("T")


class BaseRepository(ABC, Generic[T]):
    """Base repository interface.

    Provides standard CRUD operations for any entity type.
    """

    @abstractmethod
    async def get(self, id: UUID | int | str) -> T | None:
        """Get entity by ID.

        Args:
            id: Entity ID

        Returns:
            Entity or None if not found
        """
        ...

    @abstractmethod
    async def list(self, skip: int = 0, limit: int = 100, filters: dict | None = None) -> list[T]:
        """List entities with pagination.

        Args:
            skip: Number of records to skip
            limit: Maximum number of records to return
            filters: Optional filters

        Returns:
            List of entities
        """
        ...

    @abstractmethod
    async def create(self, entity: T) -> T:
        """Create new entity.

        Args:
            entity: Entity to create

        Returns:
            Created entity with ID assigned
        """
        ...

    @abstractmethod
    async def update(self, id: UUID | int | str, entity: T) -> T:
        """Update entity.

        Args:
            id: Entity ID
            entity: Updated entity data

        Returns:
            Updated entity

        Raises:
            NotFoundException: If entity not found
        """
        ...

    @abstractmethod
    async def delete(self, id: UUID | int | str) -> bool:
        """Delete entity.

        Args:
            id: Entity ID

        Returns:
            True if deleted, False if not found
        """
        ...

    @abstractmethod
    async def count(self, filters: dict | None = None) -> int:
        """Count entities.

        Args:
            filters: Optional filters

        Returns:
            Entity count
        """
        ...
