"""Async SQLAlchemy-based API Key Manager.

Rewritten for memory-cloud async architecture.
Manages API keys using AsyncSession and async/await patterns.
"""

from __future__ import annotations

import secrets
from datetime import timedelta
from typing import NamedTuple
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import APIKey, Context
from utils.datetime import utcnow
from utils.hashing import sha256_hex
from utils.logger import get_logger

logger = get_logger(__name__)

# API Key prefix for easy identification
API_KEY_PREFIX = "kagura_"


class VerifiedKey(NamedTuple):
    """Result of a successful API key verification.

    Issue #626 — extended from a plain ``(user_id, workspace_id)`` 2-tuple
    so that public-bound keys (#626) and workspace-scoped keys (#169) can
    coexist without further breaking changes when more attribution shapes
    appear. **Callers MUST use attribute access** (``result.user_id``,
    ``result.workspace_id``) — positional unpacking like
    ``user_id, workspace_id = verified`` will raise ``ValueError: too
    many values to unpack`` against this 4-field tuple. All in-tree
    callers were migrated to attribute access in this change.

    Attributes:
        id: ``api_keys.id`` integer primary key — surfaced here so the
            public endpoint can populate per-key rate-limit buckets and
            ``usage_stats.api_key_id`` without a second SELECT.
        user_id: Owner OAuth2 sub.
        workspace_id: Workspace scope (Issue #169). None for global and
            for public-bound keys (mutually exclusive with bound_context_id).
        bound_context_id: Public-context attribution (Issue #626). None
            unless the key was created with a binding.
    """

    id: int
    user_id: str
    workspace_id: UUID | None
    bound_context_id: UUID | None


