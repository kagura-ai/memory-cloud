"""Memory repository for data access operations."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import and_, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import CONTEXT_TRUST_TIER_TRUSTED, Context
from models.memory import DELIVERY_MODE_ALWAYS, SOURCE_TYPE_CONNECTOR, Memory
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

    async def get(self, id: UUID, *, include_deleted: bool = False) -> Memory | None:
        """Get memory by ID.

        #1316/#1320: soft-deleted (tombstoned) rows are EXCLUDED by default —
        every read/mutation path that fetches by id must treat a forgotten
        memory as absent during the retention window. The only caller that
        legitimately needs tombstones is ``patch_memory`` (its #439 contract
        returns 410 ``MemoryGoneError`` to authorized callers); it opts in
        with ``include_deleted=True``.

        Args:
            id: Memory UUID
            include_deleted: When True, return the row even if soft-deleted.

        Returns:
            Memory or None
        """
        stmt = select(Memory).where(Memory.id == id)
        if not include_deleted:
            stmt = stmt.where(Memory.deleted_at.is_(None))
        result = await self.db.execute(stmt)
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

        # Order by created_at DESC, with id as a unique tiebreaker so
        # pagination is deterministic when rows share a created_at value
        # (a tie on created_at otherwise lets Postgres return an arbitrary,
        # unstable order across the offset/limit windows, which can skip or
        # duplicate rows between pages). Mirrors the tiebreaker pattern used
        # by the importance-ordered query below.
        query = query.order_by(desc(Memory.created_at), desc(Memory.id))

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
        # include_deleted=True: callers gate tombstone visibility at the
        # service layer; the internal fetch must still see the row that the
        # caller already holds. Critically, forget() stamps deleted_at on the
        # in-session object BEFORE calling update() — the SELECT here
        # autoflushes that change, and a default-filtered fetch would then
        # miss its own row and break soft-delete (#1316 refactor).
        existing = await self.get(id, include_deleted=True)
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
        # include_deleted=True: hard-delete is how the nightly retention purge
        # (tasks/neural_tasks.cleanup_deleted_memories_task) erases tombstoned
        # rows after the retention window — a default-filtered fetch would
        # make the purge a silent no-op and retention/erasure would never
        # physically happen (#1316 review sweep).
        memory = await self.get(id, include_deleted=True)
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

    async def update_access_stats(
        self, memory_id: UUID, client: str, *, count_as_adoption: bool = False
    ) -> None:
        """Update access statistics.

        Args:
            memory_id: Memory ID
            client: Client name
            count_as_adoption: When True (issue #1046), also bump
                ``reference_count`` — the *adoption* signal recorded only when an
                agent deliberately fetches Layer-3 detail via ``reference()``.
                Surfacing call sites (recall top-k return, explore spreading
                activation) leave this False so adoption stays distinguishable
                from mere surfacing. ``access_count`` is bumped either way, so the
                invariant ``access_count >= reference_count`` always holds.

        Updates:
            - access_count += 1
            - reference_count += 1 (only when count_as_adoption=True)
            - last_used_at = now
            - accessed_by_clients append client
        """
        memory = await self.get(memory_id)
        if not memory:
            return

        memory.access_count = (memory.access_count or 0) + 1
        if count_as_adoption:
            memory.reference_count = (memory.reference_count or 0) + 1
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
            reference_count=memory.reference_count,
            count_as_adoption=count_as_adoption,
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

    async def get_by_resource_id(
        self, resource_id: str, context_id: UUID, user_id: str
    ) -> Memory | None:
        """Find active memory by external resource_id within a context.

        Uses the computed resource_id column (details->>'resource_id').

        Args:
            resource_id: External resource identifier
            context_id: Context UUID
            user_id: User ID (ownership check)

        Returns:
            Memory or None
        """
        result = await self.db.execute(
            select(Memory)
            .where(
                Memory.resource_id == resource_id,
                Memory.context_id == context_id,
                Memory.user_id == user_id,
                Memory.deleted_at.is_(None),
            )
            .order_by(desc(Memory.created_at))
            .limit(1)
        )
        return result.scalars().first()

    # Issue #886: the always-load read returns L1 (summary) + L2
    # (context_summary) + a few metadata fields only — never L3 (content /
    # details). Selecting just these columns keeps the every-turn load_pinned
    # path from fetching the potentially large content/details TEXT+JSONB it
    # would only discard. Order matches PinnedMemoryItem field access.
    _PINNED_COLUMNS = (
        Memory.id,
        Memory.summary,
        Memory.context_summary,
        Memory.type,
        Memory.importance,
        Memory.delivery_mode,
        Memory.created_at,
        # #1299: the per-memory binding filter matches rows by their own
        # context/type/source — cheap identifier columns, still no L3 load.
        Memory.context_id,
        Memory.source_type,
    )

    async def list_pinned(
        self, workspace_id: UUID, context_id: UUID, limit: int, *, trusted_only: bool = False
    ) -> tuple[list, int]:
        """Deterministic always-load set for a context (Issue #886).

        Returns up to ``limit`` always-delivery memories ordered by
        ``importance DESC, created_at ASC, id ASC`` — fully deterministic down
        to the ``id`` tie-break, so the same context yields the same ordered set
        every call. Also returns the total count of matching rows so the caller
        can report ``truncated`` / ``total_available`` without silent loss.

        Rows are partial (L1 + L2 + metadata only — see ``_PINNED_COLUMNS``); the
        L3 ``content`` / ``details`` are intentionally not loaded. Each row
        exposes the selected columns by name (``row.summary`` etc.).

        This is the deterministic counterpart to ``recall()``: no embedding, no
        Qdrant, no rerank — a plain indexed SQL scan (backed by the partial
        index ``idx_memories_delivery_always``).

        Args:
            workspace_id: Workspace isolation scope.
            context_id: Context whose pinned memories to load.
            limit: Hard cap on returned rows (bound; total may exceed it).

        Returns:
            ``(rows, total)`` — the bounded ordered partial rows and full count.
        """
        conditions = [
            Memory.workspace_id == workspace_id,
            Memory.context_id == context_id,
            Memory.delivery_mode == DELIVERY_MODE_ALWAYS,
            Memory.deleted_at.is_(None),
        ]
        if trusted_only:
            # #1293: the bootstrap pinned lane is behaviour-establishing (OWASP
            # LLM01/LLM03, F2 invariant 3), so it must not surface the
            # external/connector-origin rows the recall lane already drops.
            # Mirror recall()'s two-part gate exactly: context-level trust signal
            # (connector contexts = 'external') + row-level defense-in-depth.
            conditions.append(
                Memory.context_id.in_(
                    select(Context.id).where(Context.trust_tier == CONTEXT_TRUST_TIER_TRUSTED)
                )
            )
            conditions.append(Memory.source_type != SOURCE_TYPE_CONNECTOR)
        # Fetch limit+1 so the common (untruncated) case needs a single query:
        # if we get <= limit rows, that count IS the exact total. Only when the
        # probe row appears (set exceeds the cap) do we pay for a COUNT to report
        # the true total_available. load_pinned runs every agent turn, so saving
        # the COUNT on the hot path matters.
        result = await self.db.execute(
            select(*self._PINNED_COLUMNS)
            .where(*conditions)
            .order_by(desc(Memory.importance), Memory.created_at.asc(), Memory.id.asc())
            .limit(limit + 1)
        )
        rows = list(result.all())
        if len(rows) <= limit:
            return rows, len(rows)

        total = (
            await self.db.execute(select(func.count(Memory.id)).where(*conditions))
        ).scalar_one()
        return rows[:limit], total

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
