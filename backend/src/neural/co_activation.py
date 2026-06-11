"""Co-activation tracking for Hebbian learning.

Tracks which memory nodes are retrieved/used together (co-activated),
providing the input for Hebbian weight updates. This enables automatic
discovery of associations through usage patterns.

"Cells that fire together, wire together" - the co-activation tracker
identifies which cells are firing together.
"""

import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from utils.datetime import utcnow

from .config import NeuralMemoryConfig
from .models import ActivationState, CoActivationRecord
from .utils import cosine_similarity

logger = logging.getLogger(__name__)


class CoActivationTracker:
    """Tracks co-activation patterns for Hebbian learning.

    Maintains a sliding window of recent activations and detects when
    nodes are activated together (within the same retrieval session).
    """

    def __init__(self, config: NeuralMemoryConfig) -> None:
        """Initialize co-activation tracker.

        Args:
            config: Neural memory configuration
        """
        self.config = config

        # Map: user_id -> list of (timestamp, set of node IDs)
        self._activation_history: dict[str, list[tuple[datetime, set[str]]]] = defaultdict(list)

        # Separate embedding cache: user_id -> {node_id: embedding}
        # Avoids duplicating large embedding vectors in history entries
        self._embedding_cache: dict[str, dict[str, list[float]]] = defaultdict(dict)

        # Map: user_id -> {(node_1, node_2): CoActivationRecord}
        self._co_activation_records: dict[str, dict[tuple[str, str], CoActivationRecord]] = (
            defaultdict(dict)
        )

    def record_activation(
        self,
        user_id: str,
        activations: list[ActivationState],
        session_id: str | None = None,
        embeddings: dict[str, list[float]] | None = None,
        similarity_threshold: float | None = None,
    ) -> list[CoActivationRecord]:
        """Record an activation event and detect co-activations.

        Args:
            user_id: User ID (for sharding)
            activations: List of activated nodes in this retrieval
            session_id: Optional session ID for grouping
            embeddings: Map of node_id -> embedding for semantic gating.
                When provided, only pairs with cosine similarity >= the
                effective threshold will create edges.
            similarity_threshold: Per-call semantic-gate override (#982). When
                not None, takes precedence over ``config.min_similarity_for_edge``
                — the caller resolves the calibrated edge-gate threshold (DB-
                aware) and passes it in. ``None`` falls back to the config value.

        Returns:
            List of co-activation records updated/created in this event
        """
        if not self.config.track_co_activation:
            return []

        if not activations:
            return []

        timestamp = utcnow()

        activated_ids = {act.node_id for act in activations}

        # Update embedding cache (separate from history to avoid memory bloat)
        if embeddings:
            for node_id, emb in embeddings.items():
                if emb:
                    self._embedding_cache[user_id][node_id] = emb

        # Clean old history (outside time window)
        self._clean_old_history(user_id, timestamp)

        # Add to history
        self._activation_history[user_id].append((timestamp, activated_ids))

        # Detect co-activations within the time window
        co_activated_pairs = self._find_co_activations_in_window(
            user_id, timestamp, similarity_threshold
        )

        # Update co-activation records
        updated_records = []
        for node_1, node_2, act_1, act_2 in co_activated_pairs:
            record = self._update_co_activation_record(
                user_id=user_id,
                node_1=node_1,
                node_2=node_2,
                activation_1=act_1,
                activation_2=act_2,
            )
            updated_records.append(record)

        logger.debug(
            f"Recorded {len(activations)} activations for user {user_id}, "
            f"detected {len(updated_records)} co-activations"
        )

        return updated_records

    def get_co_activation_record(
        self, user_id: str, node_1: str, node_2: str
    ) -> CoActivationRecord | None:
        """Get co-activation record for a node pair.

        Args:
            user_id: User ID
            node_1: First node ID
            node_2: Second node ID

        Returns:
            CoActivationRecord if exists, None otherwise
        """
        # Ensure ordering
        if node_1 > node_2:
            node_1, node_2 = node_2, node_1

        return self._co_activation_records[user_id].get((node_1, node_2))

    def get_all_co_activations(
        self, user_id: str, min_count: int | None = None
    ) -> list[CoActivationRecord]:
        """Get all co-activation records for a user.

        Args:
            user_id: User ID
            min_count: Minimum co-activation count to include

        Returns:
            List of co-activation records
        """
        min_count = min_count if min_count is not None else self.config.min_co_activation_count

        records = list(self._co_activation_records[user_id].values())

        if min_count > 0:
            records = [r for r in records if r.count >= min_count]

        # Sort by count (descending)
        records.sort(key=lambda r: r.count, reverse=True)

        return records

    def get_frequently_co_activated_with(
        self, user_id: str, node_id: str, top_k: int = 10
    ) -> list[tuple[str, CoActivationRecord]]:
        """Get nodes that are frequently co-activated with a given node.

        Args:
            user_id: User ID
            node_id: Target node ID
            top_k: Number of top co-activated nodes to return

        Returns:
            List of (other_node_id, CoActivationRecord) tuples
        """
        related = []

        for (n1, n2), record in self._co_activation_records[user_id].items():
            if n1 == node_id:
                related.append((n2, record))
            elif n2 == node_id:
                related.append((n1, record))

        # Sort by co-activation count
        related.sort(key=lambda x: x[1].count, reverse=True)

        return related[:top_k]

    def _clean_old_history(self, user_id: str, current_time: datetime) -> None:
        """Remove activation events outside the time window.

        Args:
            user_id: User ID
            current_time: Current timestamp
        """
        window_seconds = self.config.co_activation_window
        cutoff_time = current_time - timedelta(seconds=window_seconds)

        # Filter history
        self._activation_history[user_id] = [
            (ts, ids) for ts, ids in self._activation_history[user_id] if ts >= cutoff_time
        ]

    def _find_co_activations_in_window(
        self,
        user_id: str,
        current_time: datetime,
        similarity_threshold: float | None = None,
    ) -> list[tuple[str, str, float, float]]:
        """Find all co-activated pairs within the time window.

        Applies semantic gating: pairs with cosine similarity below the
        effective threshold are skipped to prevent noise edges. The effective
        threshold is ``similarity_threshold`` when provided (#982 calibrated
        value), else ``config.min_similarity_for_edge``.

        Args:
            user_id: User ID
            current_time: Current timestamp
            similarity_threshold: Per-call gate override; ``None`` → config.

        Returns:
            List of (node_1, node_2, activation_1, activation_2) tuples
        """
        # Get all activated nodes in the window
        window_seconds = self.config.co_activation_window
        cutoff_time = current_time - timedelta(seconds=window_seconds)

        # Collect all node IDs from the window
        all_activated: dict[str, list[float]] = defaultdict(list)

        for ts, node_ids in self._activation_history[user_id]:
            if ts >= cutoff_time:
                for node_id in node_ids:
                    all_activated[node_id].append(1.0)

        # Get embeddings from cache (separate from history)
        all_embeddings = self._embedding_cache.get(user_id, {})

        # Find pairs of nodes that were both activated
        co_activated_pairs = []
        node_ids = list(all_activated.keys())
        threshold = (
            similarity_threshold
            if similarity_threshold is not None
            else self.config.min_similarity_for_edge
        )
        skipped = 0

        for i, node_1 in enumerate(node_ids):
            for node_2 in node_ids[i + 1 :]:  # Avoid duplicates
                # Semantic gating: skip pairs below similarity threshold
                emb_1 = all_embeddings.get(node_1)
                emb_2 = all_embeddings.get(node_2)

                if emb_1 and emb_2:
                    sim = cosine_similarity(emb_1, emb_2)
                    if sim < threshold:
                        skipped += 1
                        continue

                # Calculate average activation (for Hebbian update)
                avg_act_1 = sum(all_activated[node_1]) / len(all_activated[node_1])
                avg_act_2 = sum(all_activated[node_2]) / len(all_activated[node_2])

                co_activated_pairs.append((node_1, node_2, avg_act_1, avg_act_2))

        if skipped > 0:
            logger.debug(
                "semantic_gating_skipped", extra={"skipped": skipped, "threshold": threshold}
            )

        return co_activated_pairs

    def _update_co_activation_record(
        self,
        user_id: str,
        node_1: str,
        node_2: str,
        activation_1: float,
        activation_2: float,
    ) -> CoActivationRecord:
        """Update or create a co-activation record.

        Args:
            user_id: User ID
            node_1: First node ID
            node_2: Second node ID
            activation_1: Activation strength of node 1
            activation_2: Activation strength of node 2

        Returns:
            Updated/created CoActivationRecord
        """
        # Ensure ordering
        if node_1 > node_2:
            node_1, node_2 = node_2, node_1
            activation_1, activation_2 = activation_2, activation_1

        key = (node_1, node_2)

        if key in self._co_activation_records[user_id]:
            # Update existing record
            record = self._co_activation_records[user_id][key]
            record.update(activation_1, activation_2)
        else:
            # Create new record
            record = CoActivationRecord(
                node_id_1=node_1,
                node_id_2=node_2,
                count=1,
                total_activation_product=activation_1 * activation_2,
                user_id=user_id,
            )
            self._co_activation_records[user_id][key] = record

        logger.debug(
            f"Co-activation record updated: ({node_1}, {node_2}) "
            f"count={record.count}, avg_product={record.average_activation_product:.4f}"
        )

        return record

    def clear_user_data(self, user_id: str) -> None:
        """Clear all co-activation data for a user (GDPR compliance).

        Args:
            user_id: User ID to clear
        """
        if user_id in self._activation_history:
            del self._activation_history[user_id]

        if user_id in self._embedding_cache:
            del self._embedding_cache[user_id]

        if user_id in self._co_activation_records:
            del self._co_activation_records[user_id]

        logger.info(f"Cleared co-activation data for user {user_id}")

    def get_statistics(self, user_id: str) -> dict[str, Any]:
        """Get statistics about co-activation tracking.

        Args:
            user_id: User ID

        Returns:
            Dict with statistics
        """
        records = self._co_activation_records[user_id].values()

        if not records:
            return {
                "total_pairs": 0,
                "avg_count": 0.0,
                "max_count": 0,
                "min_count": 0,
            }

        counts = [r.count for r in records]

        return {
            "total_pairs": len(records),
            "avg_count": sum(counts) / len(counts),
            "max_count": max(counts),
            "min_count": min(counts),
            "history_size": len(self._activation_history[user_id]),
        }

    # ========================================================================
    # Redis Persistence (Issue #84 Phase 2C)
    # ========================================================================

    async def save_to_redis(self, user_id: str) -> int:
        """Persist co-activation records to Redis with 7-day TTL.

        Args:
            user_id: User ID

        Returns:
            Number of records saved
        """
        from db.redis import set_co_activation

        if user_id not in self._co_activation_records:
            return 0

        saved = 0
        for (node_1, node_2), record in self._co_activation_records[user_id].items():
            record_dict = {
                "count": record.count,
                "total_activation_product": record.total_activation_product,
                "first_seen": record.first_seen.isoformat(),
                "last_seen": record.last_co_activation.isoformat(),
            }

            await set_co_activation(user_id, node_1, node_2, record_dict)
            saved += 1

        logger.info(f"Persisted {saved} co-activation records to Redis for user {user_id}")
        return saved

    async def load_from_redis(self, user_id: str) -> int:
        """Load co-activation records from Redis (warm start on server restart).

        Args:
            user_id: User ID

        Returns:
            Number of records loaded
        """
        from datetime import datetime

        from db.redis import get_all_co_activations

        records_dict = await get_all_co_activations(user_id)

        if not records_dict:
            logger.debug(f"No co-activation records found in Redis for user {user_id}")
            return 0

        # Initialize user's record dict if needed
        if user_id not in self._co_activation_records:
            self._co_activation_records[user_id] = {}

        loaded = 0
        for (node_1, node_2), record_dict in records_dict.items():
            record = CoActivationRecord(
                node_id_1=node_1,
                node_id_2=node_2,
                count=record_dict["count"],
                total_activation_product=record_dict["total_activation_product"],
                user_id=user_id,
            )

            # Restore timestamps
            record.first_seen = datetime.fromisoformat(record_dict["first_seen"])
            record.last_co_activation = datetime.fromisoformat(record_dict["last_seen"])

            self._co_activation_records[user_id][(node_1, node_2)] = record
            loaded += 1

        logger.info(f"Loaded {loaded} co-activation records from Redis for user {user_id}")
        return loaded