class APIKeyManager:
    """Async API Key manager using SQLAlchemy.

    Manages API keys with async/await for memory-cloud architecture.
    """

    def __init__(self, db: AsyncSession):
        """Initialize API Key manager.

        Args:
            db: Async database session
        """
        self.db = db

    @staticmethod
    def _hash_key(api_key: str) -> str:
        """Hash API key using SHA256."""
        return sha256_hex(api_key)

    @staticmethod
    def _generate_key() -> str:
        """Generate a new API key.

        Returns:
            API key string (format: kagura_<random>)
        """
        random_part = secrets.token_urlsafe(32)
        return f"{API_KEY_PREFIX}{random_part}"

    async def create_key(
        self,
        name: str,
        user_id: str,
        expires_days: int | None = None,
        workspace_id: UUID | None = None,
        bound_context_id: UUID | None = None,
        auto_hide_minutes: int = 10,
    ) -> tuple[str, APIKey]:
        """Create a new API key.

        Issue #169: Workspace-scoped API keys (access all contexts in workspace).
        Issue #626: Public-bound API keys (attributed to one is_public=true context).
            Returning the freshly-flushed ``APIKey`` row alongside the plaintext
            saves callers a second SELECT just to read back ``id`` / ``created_at``
            for the one-time-reveal response.
        Migration 034: Added auto_hide for visibility control (Zero-knowledge model).
        Migration 034: Removed context_id parameter (deprecated).

        Args:
            name: Friendly name for the key
            user_id: User ID that owns this key
            expires_days: Optional expiration in days
            workspace_id: Optional workspace ID for workspace-scoped access (Issue #169).
                Mutually exclusive with ``bound_context_id``.
            bound_context_id: Optional context ID for public-bound attribution
                (Issue #626). The context must satisfy ``is_public=True`` at
                creation time. Mutually exclusive with ``workspace_id``.
            auto_hide_minutes: Minutes until key is auto-hidden (default: 10)

        Returns:
            ``(plaintext_key, new_api_key_row)`` — the plaintext is only ever
            available in this return value (encrypted-at-rest beyond that);
            the row carries the assigned ``id`` and ``created_at``.

        Raises:
            ValueError: If name already exists for user, if both scoping
                params are supplied, or if the bound context is not public.
        """
        if workspace_id is not None and bound_context_id is not None:
            raise ValueError(
                "workspace_id and bound_context_id are mutually exclusive — "
                "a key cannot be both workspace-scoped and public-bound"
            )

        # Validate the bound context exists and is public at creation time.
        # Subsequent flips of context.is_public are handled at request time
        # by the public endpoint (the binding row stays; access is denied
        # while is_public is false).
        if bound_context_id is not None:
            ctx_result = await self.db.execute(
                select(Context).where(Context.id == bound_context_id)
            )
            ctx = ctx_result.scalar_one_or_none()
            if ctx is None:
                raise ValueError(f"Context {bound_context_id} not found")
            if ctx.is_public is not True:
                raise ValueError(
                    "Cannot bind API key to a non-public context — "
                    "set is_public=true on the context first"
                )

        # Check if name already exists in current workspace (Issue #169)
        conditions = [
            APIKey.name == name,
            APIKey.user_id == user_id,
            APIKey.revoked_at.is_(None),
        ]
        if workspace_id:
            conditions.append(APIKey.workspace_id == workspace_id)

        result = await self.db.execute(select(APIKey).where(and_(*conditions)))
        existing = result.scalar_one_or_none()

        if existing:
            # The uniqueness query above scopes to workspace_id only when
            # one is provided (#169 keys); for owner-scoped and public-bound
            # keys it scopes to (user_id, active). Tail the message to match.
            scope_phrase = "in this workspace" if workspace_id is not None else "for this user"
            raise ValueError(f"API key with name '{name}' already exists {scope_phrase}")

        # Generate new key
        api_key = self._generate_key()
        key_hash = self._hash_key(api_key)
        key_prefix = api_key[:16]

        # Calculate expiration
        expires_at = None
        if expires_days:
            expires_at = utcnow() + timedelta(days=expires_days)

        # Calculate visibility expiration (Migration 034) - 10 minutes default
        visibility_expires_at = utcnow() + timedelta(minutes=auto_hide_minutes)

        # Migration 035: Encrypt plaintext for storage
        from utils.encryption import get_encryptor

        plaintext_encrypted = get_encryptor().encrypt(api_key)

        # Create database record
        new_key = APIKey(
            key_hash=key_hash,
            key_prefix=key_prefix,
            name=name,
            user_id=user_id,
            workspace_id=workspace_id,  # Issue #169
            bound_context_id=bound_context_id,  # Issue #626
            expires_at=expires_at,
            visibility_expires_at=visibility_expires_at,  # Migration 034
            plaintext_encrypted=plaintext_encrypted,  # Migration 035
        )

        self.db.add(new_key)
        await self.db.flush()

        logger.info(
            "api_key_created",
            name=name,
            user_id=user_id,
            workspace_id=str(workspace_id) if workspace_id else None,
            bound_context_id=str(bound_context_id) if bound_context_id else None,
        )

        return api_key, new_key

    async def verify_key(self, api_key: str) -> VerifiedKey | None:
        """Verify API key and return a ``VerifiedKey`` view.

        Issue #169: Returns workspace_id for workspace-scoped API keys.
        Issue #626: Returns bound_context_id for public-bound API keys.
        Migration 034: Removed context_id (deprecated).

        Args:
            api_key: Plaintext API key to verify

        Returns:
            ``VerifiedKey`` if valid, None if not found / revoked / expired.

            - Owner-scoped (global) keys: workspace_id=None, bound_context_id=None
            - Workspace-scoped keys (#169): workspace_id=<UUID>, bound_context_id=None
            - Public-bound keys (#626): workspace_id=None, bound_context_id=<UUID>

            The two non-null scopings are mutually exclusive (DB CHECK constraint).
        """
        key_hash = self._hash_key(api_key)

        result = await self.db.execute(select(APIKey).where(APIKey.key_hash == key_hash))
        key_record = result.scalar_one_or_none()

        if not key_record:
            return None

        # Check if revoked
        if key_record.revoked_at:
            return None

        # Check if expired
        if key_record.expires_at:
            if utcnow() > key_record.expires_at:
                return None

        # Update last_used_at
        key_record.last_used_at = utcnow()
        await self.db.flush()

        return VerifiedKey(
            id=key_record.id,
            user_id=key_record.user_id,
            workspace_id=key_record.workspace_id,
            bound_context_id=key_record.bound_context_id,
        )

    async def list_keys(
        self,
        user_id: str | None = None,
        workspace_id: UUID | None = None,
    ) -> list[APIKey]:
        """List all API keys (optionally filtered by user and/or workspace).

        Issue #169: Workspace-scoped API keys.
        Migration 034: Removed context_id filter (deprecated).

        Args:
            user_id: Optional user_id filter
            workspace_id: Optional workspace_id filter (Issue #169)

        Returns:
            List of APIKey entities
        """
        query = select(APIKey).order_by(APIKey.created_at.desc())

        if user_id:
            query = query.where(APIKey.user_id == user_id)

        if workspace_id:
            query = query.where(APIKey.workspace_id == workspace_id)

        result = await self.db.execute(query)
        return list(result.scalars().all())

    async def revoke_key(self, name: str, user_id: str) -> bool:
        """Revoke an API key.

        Args:
            name: Name of the key to revoke
            user_id: User ID that owns the key

        Returns:
            True if revoked, False if not found
        """
        result = await self.db.execute(
            select(APIKey).where(
                and_(
                    APIKey.name == name,
                    APIKey.user_id == user_id,
                    APIKey.revoked_at.is_(None),
                )
            )
        )
        key_record = result.scalar_one_or_none()

        if not key_record:
            return False

        key_record.revoked_at = utcnow()
        await self.db.flush()

        logger.info("api_key_revoked", name=name, user_id=user_id)

        return True

    async def delete_key(self, name: str, user_id: str) -> bool:
        """Permanently delete an API key.

        Args:
            name: Name of the key to delete
            user_id: User ID that owns the key

        Returns:
            True if deleted, False if not found
        """
        result = await self.db.execute(
            select(APIKey).where(and_(APIKey.name == name, APIKey.user_id == user_id))
        )
        key_record = result.scalar_one_or_none()

        if not key_record:
            return False

        await self.db.delete(key_record)
        await self.db.flush()

        logger.info("api_key_deleted", name=name, user_id=user_id)

        return True

    async def hide_key(self, key_id: int, user_id: str) -> None:
        """Manually hide API key (owner verification).

        Migration 034: Zero-knowledge model - only owner can hide.

        Args:
            key_id: ID of the key to hide
            user_id: User ID (owner verification)

        Raises:
            ValueError: If key not found
            PermissionError: If user is not the owner
        """
        result = await self.db.execute(select(APIKey).where(APIKey.id == key_id))
        key = result.scalar_one_or_none()

        if not key:
            raise ValueError(f"API key with ID {key_id} not found")

        # Zero-knowledge: only owner can hide
        if key.user_id != user_id:
            raise PermissionError("Only owner can hide API key")

        key.hidden_at = utcnow()
        key.visibility_expires_at = None  # Cancel auto-hide
        key.plaintext_encrypted = None  # Migration 035: Delete encrypted plaintext
        await self.db.flush()

        logger.info("api_key_hidden", key_id=key_id, user_id=user_id)

    @staticmethod
    def should_auto_hide(key: APIKey) -> bool:
        """Check if key should be auto-hidden.

        Migration 034: Auto-hide logic for background job.

        Args:
            key: APIKey instance

        Returns:
            True if key should be auto-hidden
        """
        # Already hidden
        if key.hidden_at:
            return False

        # No expiration set
        if not key.visibility_expires_at:
            return False

        # Check if expired
        return utcnow() >= key.visibility_expires_at
