"""Neural Memory Edge Repository (Issue #84 Phase 1).

Provides SQL-based CRUD operations for neural memory edges,
replacing NetworkX JSONB storage with normalized PostgreSQL tables.

Key Features:
    - Efficient edge CRUD with upsert semantics
    - SQL-based BFS traversal (replaces NetworkX)
    - Bulk operations for weight decay
    - Graph statistics calculation
"""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from sqlalchemy import and_, case, delete, desc, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from models.memory import EDGE_ORIGIN_HEBBIAN, EDGE_TYPE_DECLARED_LINK, Memory, NeuralMemoryEdge
from utils.datetime import utcnow
from utils.logger import get_logger

logger = get_logger(__name__)


class NeuralEdgeRepository:
    """Repository for Neural Memory edge operations (Issue #84).

    Replaces NetworkX graph operations with SQL queries for:
    - 10x faster performance on large graphs (10k+ nodes)
    - Better concurrency (row-level locking)
    - Efficient BFS traversal (recursive CTEs)
    - GDPR compliance (CASCADE delete)
    """

    def __init__(self, db: AsyncSession):
        """Initialize repository with database session.

        Args:
            db: SQLAlchemy async session
        """
        self.db = db

    def _validate_isolation_params(self, workspace_id: str | None, context_id: str | None) -> None:
        """Validate workspace_id and context_id for 3-level isolation.

        Issue #273 H-2: Centralized validation to ensure all operations enforce isolation.

        Args:
            workspace_id: Workspace ID
            context_id: Context ID

        Raises:
            ValueError: If workspace_id or context_id is None
        """
        from config.constants import ERROR_MSG_2_LEVEL_ISOLATION

        if not workspace_id or not context_id:
            raise ValueError(
                ERROR_MSG_2_LEVEL_ISOLATION.format(workspace_id=workspace_id, context_id=context_id)
            )

    async def _validate_edge_context_invariant(
        self,
        src_id: UUID,
        dst_id: UUID,
        workspace_id_uuid: UUID,
        context_id_uuid: UUID,
    ) -> None:
        """Assert both endpoint memories live in the edge's (workspace, context).

        The invariant — ``edge.workspace_id == src.workspace_id == dst.workspace_id``
        AND ``edge.context_id == src.context_id == dst.context_id`` — was trusted
        across the write surface prior to this check but never enforced. PR #394
        added a read-path Memory-scope filter as defense in depth; this write-path
        assertion prevents violating rows from entering the table in the first
        place so the invariant can be relied on by downstream graph queries and
        by the GDPR CASCADE on context deletion.

        Fails closed: a missing endpoint — hard-deleted, never existed, OR
        soft-deleted (``Memory.deleted_at IS NOT NULL``) — is treated as a
        violation rather than a silent no-op. A cross-context write that
        cannot be validated is not a write we want to persist, and writing
        new edges to soft-deleted memories would defeat the soft-delete
        semantics the application relies on for undo and GDPR replay.

        Raises:
            ValueError: If either endpoint memory is missing, soft-deleted,
                or its ``(workspace_id, context_id)`` pair does not match
                the edge's.
        """
        stmt = select(Memory.id, Memory.workspace_id, Memory.context_id).where(
            Memory.id.in_([src_id, dst_id]),
            Memory.deleted_at.is_(None),
        )
        result = await self.db.execute(stmt)
        rows = {row.id: (row.workspace_id, row.context_id) for row in result.all()}

        for endpoint_name, endpoint_id in (("src", src_id), ("dst", dst_id)):
            row = rows.get(endpoint_id)
            if row is None:
                raise ValueError(
                    f"edge context invariant violated: {endpoint_name}_id={endpoint_id} "
                    f"memory not found (missing, deleted, or inaccessible in this session)"
                )
            mem_ws, mem_ctx = row
            if mem_ws != workspace_id_uuid or mem_ctx != context_id_uuid:
                raise ValueError(
                    f"edge context invariant violated: {endpoint_name}_id={endpoint_id} "
                    f"belongs to (workspace={mem_ws}, context={mem_ctx}), "
                    f"but edge is being created in "
                    f"(workspace={workspace_id_uuid}, context={context_id_uuid})"
                )

    # ========================================================================
    # Create / Update Operations
    # ========================================================================

    async def create_or_update_edge(
        self,
        user_id: str,
        src_id: UUID,
        dst_id: UUID,
        edge_type: str = "neural_association",
        weight: float = 0.0,
        confidence: float = 1.0,
        edge_metadata: dict | None = None,
        workspace_id: str | None = None,
        context_id: str | None = None,
        protect_declared_link: bool = False,
        return_fresh_edge: bool = True,
        *,
        origin: str = EDGE_ORIGIN_HEBBIAN,  # Issue #722
    ) -> NeuralMemoryEdge:
        """Create or update an edge (upsert) with 3-level isolation.

        Single Collection Migration: Added workspace_id and context_id for isolation.

        Args:
            user_id: User identifier
            src_id: Source memory node ID
            dst_id: Destination memory node ID
            edge_type: Edge type (neural_association, related_to, etc.)
            weight: Hebbian weight (0.0-3.0)
            confidence: Confidence score (0.0-1.0)
            edge_metadata: Optional edge metadata
            workspace_id: Workspace ID (for 3-level isolation)
            context_id: Context ID (for 3-level isolation)
            protect_declared_link: When True, ON CONFLICT preserves the
                existing edge_type if it is "declared_link" (only weight/
                confidence/metadata/last_updated update). Used by automated
                writers (Hebbian co-activation, edge discovery) so user-
                declared links survive co-activation retyping. User-driven
                update_edge calls leave this False so an explicit type
                change still works. (Issue #457)
            return_fresh_edge: When True (default), the returned ORM is
                refreshed from the DB so its Python attributes reflect what
                RETURNING actually wrote (Issue #458). Hot-path callers that
                discard the return value (Hebbian via GraphService.add_edge,
                Sleep edge_discovery) pass False to skip the extra SELECT.
            origin: Edge origin (``EDGE_ORIGIN_HEBBIAN`` default). On upsert,
                non-hebbian origins are preserved — only ``hebbian`` rows can
                be overwritten. Sleep/declared writers should pass the explicit
                enum value.

        Returns:
            Created or updated edge. When ``return_fresh_edge=False`` the
            returned ORM may carry stale Python attributes from the session's
            identity map even though the DB row is correct — only safe to
            ignore the return value.

        Note:
            Uses PostgreSQL ON CONFLICT DO UPDATE for atomic upsert.
        """
        # Issue #273 Review: Enforce 3-level isolation (workspace_id/context_id required)
        if not workspace_id or not context_id:
            raise ValueError(
                f"create_or_update_edge() requires workspace_id and context_id for 3-level isolation. "
                f"Got workspace_id={workspace_id}, context_id={context_id}"
            )

        ws_uuid = UUID(workspace_id)
        ctx_uuid = UUID(context_id)
        await self._validate_edge_context_invariant(src_id, dst_id, ws_uuid, ctx_uuid)

        stmt = insert(NeuralMemoryEdge).values(
            user_id=user_id,
            src_id=src_id,
            dst_id=dst_id,
            edge_type=edge_type,
            weight=weight,
            confidence=confidence,
            edge_metadata=edge_metadata,
            workspace_id=ws_uuid,  # Required for 3-level isolation
            context_id=ctx_uuid,  # Required for 3-level isolation
            origin=origin,  # Issue #722
            created_at=utcnow(),
            last_updated=utcnow(),
        )

        # Issue #457: declared_link rows must survive Hebbian retyping.
        # CASE keeps the existing edge_type when it is declared_link;
        # weight/confidence/metadata/last_updated still update so co-
        # activation can strengthen user-declared links.
        if protect_declared_link:
            edge_type_set = case(
                (
                    NeuralMemoryEdge.edge_type == EDGE_TYPE_DECLARED_LINK,
                    EDGE_TYPE_DECLARED_LINK,
                ),
                else_=stmt.excluded.edge_type,
            )
        else:
            edge_type_set = stmt.excluded.edge_type

        # Issue #722: never demote semantic/declared edges on Hebbian co-recall.
        # Sticky origin — only 'hebbian' can be overwritten by the upsert.
        # Mirrors the protect_declared_link CASE pattern above.
        origin_set = case(
            (NeuralMemoryEdge.origin != EDGE_ORIGIN_HEBBIAN, NeuralMemoryEdge.origin),
            else_=stmt.excluded.origin,
        )

        # ON CONFLICT: Update existing edge
        stmt = stmt.on_conflict_do_update(
            constraint="unique_edge",  # (user_id, src_id, dst_id)
            set_={
                "edge_type": edge_type_set,
                "weight": stmt.excluded.weight,
                "confidence": stmt.excluded.confidence,
                "metadata": stmt.excluded.metadata,
                "origin": origin_set,  # Issue #722: sticky origin
                "last_updated": utcnow(),
            },
        ).returning(NeuralMemoryEdge)

        result = await self.db.execute(stmt)
        edge = result.scalar_one()

        # Issue #458: ON CONFLICT DO UPDATE ... RETURNING delivers the post-
        # update row, but when the same primary key is in SQLAlchemy's
        # identity map (e.g. a prior get_edge or a prior call in the same
        # session) scalar_one() returns the cached ORM instance with stale
        # Python attributes. Refresh so callers that read the return value
        # see what RETURNING actually wrote. Hot-path writers that discard
        # the return (Hebbian, Sleep edge_discovery) skip this extra SELECT.
        if return_fresh_edge:
            await self.db.refresh(edge)

        logger.debug(
            "edge_upserted",
            user_id=user_id,
            src_id=str(src_id),
            dst_id=str(dst_id),
            weight=weight,
        )

        return edge

    async def create_edge_if_absent(
        self,
        user_id: str,
        src_id: UUID,
        dst_id: UUID,
        edge_type: str,
        weight: float,
        confidence: float = 1.0,
        workspace_id: str | None = None,
        context_id: str | None = None,
        *,
        origin: str = EDGE_ORIGIN_HEBBIAN,  # Issue #722
    ) -> NeuralMemoryEdge | None:
        """Create an edge only if no edge exists for (user_id, src_id, dst_id).

        Uses ``ON CONFLICT DO NOTHING`` on the ``unique_edge`` constraint, so
        existing edges — regardless of their ``edge_type`` — are preserved.

        This is the canonical path for **synthetic / seed edges** (k-NN
        cold-start seeding, tag co-occurrence, etc.) where we must not
        overwrite Hebbian-learned edges via upsert.

        Returns:
            The newly created NeuralMemoryEdge, or ``None`` if an edge
            already existed (TOCTOU-safe: no race window).
        """
        # Issue #273 Review: Enforce 3-level isolation
        if not workspace_id or not context_id:
            raise ValueError(
                f"create_edge_if_absent() requires workspace_id and context_id for "
                f"3-level isolation. Got workspace_id={workspace_id}, context_id={context_id}"
            )

        ws_uuid = UUID(workspace_id)
        ctx_uuid = UUID(context_id)
        await self._validate_edge_context_invariant(src_id, dst_id, ws_uuid, ctx_uuid)

        stmt = (
            insert(NeuralMemoryEdge)
            .values(
                user_id=user_id,
                src_id=src_id,
                dst_id=dst_id,
                edge_type=edge_type,
                weight=weight,
                confidence=confidence,
                workspace_id=ws_uuid,
                context_id=ctx_uuid,
                origin=origin,  # Issue #722
                created_at=utcnow(),
                last_updated=utcnow(),
            )
            .on_conflict_do_nothing(constraint="unique_edge")
            .returning(NeuralMemoryEdge)
        )

        result = await self.db.execute(stmt)
        edge = result.scalar_one_or_none()

        if edge is not None:
            logger.debug(
                "edge_inserted_if_absent",
                user_id=user_id,
                src_id=str(src_id),
                dst_id=str(dst_id),
                edge_type=edge_type,
            )

        return edge

    async def update_edge_weight(
        self,
        user_id: str,
        src_id: UUID,
        dst_id: UUID,
        new_weight: float,
        workspace_id: str | None = None,
        context_id: str | None = None,
    ) -> NeuralMemoryEdge | None:
        """Update edge weight (Hebbian learning) with 3-level isolation.

        Single Collection Migration: Added workspace_id and context_id filtering.

        Args:
            user_id: User identifier
            src_id: Source node ID
            dst_id: Destination node ID
            new_weight: New weight value (0.0-3.0)
            workspace_id: Workspace ID (for isolation)
            context_id: Context ID (for isolation)

        Returns:
            Updated edge or None if not found

        Raises:
            ValueError: If workspace_id or context_id is None (Issue #273 H-2)
        """
        # Issue #273 H-2: Enforce workspace_id/context_id for 3-level isolation
        self._validate_isolation_params(workspace_id, context_id)

        conditions = [
            NeuralMemoryEdge.user_id == user_id,
            NeuralMemoryEdge.src_id == src_id,
            NeuralMemoryEdge.dst_id == dst_id,
        ]

        # Single Collection Migration: Add workspace and context filters
        if workspace_id:
            conditions.append(NeuralMemoryEdge.workspace_id == UUID(workspace_id))
        if context_id:
            conditions.append(NeuralMemoryEdge.context_id == UUID(context_id))

        stmt = (
            select(NeuralMemoryEdge)
            .where(and_(*conditions))
            .with_for_update()  # Row-level lock for concurrent updates
        )

        result = await self.db.execute(stmt)
        edge = result.scalar_one_or_none()

        if not edge:
            return None

        edge.weight = max(0.0, min(3.0, new_weight))  # Clamp to [0.0, 3.0]
        edge.last_updated = utcnow()

        logger.debug(
            "edge_weight_updated",
            user_id=user_id,
            src_id=str(src_id),
            dst_id=str(dst_id),
            old_weight=edge.weight,
            new_weight=new_weight,
        )

        return edge

    # ========================================================================
    # Read Operations
    # ========================================================================

    async def get_edge(
        self,
        user_id: str,
        src_id: UUID,
        dst_id: UUID,
        workspace_id: str | None = None,
        context_id: str | None = None,
    ) -> NeuralMemoryEdge | None:
        """Get single edge with optional 3-level isolation.

        Args:
            user_id: User identifier
            src_id: Source node ID
            dst_id: Destination node ID
            workspace_id: Workspace ID (for isolation)
            context_id: Context ID (for isolation)

        Returns:
            Edge or None if not found
        """
        conditions = [
            NeuralMemoryEdge.user_id == user_id,
            NeuralMemoryEdge.src_id == src_id,
            NeuralMemoryEdge.dst_id == dst_id,
        ]

        if workspace_id:
            conditions.append(NeuralMemoryEdge.workspace_id == UUID(workspace_id))
        if context_id:
            conditions.append(NeuralMemoryEdge.context_id == UUID(context_id))

        stmt = select(NeuralMemoryEdge).where(and_(*conditions))
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_outgoing_edges(
        self,
        user_id: str | None,
        src_id: UUID,
        min_weight: float = 0.0,
        edge_types: list[str] | None = None,
        limit: int | None = None,
        workspace_id: str | None = None,
        context_id: str | None = None,
    ) -> list[NeuralMemoryEdge]:
        """Get outgoing edges from a node with 3-level isolation.

        Single Collection Migration: Added workspace_id and context_id filtering.

        ``user_id`` is optional and mirrors the signature of ``get_all_edges`` /
        ``get_stats`` / ``get_top_connected_nodes``: ``None`` returns all
        creators' outgoing edges within the resolved (workspace_id, context_id)
        scope (shared-context reads where authorization is enforced upstream
        by ``PermissionService``); a concrete value keeps the prior
        creator-scoped behavior (private-context or owner-only reads).

        Args:
            user_id: Optional creator filter. ``None`` returns all creators'
                edges in the workspace+context.
            src_id: Source node ID
            min_weight: Minimum edge weight threshold
            edge_types: Filter by edge types
            limit: Maximum edges to return
            workspace_id: Workspace ID (for isolation)
            context_id: Context ID (for isolation)

        Returns:
            List of outgoing edges sorted by weight descending

        Raises:
            ValueError: If workspace_id or context_id is None (Issue #273 H-2)
        """
        # Issue #273 H-2: Enforce workspace_id/context_id for 3-level isolation
        self._validate_isolation_params(workspace_id, context_id)

        conditions = [
            NeuralMemoryEdge.src_id == src_id,
            NeuralMemoryEdge.weight >= min_weight,
        ]
        if user_id is not None:
            conditions.append(NeuralMemoryEdge.user_id == user_id)

        # Single Collection Migration: Add workspace and context filters
        if workspace_id:
            conditions.append(NeuralMemoryEdge.workspace_id == UUID(workspace_id))
        if context_id:
            conditions.append(NeuralMemoryEdge.context_id == UUID(context_id))

        stmt = (
            select(NeuralMemoryEdge)
            .where(and_(*conditions))
            .order_by(desc(NeuralMemoryEdge.weight))
        )

        if edge_types:
            stmt = stmt.where(NeuralMemoryEdge.edge_type.in_(edge_types))

        if limit:
            stmt = stmt.limit(limit)

        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def get_incoming_edges(
        self,
        user_id: str | None,
        dst_id: UUID,
        min_weight: float = 0.0,
        edge_types: list[str] | None = None,
        limit: int | None = None,
        workspace_id: str | None = None,
        context_id: str | None = None,
    ) -> list[NeuralMemoryEdge]:
        """Get incoming edges to a node with 3-level isolation.

        Single Collection Migration: Added workspace_id and context_id filtering.

        ``user_id`` is optional and mirrors the signature of ``get_all_edges`` /
        ``get_stats`` / ``get_top_connected_nodes``: ``None`` returns all
        creators' incoming edges within the resolved (workspace_id, context_id)
        scope (shared-context reads where authorization is enforced upstream
        by ``PermissionService``); a concrete value keeps the prior
        creator-scoped behavior (private-context or owner-only reads).

        Args:
            user_id: Optional creator filter. ``None`` returns all creators'
                edges in the workspace+context.
            dst_id: Destination node ID
            min_weight: Minimum edge weight threshold
            edge_types: Filter by edge types
            limit: Maximum edges to return
            workspace_id: Workspace ID (for isolation)
            context_id: Context ID (for isolation)

        Returns:
            List of incoming edges sorted by weight descending

        Raises:
            ValueError: If workspace_id or context_id is None (Issue #273 H-2)
        """
        # Issue #273 H-2: Enforce workspace_id/context_id for 3-level isolation
        self._validate_isolation_params(workspace_id, context_id)

        conditions = [
            NeuralMemoryEdge.dst_id == dst_id,
            NeuralMemoryEdge.weight >= min_weight,
        ]
        if user_id is not None:
            conditions.append(NeuralMemoryEdge.user_id == user_id)

        # Single Collection Migration: Add workspace and context filters
        if workspace_id:
            conditions.append(NeuralMemoryEdge.workspace_id == UUID(workspace_id))
        if context_id:
            conditions.append(NeuralMemoryEdge.context_id == UUID(context_id))

        stmt = (
            select(NeuralMemoryEdge)
            .where(and_(*conditions))
            .order_by(desc(NeuralMemoryEdge.weight))
        )

        if edge_types:
            stmt = stmt.where(NeuralMemoryEdge.edge_type.in_(edge_types))

        if limit:
            stmt = stmt.limit(limit)

        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def get_all_edges(
        self,
        user_id: str | None = None,
        min_weight: float = 0.0,
        workspace_id: str | None = None,
        context_id: str | None = None,
    ) -> list[NeuralMemoryEdge]:
        """Get all edges in a context with 3-level isolation.

        Single Collection Migration: Added workspace_id and context_id filtering.
        Issue #383: ``user_id`` is now optional — when ``None``, all edges in the
        workspace+context are returned regardless of creator (shared context
        mode). When set, results are restricted to that creator (private context
        mode or creator-scoped admin paths).

        Args:
            user_id: Optional creator filter. ``None`` returns all edges in
                the workspace+context (use for shared-context reads where
                authorization is enforced upstream).
            min_weight: Minimum edge weight threshold
            workspace_id: Workspace ID (for isolation)
            context_id: Context ID (for isolation)

        Returns:
            List of edges in the context that pass the creator filter.

        Raises:
            ValueError: If workspace_id or context_id is None (Issue #273 H-2)
        """
        # Issue #273 H-2: Enforce workspace_id/context_id for 3-level isolation
        self._validate_isolation_params(workspace_id, context_id)

        conditions = [NeuralMemoryEdge.weight >= min_weight]
        if user_id is not None:
            conditions.append(NeuralMemoryEdge.user_id == user_id)

        # Single Collection Migration: Use workspace_id/context_id for filtering
        if workspace_id:
            conditions.append(NeuralMemoryEdge.workspace_id == UUID(workspace_id))
        if context_id:
            conditions.append(NeuralMemoryEdge.context_id == UUID(context_id))

        stmt = select(NeuralMemoryEdge).where(and_(*conditions))

        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    # ========================================================================
    # Graph Traversal (BFS - replaces NetworkX)
    # ========================================================================

    async def get_neighbors(
        self,
        user_id: str,
        node_id: UUID,
        max_hops: int = 1,
        min_weight: float = 0.0,
        limit: int | None = None,
        workspace_id: str | None = None,
        context_id: str | None = None,
    ) -> list[tuple[UUID, float, int]]:
        """BFS traversal to find connected nodes with 3-level isolation.

        Single Collection Migration: Added workspace_id and context_id filtering.

        Args:
            user_id: User identifier
            node_id: Starting node ID
            max_hops: Maximum traversal depth
            min_weight: Minimum edge weight threshold
            limit: Maximum nodes to return
            workspace_id: Workspace ID (for isolation)
            context_id: Context ID (for isolation)

        Returns:
            List of (node_id, cumulative_weight, hop_distance) tuples
            sorted by cumulative_weight descending

        Raises:
            ValueError: If workspace_id or context_id is None, or max_hops exceeds limit (Issue #273 H-2, M-5)

        Performance:
            - O(N + E) where N=nodes, E=edges in subgraph
            - Uses row-level locking for concurrency
            - 10x faster than NetworkX for large graphs
        """
        # Issue #273 H-2: Enforce workspace_id/context_id for 3-level isolation
        self._validate_isolation_params(workspace_id, context_id)

        # Issue #273 M-5: Validate max_hops to prevent memory exhaustion
        from config.constants import MAX_GRAPH_DEPTH

        if max_hops < 1:
            raise ValueError(f"max_hops must be >= 1, got {max_hops}")
        if max_hops > MAX_GRAPH_DEPTH:
            raise ValueError(
                f"max_hops {max_hops} exceeds maximum allowed depth {MAX_GRAPH_DEPTH}. "
                f"This limit prevents memory exhaustion during graph traversal."
            )

        visited: dict[UUID, tuple[float, int]] = {}
        queue: list[tuple[UUID, float, int]] = [(node_id, 1.0, 0)]

        while queue:
            current_id, current_weight, current_hop = queue.pop(0)

            # Skip if already visited or exceeded max hops
            if current_id in visited or current_hop >= max_hops:
                continue

            # Record visit (exclude starting node from results)
            if current_id != node_id:
                visited[current_id] = (current_weight, current_hop)

            # Get outgoing edges with 3-level isolation
            outgoing = await self.get_outgoing_edges(
                user_id=user_id,
                src_id=current_id,
                min_weight=min_weight,
                workspace_id=workspace_id,
                context_id=context_id,
            )

            # Add neighbors to queue with decayed weight
            # Issue #273 L-2: Use constant instead of magic number
            from config.constants import SPREAD_DECAY

            for edge in outgoing:
                if edge.dst_id not in visited:
                    new_weight = current_weight * edge.weight * SPREAD_DECAY
                    queue.append((edge.dst_id, new_weight, current_hop + 1))

        # Convert to sorted list
        results = [(node_id, weight, hop) for node_id, (weight, hop) in visited.items()]
        results.sort(key=lambda x: x[1], reverse=True)

        return results[:limit] if limit else results

    # ========================================================================
    # Bulk Operations
    # ========================================================================

    async def bulk_decay_weights(
        self,
        user_id: str,
        decay_factor: float = 0.95,
        *,
        only_origin: str | None = None,  # Issue #722
    ) -> int:
        """Apply exponential decay to all edge weights (Issue #84 Phase 1).

        Args:
            user_id: User identifier
            decay_factor: Multiplicative decay (0.95 = 5% decay)
            only_origin: When set, only edges with this origin are decayed.
                Pass ``EDGE_ORIGIN_HEBBIAN`` to skip semantic edges during
                Hebbian maintenance cycles. (Issue #722)

        Returns:
            Number of edges updated

        Formula:
            w_new = w_old * decay_factor
        """
        stmt = select(NeuralMemoryEdge).where(NeuralMemoryEdge.user_id == user_id)
        if only_origin is not None:
            stmt = stmt.where(NeuralMemoryEdge.origin == only_origin)
        stmt = stmt.with_for_update()

        result = await self.db.execute(stmt)
        edges = list(result.scalars().all())

        count = 0
        for edge in edges:
            edge.weight = edge.weight * decay_factor
            edge.last_updated = utcnow()
            count += 1

        logger.info(
            "bulk_decay_applied", user_id=user_id, edges_updated=count, only_origin=only_origin
        )
        return count

    async def prune_weak_edges(
        self,
        user_id: str,
        weight_threshold: float = 0.01,
        *,
        only_origin: str | None = None,  # Issue #722
    ) -> int:
        """Remove edges below weight threshold.

        Args:
            user_id: User identifier
            weight_threshold: Minimum weight to keep
            only_origin: When set, only edges with this origin are pruned.
                Pass ``EDGE_ORIGIN_HEBBIAN`` to preserve semantic edges
                during Hebbian maintenance cycles. (Issue #722)

        Returns:
            Number of edges deleted
        """
        conds = [
            NeuralMemoryEdge.user_id == user_id,
            NeuralMemoryEdge.weight < weight_threshold,
        ]
        if only_origin is not None:
            conds.append(NeuralMemoryEdge.origin == only_origin)
        stmt = delete(NeuralMemoryEdge).where(and_(*conds))

        result = cast(CursorResult[Any], await self.db.execute(stmt))
        deleted_count = result.rowcount or 0

        logger.info(
            "weak_edges_pruned",
            user_id=user_id,
            threshold=weight_threshold,
            deleted=deleted_count,
            only_origin=only_origin,
        )

        return deleted_count

    # ========================================================================
    # Statistics & Metrics
    # ========================================================================

    async def get_stats(
        self,
        user_id: str | None = None,
        workspace_id: str | None = None,
        context_id: str | None = None,
    ) -> dict[str, int | float]:
        """Get graph statistics for a context with 3-level isolation.

        Single Collection Migration: Added workspace_id and context_id filtering.
        Issue #383: ``user_id`` is now optional — ``None`` aggregates over all
        creators in the workspace+context (shared context), otherwise restricts
        to that single creator (private context).

        **Caller contract**: ``workspace_id`` and ``context_id`` are **always**
        required (enforced by ``_validate_isolation_params`` below). The
        ``user_id`` axis is the only optional one. "All edges authored by this
        user across all contexts" is NOT a supported use case for this method —
        that access pattern would bypass the 3-level isolation and is rejected
        by the validator.

        Args:
            user_id: Optional creator filter. ``None`` aggregates over all
                creators in the workspace+context.
            workspace_id: Workspace ID (required for isolation)
            context_id: Context ID (required for isolation)

        Returns:
            Dictionary with graph metrics:
                - total_edges: Total edge count
                - avg_weight: Average edge weight
                - max_weight: Maximum edge weight
                - min_weight: Minimum edge weight

        Raises:
            ValueError: If workspace_id or context_id is None (Issue #273 H-2)
        """
        # Issue #273 H-2: Enforce workspace_id/context_id for 3-level isolation
        self._validate_isolation_params(workspace_id, context_id)

        # Build conditions
        conditions: list = []
        if user_id is not None:
            conditions.append(NeuralMemoryEdge.user_id == user_id)

        # Single Collection Migration: Use workspace_id/context_id for filtering
        if workspace_id:
            conditions.append(NeuralMemoryEdge.workspace_id == UUID(workspace_id))
        if context_id:
            conditions.append(NeuralMemoryEdge.context_id == UUID(context_id))

        # Count edges
        count_stmt = select(func.count(NeuralMemoryEdge.id)).where(and_(*conditions))
        count_result = await self.db.execute(count_stmt)
        total_edges = count_result.scalar() or 0

        if total_edges == 0:
            return {
                "total_edges": 0,
                "avg_weight": 0.0,
                "max_weight": 0.0,
                "min_weight": 0.0,
            }

        # Aggregate statistics
        stats_stmt = select(
            func.avg(NeuralMemoryEdge.weight).label("avg_weight"),
            func.max(NeuralMemoryEdge.weight).label("max_weight"),
            func.min(NeuralMemoryEdge.weight).label("min_weight"),
        ).where(and_(*conditions))

        stats_result = await self.db.execute(stats_stmt)
        stats_row = stats_result.one()

        return {
            "total_edges": total_edges,
            "avg_weight": float(stats_row.avg_weight or 0.0),
            "max_weight": float(stats_row.max_weight or 0.0),
            "min_weight": float(stats_row.min_weight or 0.0),
        }

    async def get_node_degree(self, user_id: str, node_id: UUID) -> tuple[int, int]:
        """Get node degree (in-degree, out-degree).

        Args:
            user_id: User identifier
            node_id: Memory node ID

        Returns:
            Tuple of (in_degree, out_degree)
        """
        # Count incoming edges
        in_stmt = select(func.count(NeuralMemoryEdge.id)).where(
            and_(
                NeuralMemoryEdge.user_id == user_id,
                NeuralMemoryEdge.dst_id == node_id,
            )
        )
        in_result = await self.db.execute(in_stmt)
        in_degree = in_result.scalar() or 0

        # Count outgoing edges
        out_stmt = select(func.count(NeuralMemoryEdge.id)).where(
            and_(
                NeuralMemoryEdge.user_id == user_id,
                NeuralMemoryEdge.src_id == node_id,
            )
        )
        out_result = await self.db.execute(out_stmt)
        out_degree = out_result.scalar() or 0

        return (in_degree, out_degree)

    async def get_top_connected_nodes(
        self,
        user_id: str | None = None,
        limit: int = 10,
        workspace_id: str | None = None,
        context_id: str | None = None,
    ) -> list[tuple[UUID, int]]:
        """Get most connected nodes (highest total degree).

        Single Collection Migration: Added workspace_id/context_id for filtering.
        Issue #383: ``user_id`` is now optional — ``None`` aggregates across all
        creators in the workspace+context (shared context mode).

        Args:
            user_id: Optional creator filter. ``None`` = all creators.
            limit: Number of top nodes to return
            workspace_id: Workspace ID (for isolation)
            context_id: Context ID (for isolation)

        Returns:
            List of (node_id, total_degree) tuples sorted by degree descending

        Raises:
            ValueError: If workspace_id or context_id is None (Issue #273 H-2).
                Mirrors the guard on sibling read methods so ``user_id=None``
                cannot devolve into an unscoped full-table aggregation
                (Copilot catch on PR #394 loop 2).
        """
        # Issue #273 H-2 / Issue #383: Enforce workspace_id/context_id even when
        # user_id is None (shared-context mode) — without both, the shared mode
        # would aggregate across tenants.
        self._validate_isolation_params(workspace_id, context_id)

        # Build filter conditions
        conditions: list = []
        if user_id is not None:
            conditions.append(NeuralMemoryEdge.user_id == user_id)
        if workspace_id:
            conditions.append(NeuralMemoryEdge.workspace_id == UUID(workspace_id))
        if context_id:
            conditions.append(NeuralMemoryEdge.context_id == UUID(context_id))

        # Union of src_id and dst_id with counts
        stmt = (
            select(
                NeuralMemoryEdge.src_id.label("node_id"),
                func.count().label("degree"),
            )
            .where(and_(*conditions))
            .group_by(NeuralMemoryEdge.src_id)
            .union_all(
                select(
                    NeuralMemoryEdge.dst_id.label("node_id"),
                    func.count().label("degree"),
                )
                .where(and_(*conditions))
                .group_by(NeuralMemoryEdge.dst_id)
            )
        )

        # Aggregate union results
        final_stmt = (
            select(
                stmt.c.node_id,
                func.sum(stmt.c.degree).label("total_degree"),
            )
            .group_by(stmt.c.node_id)
            .order_by(desc("total_degree"))
            .limit(limit)
        )

        result = await self.db.execute(final_stmt)
        return [(row.node_id, row.total_degree) for row in result.all()]

    # ========================================================================
    # Delete Operations
    # ========================================================================

    async def delete_edge(
        self,
        user_id: str,
        src_id: UUID,
        dst_id: UUID,
        workspace_id: str | None = None,
        context_id: str | None = None,
    ) -> bool:
        """Delete a single edge with optional 3-level isolation.

        Args:
            user_id: User identifier
            src_id: Source node ID
            dst_id: Destination node ID
            workspace_id: Workspace ID (for isolation)
            context_id: Context ID (for isolation)

        Returns:
            True if deleted, False if not found
        """
        conditions = [
            NeuralMemoryEdge.user_id == user_id,
            NeuralMemoryEdge.src_id == src_id,
            NeuralMemoryEdge.dst_id == dst_id,
        ]

        if workspace_id:
            conditions.append(NeuralMemoryEdge.workspace_id == UUID(workspace_id))
        if context_id:
            conditions.append(NeuralMemoryEdge.context_id == UUID(context_id))

        stmt = delete(NeuralMemoryEdge).where(and_(*conditions))

        result = cast(CursorResult[Any], await self.db.execute(stmt))
        deleted = (result.rowcount or 0) > 0

        if deleted:
            logger.debug("edge_deleted", user_id=user_id, src_id=str(src_id), dst_id=str(dst_id))

        return deleted

    async def delete_node_edges(
        self,
        user_id: str,
        node_id: UUID,
        workspace_id: str | None = None,
        context_id: str | None = None,
    ) -> int:
        """Delete all edges connected to a node with 3-level isolation.

        Single Collection Migration: Added workspace_id and context_id filtering.

        Args:
            user_id: User identifier
            node_id: Node ID to delete edges for
            workspace_id: Workspace ID (for isolation)
            context_id: Context ID (for isolation)

        Returns:
            Number of edges deleted

        Raises:
            ValueError: If workspace_id or context_id is None (Issue #273 H-2)
        """
        # Issue #273 H-2: Enforce workspace_id/context_id for 3-level isolation
        self._validate_isolation_params(workspace_id, context_id)

        conditions = [
            NeuralMemoryEdge.user_id == user_id,
            or_(
                NeuralMemoryEdge.src_id == node_id,
                NeuralMemoryEdge.dst_id == node_id,
            ),
        ]

        # Single Collection Migration: Add workspace and context filters
        if workspace_id:
            conditions.append(NeuralMemoryEdge.workspace_id == UUID(workspace_id))
        if context_id:
            conditions.append(NeuralMemoryEdge.context_id == UUID(context_id))

        stmt = delete(NeuralMemoryEdge).where(and_(*conditions))

        result = cast(CursorResult[Any], await self.db.execute(stmt))
        deleted_count = result.rowcount or 0

        logger.info(
            "node_edges_deleted",
            user_id=user_id,
            node_id=str(node_id),
            count=deleted_count,
            workspace_id=workspace_id,
            context_id=context_id,
        )

        return deleted_count

    async def transfer_edges(
        self,
        from_node_id: UUID,
        to_node_id: UUID,
        user_id: str,
        workspace_id: str | None = None,
        context_id: str | None = None,
    ) -> int:
        """Transfer all edges from one node to another.

        Issue #101: Used by Sleep Maintenance dedup/merge to reassign
        edges from a merged (loser) memory to the winner memory.
        Self-loops (where the edge would connect to_node to itself) are deleted.

        Args:
            from_node_id: Source node (loser being merged)
            to_node_id: Target node (winner keeping edges)
            user_id: User identifier
            workspace_id: Workspace ID (for isolation)
            context_id: Context ID (for isolation)

        Returns:
            Number of edges transferred
        """
        self._validate_isolation_params(workspace_id, context_id)

        transferred = 0

        # Get all edges connected to from_node
        conditions = [
            NeuralMemoryEdge.user_id == user_id,
            or_(
                NeuralMemoryEdge.src_id == from_node_id,
                NeuralMemoryEdge.dst_id == from_node_id,
            ),
        ]
        if workspace_id:
            conditions.append(NeuralMemoryEdge.workspace_id == UUID(workspace_id))
        if context_id:
            conditions.append(NeuralMemoryEdge.context_id == UUID(context_id))

        stmt = select(NeuralMemoryEdge).where(and_(*conditions))
        result = await self.db.execute(stmt)
        edges = list(result.scalars().all())

        for edge in edges:
            new_src = to_node_id if edge.src_id == from_node_id else edge.src_id
            new_dst = to_node_id if edge.dst_id == from_node_id else edge.dst_id

            # Skip self-loops
            if new_src == new_dst:
                await self.db.delete(edge)
                continue

            # Check for conflicting edge (unique_edge constraint).
            #
            # The DB `unique_edge` UNIQUE constraint covers
            # `(user_id, src_id, dst_id)` only — NOT `edge_type`
            # (see `models/memory.py` UniqueConstraint definition and
            # alembic migration `d18bcb6512e2_add_unique_edge_constraint_*`).
            # Including `edge_type` here previously caused #428: a transfer
            # that produced (C, B, related_to) when (C, B, depends_on) already
            # existed was reported as "no conflict" by this 4-col check, then
            # rejected by the DB 3-col constraint as IntegrityError, aborting
            # the entire sleep run on contexts where any such pair existed.
            #
            # If the existing edge has a different `edge_type`, the
            # weight-based winner-keeping logic below still applies (one
            # edge per (src, dst) pair, matching DB semantics).
            conflict_conditions = [
                NeuralMemoryEdge.user_id == user_id,
                NeuralMemoryEdge.src_id == new_src,
                NeuralMemoryEdge.dst_id == new_dst,
            ]
            if workspace_id:
                conflict_conditions.append(NeuralMemoryEdge.workspace_id == UUID(workspace_id))
            if context_id:
                conflict_conditions.append(NeuralMemoryEdge.context_id == UUID(context_id))
            conflict_stmt = select(NeuralMemoryEdge).where(and_(*conflict_conditions))
            conflict_result = await self.db.execute(conflict_stmt)
            existing_edge = conflict_result.scalar_one_or_none()

            if existing_edge and existing_edge.id != edge.id:
                # Keep the edge with higher weight, delete the other
                if existing_edge.weight >= edge.weight:
                    await self.db.delete(edge)
                else:
                    await self.db.delete(existing_edge)
                    edge.src_id = new_src
                    edge.dst_id = new_dst
                    edge.last_updated = utcnow()
                    transferred += 1
                continue

            # Update edge to point to winner
            edge.src_id = new_src
            edge.dst_id = new_dst
            edge.last_updated = utcnow()
            transferred += 1

        logger.info(
            "edges_transferred",
            from_node=str(from_node_id),
            to_node=str(to_node_id),
            count=transferred,
        )

        return transferred

    async def delete_semantic_edges_for_dead_pairs(self) -> int:
        """Delete origin='semantic' edges whose src or dst memory is soft-deleted.

        Issue #722: monthly hygiene for semantic edges, which are exempt from
        the Hebbian decay/prune loop and so need their own dead-endpoint sweep.

        Note:
            The ``memories.deleted_at`` column is not indexed; this subquery
            falls back to a sequential scan. Tolerable at monthly cadence for
            current scale (≤1M memory rows). Add a partial index
            (``WHERE deleted_at IS NOT NULL``) before this query is run more
            frequently or on much larger tables.

        Returns:
            Number of edges deleted.
        """
        from models.memory import EDGE_ORIGIN_SEMANTIC, Memory

        sub_dead = select(Memory.id).where(Memory.deleted_at.is_not(None)).scalar_subquery()

        stmt = delete(NeuralMemoryEdge).where(
            and_(
                NeuralMemoryEdge.origin == EDGE_ORIGIN_SEMANTIC,
                or_(
                    NeuralMemoryEdge.src_id.in_(sub_dead),
                    NeuralMemoryEdge.dst_id.in_(sub_dead),
                ),
            )
        )
        result = cast(CursorResult[Any], await self.db.execute(stmt))
        return result.rowcount or 0

    async def delete_all_edges(self, user_id: str) -> int:
        """Delete all edges for a user (graph reset).

        Args:
            user_id: User identifier

        Returns:
            Number of edges deleted
        """
        stmt = delete(NeuralMemoryEdge).where(NeuralMemoryEdge.user_id == user_id)

        result = cast(CursorResult[Any], await self.db.execute(stmt))
        deleted_count = result.rowcount or 0

        logger.warning("all_edges_deleted", user_id=user_id, count=deleted_count)

        return deleted_count
