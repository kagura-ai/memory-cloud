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

from config.settings import get_settings
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
        key_prefix: The key's non-secret prefix (``api_keys.key_prefix``,
            e.g. ``kagura_abc123``). Issue #1164: surfaced onto the
            authenticated principal (``api_key_prefix``) so audit trails for
            programmatic member-management actions attribute the specific
            acting key, not just the owner user_id.
        agent_id: Issue #1275 (RFC-0002 P0-2): the agent this key is bound
            to, already verified ``active`` (suspended/retired agents are
            rejected at verify time — fail-closed kill switch). None for
            unbound keys.
        agent_enforcement_mode: The bound agent's ``enforcement_mode``
            (``shadow`` | ``enforce``), read in the same verify-time lookup
            so the binding chokepoints need no second agents query. None for
            unbound keys.
    """

    id: int
    user_id: str
    workspace_id: UUID | None
    bound_context_id: UUID | None
    key_prefix: str | None = None
    agent_id: UUID | None = None
    agent_enforcement_mode: str | None = None


def apply_zero_knowledge_hide(key: APIKey) -> None:
    """Apply the Migration-034/035 zero-knowledge hide mutations to ``key``.

    Stops showing the plaintext (``hidden_at`` now, ``visibility_expires_at``
    cleared) AND drops the Fernet-decryptable at-rest copy
    (``plaintext_encrypted`` nulled). Shared by ``hide_key``, the
    owner-provisioned force-hide (member_credentials), and the programmatic
    soft-revoke so the three never drift — the at-rest copy must always be
    dropped, because the hourly auto-hide sweeper skips already-hidden and
    revoked rows and would never revisit them. Does NOT flush/commit.
    """
    key.hidden_at = utcnow()
    key.visibility_expires_at = None
    key.plaintext_encrypted = None


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
        agent_id: UUID | None = None,
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
            agent_id: Issue #1275 (RFC-0002 P0-2): bind the key to a
                registered agent. Requires ``workspace_id`` (an agent-bound
                key with the global-key shape is rejected at mint) and the
                agent MUST belong to the same workspace — PostgreSQL cannot
                express this cross-table invariant in a CHECK, so it is
                enforced here and re-asserted defensively at verify. Mutually
                exclusive with ``bound_context_id``.

        Returns:
            ``(plaintext_key, new_api_key_row)`` — the plaintext is only ever
            available in this return value (encrypted-at-rest beyond that);
            the row carries the assigned ``id`` and ``created_at``.

        Raises:
            ValueError: If name already exists for user, if both scoping
                params are supplied, if the bound context is not public, or
                if the agent binding is malformed (missing/mismatched
                workspace, unknown agent, or a non-active agent).
        """
        if workspace_id is not None and bound_context_id is not None:
            raise ValueError(
                "workspace_id and bound_context_id are mutually exclusive — "
                "a key cannot be both workspace-scoped and public-bound"
            )

        # Issue #1275: agent-bound mint gates. Fail-closed at mint so an
        # invalid binding never reaches the verify path.
        if agent_id is not None:
            if bound_context_id is not None:
                raise ValueError(
                    "agent_id and bound_context_id are mutually exclusive — "
                    "a key cannot be both agent-bound and public-bound"
                )
            if workspace_id is None:
                raise ValueError(
                    "agent-bound keys must be workspace-scoped — a global key cannot carry agent_id"
                )
            from models.agent import AGENT_STATUS_ACTIVE, Agent

            agent_result = await self.db.execute(select(Agent).where(Agent.id == agent_id))
            agent = agent_result.scalar_one_or_none()
            if agent is None:
                raise ValueError(f"Agent {agent_id} not found")
            if agent.workspace_id != workspace_id:
                raise ValueError(
                    "agent and key workspace mismatch — the agent must belong "
                    "to the key's workspace"
                )
            if agent.status != AGENT_STATUS_ACTIVE:
                raise ValueError(
                    f"agent is '{agent.status}' — keys can only be minted for active agents"
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
            agent_id=agent_id,  # Issue #1275
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
            agent_id=str(agent_id) if agent_id else None,
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

        # Issue #964: the SELECT above filters by key_hash in the DB B-tree,
        # which leaks an application-level query-timing oracle (a prefix-matching
        # hash takes measurably longer than a total miss). Re-gate acceptance on
        # a constant-time comparison before trusting the surfaced row, so a
        # co-located adversary cannot use response timing to search the hash
        # space. Both operands are fixed-length sha256 hex strings.
        if not secrets.compare_digest(key_record.key_hash, key_hash):
            return None

        # Check if revoked
        if key_record.revoked_at:
            return None

        # Check if expired
        if key_record.expires_at:
            if utcnow() > key_record.expires_at:
                return None

        # Issue #1275 (RFC-0002 P0-2): agent-bound key gates. Only bound keys
        # pay the extra agents SELECT — unbound keys (every credential
        # existing before P0-2) stay byte-for-byte on the pre-#1275 path.
        agent_enforcement_mode: str | None = None
        if key_record.agent_id is not None:
            from models.agent import AGENT_STATUS_ACTIVE, Agent

            agent_result = await self.db.execute(
                select(Agent).where(Agent.id == key_record.agent_id)
            )
            agent = agent_result.scalar_one_or_none()
            # Fail-closed kill switch: a suspended/retired (or somehow
            # missing) agent invalidates every key bound to it — one row
            # update beats revoking N keys.
            if agent is None or agent.status != AGENT_STATUS_ACTIVE:
                logger.warning(
                    "api_key_rejected_agent_kill_switch",
                    key_prefix=key_record.key_prefix,
                    agent_id=str(key_record.agent_id),
                    agent_status=agent.status if agent else "missing",
                )
                return None
            # Defensive re-assert of the mint-time invariant (PostgreSQL
            # cannot express the cross-table workspace equality in a CHECK).
            if agent.workspace_id != key_record.workspace_id:
                logger.warning(
                    "api_key_rejected_agent_workspace_mismatch",
                    key_prefix=key_record.key_prefix,
                    agent_id=str(agent.id),
                )
                return None
            agent_enforcement_mode = agent.enforcement_mode

            # Agent liveness signal — throttled like last_used_at below.
            from services.agent_registry_service import AgentRegistryService

            await AgentRegistryService(self.db).touch_last_seen(agent.id)

        # Update last_used_at — throttled (#947). This runs on EVERY auth (one
        # per MCP tool call), so writing the row each time is hot-row
        # write-amplification. Skip the write while the stored timestamp is
        # fresher than the throttle window; a NULL (never-used) key always
        # writes. The compare is naive-UTC vs naive-UTC (both via utcnow()), and
        # guarding the assignment itself (not just the flush) keeps the session
        # object clean so a later request-level commit cannot persist it anyway.
        now = utcnow()
        throttle = timedelta(seconds=get_settings().api_key_last_used_throttle_seconds)
        if key_record.last_used_at is None or now - key_record.last_used_at >= throttle:
            key_record.last_used_at = now
            await self.db.flush()

        return VerifiedKey(
            id=key_record.id,
            user_id=key_record.user_id,
            workspace_id=key_record.workspace_id,
            bound_context_id=key_record.bound_context_id,
            key_prefix=key_record.key_prefix,
            agent_id=key_record.agent_id,
            agent_enforcement_mode=agent_enforcement_mode,
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

        apply_zero_knowledge_hide(key)
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
