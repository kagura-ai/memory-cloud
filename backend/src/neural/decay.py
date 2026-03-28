"""Forgetting and decay mechanisms for neural memory.

Implements automatic weight decay and selective pruning to prevent
memory bloat and enable graceful forgetting of unused associations.

Biological inspiration: Memories that are not reinforced fade over time,
allowing new information to be learned without interference.
"""

import logging
import math
from datetime import datetime, timedelta
from typing import Any

from src.services.graph_service import GraphService as GraphMemory
from utils.datetime import utcnow

from .config import NeuralMemoryConfig

logger = logging.getLogger(__name__)


class DecayManager:
    """Manages automatic forgetting and weight decay for neural memory."""

    def __init__(
        self,
        graph: GraphMemory,
        config: NeuralMemoryConfig,
    ) -> None:
        """Initialize decay manager.

        Args:
            graph: Graph memory instance
            config: Neural memory configuration
        """
        self.graph = graph
        self.config = config
        self._last_decay_time: datetime | None = None

    async def apply_decay(self, user_id: str) -> dict[str, int | float]:
        """Apply exponential decay to all edge weights.

        Formula:
            w_ij(t+Δt) = w_ij(t) · exp(-decay_rate · Δt)

        Args:
            user_id: User ID (for filtering user-specific edges)

        Returns:
            Dict with statistics (edges_decayed, edges_pruned, delta_seconds)
        """
        if not self.config.enable_decay:
            logger.debug("Decay is disabled in configuration")
            return {"edges_decayed": 0, "edges_pruned": 0}

        current_time = utcnow()

        # Calculate time delta
        if self._last_decay_time:
            delta_seconds = (current_time - self._last_decay_time).total_seconds()
        else:
            # First run - use default interval
            delta_seconds = self.config.decay_background_interval

        if delta_seconds <= 0:
            return {"edges_decayed": 0, "edges_pruned": 0}

        # Apply decay to all edges (Issue #84: SQL backend bulk operation)
        decay_factor = math.exp(-self.config.decay_rate * delta_seconds)
        edges_decayed = await self.graph.edge_repo.bulk_decay_weights(user_id, decay_factor)

        # Prune weak edges
        edges_pruned = await self.graph.edge_repo.prune_weak_edges(
            user_id, self.config.prune_threshold
        )

        self._last_decay_time = current_time

        logger.info(
            f"Applied decay: {edges_decayed} edges decayed, "
            f"{edges_pruned} edges pruned "
            f"(Δt={delta_seconds:.0f}s)"
        )

        return {
            "edges_decayed": edges_decayed,
            "edges_pruned": edges_pruned,
            "delta_seconds": delta_seconds,
        }

    async def prune_weak_edges(self, user_id: str, threshold: float | None = None) -> int:
        """Prune edges below a weight threshold.

        Args:
            user_id: User ID
            threshold: Weight threshold (default: config.prune_threshold)

        Returns:
            Number of edges pruned
        """
        threshold = threshold if threshold is not None else self.config.prune_threshold

        # SQL backend: use bulk prune (Issue #84)
        pruned_count = await self.graph.edge_repo.prune_weak_edges(user_id, threshold)

        logger.info(f"Pruned {pruned_count} weak edges (threshold={threshold:.4f})")

        return pruned_count

    async def prune_old_nodes(
        self, user_id: str, age_days: float, importance_threshold: float = 0.3
    ) -> int:
        """Prune old, low-importance nodes.

        Args:
            user_id: User ID
            age_days: Age threshold in days
            importance_threshold: Importance threshold [0, 1]

        Returns:
            Number of nodes pruned
        """
        current_time = utcnow()
        cutoff_time = current_time - timedelta(days=age_days)

        # SQL backend: Query old nodes from memories table (Issue #84)
        from sqlalchemy import and_, select

        from models.memory import Memory

        stmt = select(Memory.id).where(
            and_(
                Memory.user_id == user_id,
                Memory.created_at < cutoff_time,
                Memory.importance < importance_threshold,
                Memory.deleted_at.is_(None),
            )
        )

        result = await self.graph.db.execute(stmt)
        old_node_ids = [row[0] for row in result.all()]

        # Remove edges for old nodes
        nodes_removed = 0
        for node_id in old_node_ids:
            deleted_count = await self.graph.edge_repo.delete_node_edges(user_id, node_id)
            if deleted_count > 0:
                nodes_removed += 1

        logger.info(
            f"Pruned {nodes_removed} old nodes "
            f"(age>{age_days}d, importance<{importance_threshold:.2f})"
        )

        return nodes_removed

    async def consolidate_to_long_term(
        self, user_id: str, nodes: list[dict[str, Any]]
    ) -> list[str]:
        """Promote qualifying nodes to long-term memory.

        Criteria (from config):
        - use_count >= consolidation_use_count_min
        - importance >= consolidation_importance_min
        - diversity >= consolidation_diversity_min (TODO: implement diversity metric)

        Args:
            user_id: User ID
            nodes: List of node data dicts

        Returns:
            List of promoted node IDs
        """
        # SQL backend: Memory consolidation handled by MemoryService (Issue #84)
        # Node metadata (scope: working→persistent) is managed in memories table
        logger.info(
            "consolidate_to_long_term() deprecated in SQL backend. "
            "Memory consolidation handled by MemoryService (scope field)."
        )
        return []

    async def get_decay_statistics(self, user_id: str) -> dict[str, Any]:
        """Get statistics about edge weights and decay status.

        Args:
            user_id: User ID

        Returns:
            Dict with statistics
        """
        # SQL backend: use edge_repo statistics (Issue #84)
        edge_stats = await self.graph.edge_repo.get_stats(user_id)

        # Count edges below threshold (requires edge query)
        from sqlalchemy import and_, func, select

        from models.memory import NeuralMemoryEdge

        below_threshold_stmt = select(func.count(NeuralMemoryEdge.id)).where(
            and_(
                NeuralMemoryEdge.user_id == user_id,
                NeuralMemoryEdge.edge_type == "neural_association",
                NeuralMemoryEdge.weight < self.config.prune_threshold,
            )
        )

        below_result = await self.graph.db.execute(below_threshold_stmt)
        below_threshold = below_result.scalar() or 0

        return {
            "total_neural_edges": edge_stats["total_edges"],
            "avg_weight": edge_stats["avg_weight"],
            "max_weight": edge_stats["max_weight"],
            "min_weight": edge_stats["min_weight"],
            "below_threshold": below_threshold,
            "last_decay_time": self._last_decay_time.isoformat() if self._last_decay_time else None,
        }
