"""User repository for data access operations."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import User
from repositories.base import BaseRepository
from utils.datetime import utcnow
from utils.exceptions import NotFoundException
from utils.logger import get_logger

logger = get_logger(__name__)


class UserRepository(BaseRepository[User]):
    """User repository for PostgreSQL operations."""

    def __init__(self, db: AsyncSession):
        """Initialize repository.

        Args:
            db: Database session
        """
        self.db = db

    async def get(self, id: int) -> User | None:
        """Get user by ID.

        Args:
            id: User ID (integer primary key)

        Returns:
            User or None
        """
        result = await self.db.execute(select(User).where(User.id == id))
        return result.scalar_one_or_none()

    async def get_by_email(self, email: str) -> User | None:
        """Get user by email.

        Args:
            email: User email

        Returns:
            User or None
        """
        result = await self.db.execute(select(User).where(User.email == email))
        return result.scalar_one_or_none()

    async def get_by_oauth_id(self, user_id: str) -> User | None:
        """Get user by OAuth2 sub (user_id).

        Args:
            user_id: OAuth2 sub claim

        Returns:
            User or None
        """
        result = await self.db.execute(select(User).where(User.user_id == user_id))
        return result.scalar_one_or_none()

    async def list(
        self, skip: int = 0, limit: int = 100, filters: dict | None = None
    ) -> list[User]:
        """List users.

        Args:
            skip: Offset
            limit: Max results
            filters: Optional filters (role, etc.)

        Returns:
            List of users
        """
        query = select(User)

        # Apply filters
        if filters and "role" in filters:
            query = query.where(User.role == filters["role"])

        query = query.offset(skip).limit(limit)

        result = await self.db.execute(query)
        return list(result.scalars().all())

    async def create(self, user: User) -> User:
        """Create new user.

        Args:
            user: User entity

        Returns:
            Created user
        """
        self.db.add(user)
        await self.db.flush()
        await self.db.refresh(user)

        logger.info("user_created", email=user.email, user_id=user.user_id)

        return user

    async def update(self, id: int, user: User) -> User:
        """Update user.

        Args:
            id: User ID
            user: Updated user data

        Returns:
            Updated user

        Raises:
            NotFoundException: If user not found
        """
        existing = await self.get(id)
        if not existing:
            raise NotFoundException("User", str(id))

        # Update fields
        for key, value in user.__dict__.items():
            if not key.startswith("_") and key not in ("id", "created_at"):
                setattr(existing, key, value)

        existing.updated_at = utcnow()
        await self.db.flush()
        await self.db.refresh(existing)

        logger.info("user_updated", user_id=existing.user_id)

        return existing

    async def delete(self, id: int) -> bool:
        """Delete user.

        Args:
            id: User ID

        Returns:
            True if deleted
        """
        user = await self.get(id)
        if not user:
            return False

        await self.db.delete(user)
        await self.db.flush()

        logger.info("user_deleted", user_id=user.user_id)

        return True

    async def count(self, filters: dict | None = None) -> int:
        """Count users.

        Args:
            filters: Optional filters

        Returns:
            User count
        """
        from sqlalchemy import func

        query = select(func.count(User.id))

        if filters and "role" in filters:
            query = query.where(User.role == filters["role"])

        result = await self.db.execute(query)
        return result.scalar_one()

    async def update_last_login(self, user_id: str) -> None:
        """Update last login timestamp.

        Args:
            user_id: OAuth2 sub
        """
        user = await self.get_by_oauth_id(user_id)
        if user:
            user.last_login_at = utcnow()
            await self.db.flush()

            logger.debug("last_login_updated", user_id=user_id)
