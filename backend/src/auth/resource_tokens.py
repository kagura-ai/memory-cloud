"""Resource Token Manager for Resource Ingest API authentication.

Issue #238: Resource-scoped API tokens for external systems.

Based on auth/api_keys.py pattern with resource-specific adaptations.
"""

from __future__ import annotations

import hashlib
import secrets

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.resource import ResourceToken
from utils.logger import get_logger

logger = get_logger(__name__)

# Resource token prefix for easy identification
RESOURCE_TOKEN_PREFIX = "kagura_resource_"


class ResourceTokenManager:
    """Resource token manager for Resource Ingest API.

    Issue #238: Manages resource-scoped API tokens using async/await.

    Pattern: Based on APIKeyManager (auth/api_keys.py)
    """

    def __init__(self, db: AsyncSession):
        """Initialize resource token manager.

        Args:
            db: Async database session
        """
        self.db = db

    @staticmethod
    def _hash_token(token: str) -> str:
        """Hash token using SHA256.

        Args:
            token: Plaintext token

        Returns:
            Hexadecimal hash string
        """
        return hashlib.sha256(token.encode()).hexdigest()

    @staticmethod
    def _generate_token() -> str:
        """Generate a new resource token.

        Returns:
            Token string (format: kagura_resource_<random>)
        """
        random_part = secrets.token_urlsafe(32)
        return f"{RESOURCE_TOKEN_PREFIX}{random_part}"

    async def create_token(
        self,
        resource_id: str,
        description: str | None = None,
        quota_events_per_hour: int = 1000,
        created_by: str | None = None,
    ) -> tuple[str, ResourceToken]:
        """Create a new resource token.

        Args:
            resource_id: Resource identifier this token is scoped to
            description: Human-readable description
            quota_events_per_hour: Event ingestion quota (default: 1000/hour)
            created_by: User ID who created this token

        Returns:
            Plaintext token (only shown once)

        Raises:
            ValueError: If resource_id is invalid
        """
        # Validate resource_id format
        if not resource_id or len(resource_id) > 255:
            raise ValueError(f"Invalid resource_id: {resource_id}")

        # Generate new token
        token = self._generate_token()
        token_hash = self._hash_token(token)

        # Create database record
        new_token = ResourceToken(
            resource_id=resource_id,
            token_hash=token_hash,
            description=description,
            quota_events_per_hour=quota_events_per_hour,
            created_by=created_by,
        )

        self.db.add(new_token)
        await self.db.flush()

        logger.info(
            "resource_token_created",
            resource_id=resource_id,
            quota=quota_events_per_hour,
            created_by=created_by,
        )

        return token, new_token

    async def verify_token(self, token: str, resource_id: str) -> ResourceToken | None:
        """Verify resource token and return token record.

        Args:
            token: Plaintext token to verify
            resource_id: Expected resource_id (must match token's scope)

        Returns:
            ResourceToken record if valid, None otherwise
        """
        token_hash = self._hash_token(token)

        # Query token with resource_id validation
        result = await self.db.execute(
            select(ResourceToken).where(
                and_(
                    ResourceToken.token_hash == token_hash,
                    ResourceToken.resource_id == resource_id,
                    ResourceToken.is_active == True,  # noqa: E712
                )
            )
        )
        token_record = result.scalar_one_or_none()

        if not token_record:
            logger.debug(  # Changed from warning to debug (security - don't log token prefix)
                "invalid_resource_token_attempt",
                resource_id=resource_id,
            )
            return None

        # Update last_used_at
        from utils.datetime import utcnow

        token_record.last_used_at = utcnow()
        await self.db.flush()

        logger.debug(
            "resource_token_verified",
            resource_id=resource_id,
            token_id=token_record.id,
        )

        return token_record

    async def revoke_token(self, token_id: int) -> None:
        """Revoke a resource token.

        Args:
            token_id: Token ID to revoke

        Raises:
            ValueError: If token not found
        """
        token = await self.db.get(ResourceToken, token_id)

        if not token:
            raise ValueError(f"Resource token {token_id} not found")

        token.is_active = False
        await self.db.flush()

        logger.info("resource_token_revoked", token_id=token_id, resource_id=token.resource_id)

    async def list_tokens(
        self,
        resource_id: str | None = None,
        created_by: str | None = None,
        include_revoked: bool = True,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[ResourceToken]:
        """List resource tokens with optional filters and pagination.

        Issue #264: Added pagination support and created_by filter.

        Args:
            resource_id: Optional resource_id filter
            created_by: Optional created_by filter (for user-specific tokens)
            include_revoked: Include revoked tokens (default: True)
            limit: Maximum number of tokens to return (None = all)
            offset: Starting offset for pagination (default: 0)

        Returns:
            List of ResourceToken entities
        """
        query = select(ResourceToken).order_by(ResourceToken.created_at.desc())

        if resource_id:
            query = query.where(ResourceToken.resource_id == resource_id)

        if created_by:
            query = query.where(ResourceToken.created_by == created_by)

        if not include_revoked:
            query = query.where(ResourceToken.is_active == True)  # noqa: E712

        if limit is not None:
            query = query.offset(offset).limit(limit)

        result = await self.db.execute(query)
        return list(result.scalars().all())

    async def count_tokens(
        self,
        resource_id: str | None = None,
        created_by: str | None = None,
        include_revoked: bool = True,
    ) -> int:
        """Count resource tokens matching filters.

        Issue #264: For pagination total count.

        Args:
            resource_id: Optional resource_id filter
            created_by: Optional created_by filter
            include_revoked: Include revoked tokens (default: True)

        Returns:
            Total count of matching tokens
        """
        query = select(func.count(ResourceToken.id))

        conditions = []
        if resource_id:
            conditions.append(ResourceToken.resource_id == resource_id)
        if created_by:
            conditions.append(ResourceToken.created_by == created_by)
        if not include_revoked:
            conditions.append(ResourceToken.is_active == True)  # noqa: E712

        if conditions:
            query = query.where(and_(*conditions))

        result = await self.db.execute(query)
        return result.scalar() or 0
