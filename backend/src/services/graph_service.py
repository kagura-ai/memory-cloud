"""Graph Service for SQL-based Neural Memory operations (Issue #84 Phase 1).

Replaces NetworkX JSONB storage with PostgreSQL edge table for:
- 10x faster graph operations
- Better concurrency (row-level locking)
- Efficient BFS traversal (SQL recursive CTEs)
- Scalability to 10k+ nodes

Migration: backend/scripts/migrate_networkx_to_sql.py
"""

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from models.memory import (
    EDGE_TYPE_DECLARED_LINK,
    EDGE_TYPE_DEPENDS_ON,
    EDGE_TYPE_LEARNED_FROM,
    EDGE_TYPE_NEURAL_ASSOCIATION,
    EDGE_TYPE_RELATED_TO,
    EDGE_TYPE_SEMANTIC_SIMILARITY,
    EDGE_TYPE_TAG_COOCCURRENCE,
)
from repositories.neural_edge import NeuralEdgeRepository
from utils.datetime import to_utc_iso
from utils.logger import get_logger

logger = get_logger(__name__)


class GraphService:
    """SQL-based graph service for Neural Memory (Issue #84).

    Manages a directed graph of memory nodes with weighted edges
    representing neural associations learned through Hebbian learning.

    **Breaking Change from v0.7.x**:
        - Replaced NetworkX with SQL backend
        - All methods are now async

    Node Types:
        - memory: Memory nodes (corresponds to memories table)
        - topic: Topic/entity nodes (Phase 3)
        - user: User nodes (future)

    Edge Types (mirrors the DB CHECK constraint and ``EDGE_TYPE_*`` constants):
        - neural_association: Learned associations (Hebbian)
        - related_to: Semantic relationship
        - depends_on: Dependency relationship
        - learned_from: Learning source
        - semantic_similarity: k-NN cold-start seeding (#221)
        - declared_link: Client-declared explicit link (#215)
        - tag_cooccurrence: Tag co-occurrence cold-start seeding (#223)

    Attributes:
        user_id: Owner user ID (GDPR compliance)
        edge_repo: NeuralEdgeRepository for SQL operations
        db: AsyncSession for database access
    """

    # Sentinel for ``stats(owner_filter=...)`` to distinguish "caller did not
    # pass the argument, use the legacy self.user_id default" from "caller
    # explicitly requested the no-filter (shared-context) mode by passing None".
    # Without this sentinel, the shared-mode default would silently regress
    # existing per-user callers (sleep/consolidation, neural_tasks) that invoke
    # ``stats()`` with no arguments and rely on implicit creator scoping.
    _STATS_OWNER_DEFAULT: Any = object()

    NODE_TYPES = ["memory", "user", "topic"]
    # Issue #506: full set of edge_types accepted by the DB CHECK constraint
    # (mirrors ``mcp_server/tools/edge.py::VALID_EDGE_TYPES`` from PR #507).
    # ``frozenset`` for immutability + O(1) ``not in`` lookup at the validator
    # in ``add_edge``. Sourced from ``EDGE_TYPE_*`` constants so this set
    # cannot drift from the schema literal.
    EDGE_TYPES: frozenset[str] = frozenset(
        {
            EDGE_TYPE_NEURAL_ASSOCIATION,
            EDGE_TYPE_RELATED_TO,
            EDGE_TYPE_DEPENDS_ON,
            EDGE_TYPE_LEARNED_FROM,
            EDGE_TYPE_SEMANTIC_SIMILARITY,
            EDGE_TYPE_DECLARED_LINK,
            EDGE_TYPE_TAG_COOCCURRENCE,
        }
    )

    def __init__(
        self,
        user_id: str,
        db: AsyncSession,
        workspace_id: str | None = None,
        context_id: str | None = None,
    ):
        """Initialize graph service with SQL backend and 3-level isolation.

        Single Collection Migration: Added workspace_id and context_id for isolation.

        Args:
            user_id: User identifier for GDPR compliance
            db: SQLAlchemy async session
            workspace_id: Workspace ID (for 3-level isolation)
            context_id: Context ID (for 3-level isolation)

        Note:
            **Breaking change**: Requires AsyncSession parameter (v0.8.0+)
        """
        self.user_id = user_id
        self.db = db
        self.edge_repo = NeuralEdgeRepository(db)
        self.workspace_id = workspace_id  # Single Collection Migration
        self.context_id = context_id  # Single Collection Migration

    # ========================================================================
    # Node Operations (Simplified - nodes stored in memories table)
    # ========================================================================

    async def add_node(
        self,
        node_id: str | UUID,
        node_type: str = "memory",
        data: dict[str, Any] | None = None,
    ) -> None:
        """Add node to graph (no-op in SQL backend).

        In SQL backend, nodes exist implicitly via memories table.
        This method validates node_type for compatibility.

        Args:
            node_id: Unique node identifier (memory UUID)
            node_type: Node type (default: "memory")
            data: Additional node data (ignored in SQL backend)

        Raises:
            ValueError: If node_type is invalid

        Note:
            SQL backend manages nodes via memories table, not graph_service.
        """
        if node_type not in self.NODE_TYPES:
            raise ValueError(f"Invalid node_type: {node_type}. Must be one of {self.NODE_TYPES}")

        logger.debug("node_validated", node_id=str(node_id), node_type=node_type)

    async def has_node(self, node_id: str | UUID) -> bool:
        """Check if node exists (via edges table).

        Args:
            node_id: Node ID

        Returns:
            True if node has any edges
        """
        node_uuid = UUID(node_id) if isinstance(node_id, str) else node_id
        in_deg, out_deg = await self.edge_repo.get_node_degree(self.user_id, node_uuid)
        return (in_deg + out_deg) > 0

    async def remove_node(self, node_id: str | UUID) -> None:
        """Remove node from graph (delete all connected edges).

        Args:
            node_id: Node ID to remove

        Note:
            Deletes all edges where node is src or dst.
        """
        node_uuid = UUID(node_id) if isinstance(node_id, str) else node_id
        deleted_count = await self.edge_repo.delete_node_edges(
            user_id=self.user_id,
            node_id=node_uuid,
            workspace_id=self.workspace_id,  # Single Collection Migration
            context_id=self.context_id,  # Single Collection Migration
        )

        logger.info(
            "node_removed",
            user_id=self.user_id,
            node_id=str(node_uuid),
            edges_deleted=deleted_count,
        )

    # ========================================================================
    # Edge Operations
    # ========================================================================

    async def add_edge(
        self,
        src_id: str | UUID,
        dst_id: str | UUID,
        rel_type: str = EDGE_TYPE_NEURAL_ASSOCIATION,
        weight: float = 1.0,
        edge_metadata: dict[str, Any] | None = None,
        confidence: float = 1.0,
    ) -> None:
        """Add edge between nodes.

        Args:
            src_id: Source node ID
            dst_id: Destination node ID
            rel_type: Relationship type (default: "neural_association")
            weight: Edge weight (0.0-3.0 for Neural Memory)
            edge_metadata: Additional edge metadata
            confidence: Confidence score (0.0-1.0)

        Raises:
            ValueError: If rel_type is invalid
        """
        if rel_type not in self.EDGE_TYPES:
            raise ValueError(f"Invalid rel_type: {rel_type}. Must be one of {self.EDGE_TYPES}")

        src_uuid = UUID(src_id) if isinstance(src_id, str) else src_id
        dst_uuid = UUID(dst_id) if isinstance(dst_id, str) else dst_id

        await self.edge_repo.create_or_update_edge(
            user_id=self.user_id,
            src_id=src_uuid,
            dst_id=dst_uuid,
            edge_type=rel_type,
            weight=weight,
            confidence=confidence,
            edge_metadata=edge_metadata,
            workspace_id=self.workspace_id,  # Single Collection Migration
            context_id=self.context_id,  # Single Collection Migration
            # Issue #457: Hebbian co-activation flows through here; never let
            # automated retyping clobber a user-declared link.
            protect_declared_link=True,
            # Hebbian / GraphService.add_edge callers discard the return value
            # — skip the post-upsert SELECT (Issue #458 doesn't apply here).
            return_fresh_edge=False,
        )

        logger.debug(
            "edge_added",
            user_id=self.user_id,
            src_id=str(src_uuid),
            dst_id=str(dst_uuid),
            weight=weight,
        )

    async def get_edge(self, src_id: str | UUID, dst_id: str | UUID) -> dict[str, Any] | None:
        """Get edge data.

        Args:
            src_id: Source node ID
            dst_id: Destination node ID

        Returns:
            Edge data dict or None if not found
        """
        src_uuid = UUID(src_id) if isinstance(src_id, str) else src_id
        dst_uuid = UUID(dst_id) if isinstance(dst_id, str) else dst_id

        edge = await self.edge_repo.get_edge(self.user_id, src_uuid, dst_uuid)

        if not edge:
            return None

        return {
            "src": str(edge.src_id),
            "dst": str(edge.dst_id),
            "type": edge.edge_type,
            "weight": edge.weight,
            "confidence": edge.confidence,
            "metadata": edge.edge_metadata,
            "created_at": to_utc_iso(edge.created_at),
            "last_updated": to_utc_iso(edge.last_updated),
        }

    async def has_edge(self, src_id: str | UUID, dst_id: str | UUID) -> bool:
        """Check if edge exists.

        Args:
            src_id: Source node ID
            dst_id: Destination node ID

        Returns:
            True if edge exists
        """
        src_uuid = UUID(src_id) if isinstance(src_id, str) else src_id
        dst_uuid = UUID(dst_id) if isinstance(dst_id, str) else dst_id

        edge = await self.edge_repo.get_edge(self.user_id, src_uuid, dst_uuid)
        return edge is not None

    async def remove_edge(self, src_id: str | UUID, dst_id: str | UUID) -> None:
        """Remove edge from graph.

        Args:
            src_id: Source node ID
            dst_id: Destination node ID
        """
        src_uuid = UUID(src_id) if isinstance(src_id, str) else src_id
        dst_uuid = UUID(dst_id) if isinstance(dst_id, str) else dst_id

        deleted = await self.edge_repo.delete_edge(self.user_id, src_uuid, dst_uuid)

        if deleted:
            logger.debug(
                "edge_removed", user_id=self.user_id, src_id=str(src_uuid), dst_id=str(dst_uuid)
            )

    # ========================================================================
    # Graph Traversal
    # ========================================================================

    async def get_neighbors(self, node_id: str | UUID, max_hops: int = 1) -> list[str]:
        """Get all neighbors via BFS traversal.

        Args:
            node_id: Node ID
            max_hops: Maximum traversal depth (default: 1 = direct neighbors)

        Returns:
            List of neighbor node IDs
        """
        node_uuid = UUID(node_id) if isinstance(node_id, str) else node_id

        # BFS traversal via SQL with 3-level isolation
        neighbors_data = await self.edge_repo.get_neighbors(
            user_id=self.user_id,
            node_id=node_uuid,
            max_hops=max_hops,
            min_weight=0.0,
            workspace_id=self.workspace_id,  # Single Collection Migration
            context_id=self.context_id,  # Single Collection Migration
        )

        # Extract node IDs
        return [str(neighbor_id) for neighbor_id, _, _ in neighbors_data]

    # ========================================================================
    # Statistics
    # ========================================================================

    async def stats(self, *, owner_filter: Any = _STATS_OWNER_DEFAULT) -> dict[str, Any]:
        """Get graph statistics with 3-level isolation.

        Issue #383: visibility-aware. ``owner_filter`` controls creator scoping:

        - **Omitted** (default, backward-compatible): filter by ``self.user_id``
          — the pre-#383 "per-user metrics" semantics that sleep/consolidation,
          neural_tasks, and internal callers rely on. Does NOT aggregate across
          the whole workspace, which would distort consolidation heuristics.
        - ``None`` (explicit): no creator filter — aggregate across all creators
          in workspace+context (shared-context HTTP reads). Only pass this when
          the caller's workspace membership has been verified upstream.
        - ``str`` (creator user_id): restrict to edges created by that user.
          Private-context reads or admin paths pass this explicitly.

        Args:
            owner_filter: Optional creator filter — see above. The sentinel
                default distinguishes "omitted" from "explicit None".

        Returns:
            Stats dict with edge counts and weights
        """
        effective_filter: str | None = (
            self.user_id if owner_filter is self._STATS_OWNER_DEFAULT else owner_filter
        )
        edge_stats = await self.edge_repo.get_stats(
            effective_filter,
            workspace_id=self.workspace_id,
            context_id=self.context_id,
        )

        # Get unique node count (distinct src_id + dst_id)
        from sqlalchemy import and_, select

        from models.memory import NeuralMemoryEdge

        conditions: list = []
        if effective_filter is not None:
            conditions.append(NeuralMemoryEdge.user_id == effective_filter)
        if self.workspace_id:
            conditions.append(NeuralMemoryEdge.workspace_id == UUID(self.workspace_id))
        if self.context_id:
            conditions.append(NeuralMemoryEdge.context_id == UUID(self.context_id))

        src_nodes = select(NeuralMemoryEdge.src_id).where(and_(*conditions))
        dst_nodes = select(NeuralMemoryEdge.dst_id).where(and_(*conditions))
        all_nodes = src_nodes.union(dst_nodes)

        result = await self.db.execute(select(all_nodes.subquery()))
        unique_nodes = set(result.scalars().all())
        total_nodes = len(unique_nodes)

        # Calculate density
        max_possible_edges = total_nodes * (total_nodes - 1) if total_nodes > 1 else 0
        density = edge_stats["total_edges"] / max_possible_edges if max_possible_edges > 0 else 0.0

        return {
            "total_nodes": total_nodes,
            "total_edges": edge_stats["total_edges"],
            "avg_edge_weight": round(edge_stats["avg_weight"], 4),
            "max_edge_weight": round(edge_stats["max_weight"], 4),
            "min_edge_weight": round(edge_stats["min_weight"], 4),
            "density": round(density, 4),
        }

    async def clear(self) -> None:
        """Clear all edges from graph."""
        deleted_count = await self.edge_repo.delete_all_edges(self.user_id)
        logger.warning("graph_cleared", user_id=self.user_id, edges_deleted=deleted_count)

    async def get_node_metrics(self, node_id: str | UUID) -> dict[str, Any]:
        """Get Neural Memory metrics for a specific node.

        Calculates graph-based metrics to assist with memory consolidation
        and promotion decisions (Issue #44).

        Args:
            node_id: Node ID to analyze

        Returns:
            Dict with metrics:
            - centrality: Degree centrality (0.0-1.0)
            - edge_count: Number of edges connected to this node
            - avg_edge_weight: Average weight of connected edges
            - max_edge_weight: Maximum edge weight
            - is_hub_node: True if edge_count >= 5
            - is_isolated: True if edge_count == 0
        """
        node_uuid = UUID(node_id) if isinstance(node_id, str) else node_id

        # Get edges
        incoming = await self.edge_repo.get_incoming_edges(
            self.user_id, node_uuid, workspace_id=self.workspace_id, context_id=self.context_id
        )
        outgoing = await self.edge_repo.get_outgoing_edges(
            self.user_id, node_uuid, workspace_id=self.workspace_id, context_id=self.context_id
        )
        all_edges = incoming + outgoing

        edge_count = len(all_edges)

        if edge_count == 0:
            return {
                "centrality": 0.0,
                "edge_count": 0,
                "avg_edge_weight": 0.0,
                "max_edge_weight": 0.0,
                "is_hub_node": False,
                "is_isolated": True,
            }

        # Calculate edge weights
        weights = [edge.weight for edge in all_edges]
        avg_weight = sum(weights) / len(weights)
        max_weight = max(weights)

        # Calculate degree centrality (simple: degree / max_possible_degree)
        stats = await self.stats()
        max_degree = stats["total_nodes"] - 1 if stats["total_nodes"] > 1 else 1
        centrality = edge_count / max_degree if max_degree > 0 else 0.0

        return {
            "centrality": round(centrality, 4),
            "edge_count": edge_count,
            "avg_edge_weight": round(avg_weight, 4),
            "max_edge_weight": round(max_weight, 4),
            "is_hub_node": edge_count >= 5,
            "is_isolated": False,
        }

    # ========================================================================
    # Graph Synchronization (Issue #84 Phase 2B)
    # ========================================================================

    async def sync_node_from_memory(self, memory_id: str | UUID) -> bool:
        """Sync graph node attributes from memories table.

        Updates node attributes (importance, use_count, last_used_at) to match
        the current state in the database.

        Args:
            memory_id: Memory ID to sync

        Returns:
            True if synced, False if node doesn't exist

        Note:
            Issue #84 Phase 2B: In SQL backend, this is a no-op since node
            attributes are always fresh via JOINs. Kept for API compatibility.
        """
        from sqlalchemy import select

        from models.memory import Memory

        node_uuid = UUID(memory_id) if isinstance(memory_id, str) else memory_id

        if not await self.has_node(node_uuid):
            return False

        # Verify memory exists
        result = await self.db.execute(select(Memory).where(Memory.id == node_uuid))
        memory = result.scalar_one_or_none()

        if not memory or memory.user_id != self.user_id:
            logger.warning("sync_failed", node_id=str(node_uuid), reason="not_found")
            return False

        logger.debug(
            "node_synced",
            node_id=str(node_uuid),
            importance=memory.importance,
            use_count=memory.access_count,
            note="SQL_backend_always_fresh",
        )

        return True

    def __repr__(self) -> str:
        """String representation (async stats call required)."""
        return f"GraphService(user={self.user_id}, backend='SQL')"
