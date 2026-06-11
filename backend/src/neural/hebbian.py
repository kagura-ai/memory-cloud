"""Hebbian learning update mechanism.

Implements the "cells that fire together, wire together" principle for
dynamically strengthening associations between co-activated memory nodes.

Formula:
    Δw_ij ← η · (a_i · C_i) · (a_j · C_j) - λ · w_ij

Where:
    - η: learning rate
    - a_i, a_j: activation strengths of nodes i and j
    - C_i, C_j: confidence/trust scores (poisoning defense)
    - λ: L2 decay coefficient (prevents weight explosion)
    - w_ij: current edge weight

References:
    - Hopfield Networks is All You Need (arXiv:2008.02217)
    - Hebbian Learning principles
"""

import logging
from collections import defaultdict

from models.memory import EDGE_ORIGIN_HEBBIAN
from src.services.graph_service import GraphService as GraphMemory

from .config import NeuralMemoryConfig
from .models import ActivationState, HebbianUpdate, NeuralMemoryNode
from .utils import cosine_similarity

logger = logging.getLogger(__name__)


class HebbianLearner:
    """Hebbian learning update manager.

    Handles batch updates of edge weights based on co-activation patterns,
    with trust-based modulation and gradient clipping for security.
    """

    def __init__(
        self,
        graph: GraphMemory,
        config: NeuralMemoryConfig,
    ) -> None:
        """Initialize Hebbian learner.

        Args:
            graph: Graph memory instance to update
            config: Neural memory configuration
        """
        self.graph = graph
        self.config = config
        self._update_queue: dict[str, list[HebbianUpdate]] = defaultdict(list)
        # #983 prune-cliff fix: sub-threshold weight accumulated for edges
        # that do not exist yet, keyed (src_id, dst_id) per user. In-process
        # only (same volatility contract as CoActivationTracker records);
        # entries are popped when the edge materializes.
        self._pending_weights: dict[str, dict[tuple[str, str], float]] = defaultdict(dict)

    async def queue_update(
        self,
        user_id: str,
        activations: list[ActivationState],
        nodes: dict[str, NeuralMemoryNode],
        similarity_threshold: float | None = None,
        floor_threshold: float | None = None,
        co_activation_counts: dict[tuple[str, str], int] | None = None,
    ) -> None:
        """Queue Hebbian updates for co-activated nodes.

        Applies semantic gating (Issue #118): pairs with cosine similarity
        below the effective threshold are skipped to prevent noise edges from
        accumulating. The effective threshold is ``similarity_threshold`` when
        provided (#982 calibrated edge-gate value), else
        ``config.min_similarity_for_edge``.

        2D edge gate (#983): when ``floor_threshold`` and
        ``co_activation_counts`` are provided (and
        ``config.edge_gate_repetition_enabled``), pairs in the
        [floor, threshold) cosine band are admitted once their repetition
        evidence reaches ``config.edge_gate_min_evidence`` — genuine
        cross-topic associations are co-recalled repeatedly across distinct
        queries, noise pairs co-occur in few. Below the floor stays a hard
        reject.

        Args:
            user_id: User ID (for sharding)
            activations: List of activated nodes in this retrieval
            nodes: Map of node_id -> NeuralMemoryNode (for confidence scores)
            similarity_threshold: Per-call semantic-gate override (#982);
                ``None`` falls back to ``config.min_similarity_for_edge``.
            floor_threshold: Lower bound of the repetition band (#983);
                ``None`` disables the repetition axis for this call.
            co_activation_counts: Map of ``(node_id_1, node_id_2)`` — ordered
                ``min, max`` — to same-event co-recall counts from the
                ``CoActivationTracker`` (#983).
        """
        threshold = (
            similarity_threshold
            if similarity_threshold is not None
            else self.config.min_similarity_for_edge
        )
        repetition_active = (
            self.config.edge_gate_repetition_enabled
            and floor_threshold is not None
            and co_activation_counts is not None
        )
        skipped = 0
        admitted_by_repetition = 0

        for i, act_i in enumerate(activations):
            for act_j in activations[i + 1 :]:  # Avoid duplicates
                node_i = nodes.get(act_i.node_id)
                node_j = nodes.get(act_j.node_id)

                if not node_i or not node_j:
                    continue

                # Semantic gating: skip pairs below similarity threshold,
                # unless the repetition axis (#983) admits a band pair.
                if node_i.embedding and node_j.embedding:
                    sim = cosine_similarity(node_i.embedding, node_j.embedding)
                    if sim < threshold:
                        pair_key = (
                            (act_i.node_id, act_j.node_id)
                            if act_i.node_id < act_j.node_id
                            else (act_j.node_id, act_i.node_id)
                        )
                        in_band = repetition_active and sim >= floor_threshold
                        evidence = co_activation_counts.get(pair_key, 0) if repetition_active else 0
                        if not (in_band and evidence >= self.config.edge_gate_min_evidence):
                            skipped += 1
                            continue
                        admitted_by_repetition += 1

                # Get current weight (Issue #84: async)
                current_weight = await self._get_current_weight(
                    user_id, act_i.node_id, act_j.node_id
                )

                # Calculate Δw with trust modulation
                delta_w = self._calculate_delta_weight(
                    activation_i=act_i.activation,
                    activation_j=act_j.activation,
                    confidence_i=node_i.confidence if self.config.enable_trust_modulation else 1.0,
                    confidence_j=node_j.confidence if self.config.enable_trust_modulation else 1.0,
                    current_weight=current_weight,
                )

                # Queue bidirectional updates (undirected graph)
                self._update_queue[user_id].append(
                    HebbianUpdate(
                        user_id=user_id,
                        src_id=act_i.node_id,
                        dst_id=act_j.node_id,
                        delta_weight=delta_w,
                    )
                )
                self._update_queue[user_id].append(
                    HebbianUpdate(
                        user_id=user_id,
                        src_id=act_j.node_id,
                        dst_id=act_i.node_id,
                        delta_weight=delta_w,
                    )
                )

        if skipped > 0:
            logger.debug(
                "hebbian_semantic_gating", extra={"skipped": skipped, "threshold": threshold}
            )

        if admitted_by_repetition > 0:
            logger.debug(
                "hebbian_repetition_admitted",
                extra={
                    "admitted": admitted_by_repetition,
                    "floor": floor_threshold,
                    "min_evidence": self.config.edge_gate_min_evidence,
                },
            )

        logger.debug(
            f"Queued {len(self._update_queue[user_id])} Hebbian updates for user {user_id}"
        )

    async def apply_updates(self, user_id: str) -> int:
        """Apply all queued updates for a user (with gradient clipping).

        Args:
            user_id: User ID

        Returns:
            Number of edges updated
        """
        if user_id not in self._update_queue:
            return 0

        updates = self._update_queue[user_id]
        if not updates:
            return 0

        # Group updates by edge (src, dst) and sum deltas
        edge_deltas: dict[tuple[str, str], float] = defaultdict(float)
        for update in updates:
            edge_deltas[(update.src_id, update.dst_id)] += update.delta_weight

        # Apply gradient clipping (DP-SGD style)
        if self.config.gradient_clipping > 0:
            edge_deltas = self._clip_gradients(edge_deltas)

        # Apply updates to graph (Issue #84: async SQL operations)
        edges_updated = 0
        for (src_id, dst_id), delta_w in edge_deltas.items():
            new_weight = await self._apply_update_to_edge(user_id, src_id, dst_id, delta_w)
            if new_weight is not None:
                edges_updated += 1

        # Note: Transaction committed by MemoryService for atomic operations
        # (edges + access_stats + promotions in single transaction)

        # Issue #150: Prune weak edges to maintain top_m_edges limit
        # Collect unique source nodes that were updated
        updated_nodes = {src_id for src_id, _ in edge_deltas.keys()}

        logger.debug(
            f"Pruning check: {len(updated_nodes)} nodes updated, top_m_edges={self.config.top_m_edges}"
        )

        # Prune weak edges from each updated node
        total_pruned = 0
        for node_id in updated_nodes:
            try:
                pruned_count = await self.prune_weak_edges(user_id, node_id)
                if pruned_count > 0:
                    logger.debug(f"Pruned {pruned_count} edges from node {node_id}")
                total_pruned += pruned_count
            except Exception as e:
                logger.error(f"Failed to prune edges for node {node_id}: {e}")

        if total_pruned > 0:
            logger.info(
                f"Pruned {total_pruned} weak edges from {len(updated_nodes)} nodes "
                f"(top_m_edges={self.config.top_m_edges})"
            )

        # Clear queue
        self._update_queue[user_id].clear()

        logger.info(f"Applied {edges_updated} Hebbian updates for user {user_id}")
        return edges_updated

    def _calculate_delta_weight(
        self,
        activation_i: float,
        activation_j: float,
        confidence_i: float,
        confidence_j: float,
        current_weight: float,
    ) -> float:
        """Calculate Hebbian weight update.

        Formula:
            Δw = η · (a_i · C_i) · (a_j · C_j) - λ · w

        Args:
            activation_i: Activation of node i [0, 1]
            activation_j: Activation of node j [0, 1]
            confidence_i: Confidence of node i [0, 1]
            confidence_j: Confidence of node j [0, 1]
            current_weight: Current edge weight

        Returns:
            Weight delta (can be negative if decay dominates)
        """
        # Hebbian term: strengthening based on co-activation
        hebbian_term = (
            self.config.learning_rate
            * (activation_i * confidence_i)
            * (activation_j * confidence_j)
        )

        # Decay term: regularization to prevent weight explosion
        decay_term = self.config.decay_lambda * current_weight

        delta_w = hebbian_term - decay_term

        return delta_w

    async def _get_current_weight(self, user_id: str, src_id: str, dst_id: str) -> float:
        """Get current edge weight from graph.

        Args:
            user_id: User ID
            src_id: Source node ID
            dst_id: Destination node ID

        Returns:
            Current weight (0.0 if edge doesn't exist)
        """
        # Query graph for edge data (Issue #84: SQL backend)
        try:
            edge_data = await self.graph.get_edge(src_id, dst_id)
            if edge_data:
                return edge_data.get("weight", 0.0)
        except Exception as e:
            logger.debug(f"Edge ({src_id}, {dst_id}) not found: {e}")

        return 0.0

    async def _apply_update_to_edge(
        self, user_id: str, src_id: str, dst_id: str, delta_w: float
    ) -> float | None:
        """Apply weight update to graph edge.

        Args:
            user_id: User ID
            src_id: Source node ID
            dst_id: Destination node ID
            delta_w: Weight delta to apply

        Returns:
            New weight value, or None if update failed
        """
        pair_key = (src_id, dst_id)
        # #983: fold in any pending sub-threshold weight from earlier
        # co-recalls of this not-yet-materialized edge.
        pending = self._pending_weights[user_id].pop(pair_key, 0.0)
        current_weight = await self._get_current_weight(user_id, src_id, dst_id)
        new_weight = current_weight + pending + delta_w

        # Clip to [0, weight_max]
        new_weight = max(0.0, min(new_weight, self.config.weight_max))

        # Prune if below threshold
        if new_weight < self.config.prune_threshold:
            try:
                if await self.graph.has_edge(src_id, dst_id):
                    # Existing edge decayed below threshold — prune. The #983
                    # cliff fix applies only to not-yet-materialized edges;
                    # decay-side pruning is unchanged.
                    await self.graph.remove_edge(src_id, dst_id)
                    logger.debug(f"Pruned edge ({src_id}, {dst_id}) (weight={new_weight:.4f})")
                elif new_weight > 0.0:
                    # #983 prune-cliff fix: the edge does not exist yet, so a
                    # sub-threshold first update used to be dropped — making
                    # accumulation across repeated co-recalls impossible
                    # (~12% of observed pairs in the #969 audit). Stash it.
                    self._pending_weights[user_id][pair_key] = new_weight
                return 0.0
            except Exception as e:
                logger.error(f"Failed to remove edge ({src_id}, {dst_id}): {e}")
                return None

        # Update or create edge
        try:
            edge_exists = await self.graph.has_edge(src_id, dst_id)
            if edge_exists:
                # Update existing edge (Issue #84: SQL backend)
                await self.graph.add_edge(
                    src_id=src_id,
                    dst_id=dst_id,
                    rel_type="neural_association",
                    weight=new_weight,
                    confidence=1.0,
                )
            else:
                # Hebbian-created edges use rel_type="neural_association" so that
                # any edge-stats query filtering by that type counts them. Prior
                # mismatches against "learned_from" caused silently-zero stats.
                await self.graph.add_edge(
                    src_id=src_id,
                    dst_id=dst_id,
                    rel_type="neural_association",
                    weight=new_weight,
                    edge_metadata={
                        "created_by": "hebbian_learning",
                    },
                    confidence=1.0,
                )

            logger.debug(
                f"Updated edge ({src_id}, {dst_id}): "
                f"{current_weight:.4f} -> {new_weight:.4f} (Δ={delta_w:.4f})"
            )
            return new_weight

        except Exception as e:
            logger.error(f"Failed to update edge ({src_id}, {dst_id}): {e}")
            return None

    def _clip_gradients(
        self, edge_deltas: dict[tuple[str, str], float]
    ) -> dict[tuple[str, str], float]:
        """Clip gradients to prevent poisoning attacks.

        Limits the total weight change per node to prevent a single
        malicious interaction from dominating the graph.

        Args:
            edge_deltas: Map of (src, dst) -> delta_w

        Returns:
            Clipped edge deltas
        """
        # Calculate total delta per node (outgoing edges)
        node_total_deltas: dict[str, float] = defaultdict(float)
        for (src_id, _), delta_w in edge_deltas.items():
            node_total_deltas[src_id] += abs(delta_w)

        # Clip if any node exceeds threshold
        clipped_deltas = {}
        for (src_id, dst_id), delta_w in edge_deltas.items():
            total_delta = node_total_deltas[src_id]
            if total_delta > self.config.gradient_clipping:
                # Scale down proportionally
                scale = self.config.gradient_clipping / total_delta
                clipped_deltas[(src_id, dst_id)] = delta_w * scale
                logger.debug(
                    f"Clipped gradient for node {src_id}: {delta_w:.4f} -> {delta_w * scale:.4f}"
                )
            else:
                clipped_deltas[(src_id, dst_id)] = delta_w

        return clipped_deltas

    async def prune_weak_edges(self, user_id: str, node_id: str) -> int:
        """Prune weak outgoing edges from a node (keep top-M).

        Args:
            user_id: User ID
            node_id: Node to prune

        Returns:
            Number of edges removed
        """
        # Get all outgoing edges (Issue #84: SQL backend)
        try:
            from uuid import UUID

            node_uuid = UUID(node_id) if isinstance(node_id, str) else node_id
            # Issue #724: only Hebbian-origin edges participate in the per-node
            # top-M cap. Semantic/declared edges (origin != 'hebbian') are exempt
            # from this pruner — they are managed by sleep edge_discovery and the
            # decay carve-out (#722) — so they can never be evicted here, nor do
            # they consume the top_m_edges budget. The filter is pushed into SQL
            # via get_outgoing_edges(origin=...) (#741).
            outgoing_edges = await self.graph.edge_repo.get_outgoing_edges(
                user_id,
                node_uuid,
                workspace_id=self.graph.workspace_id,
                context_id=self.graph.context_id,
                origin=EDGE_ORIGIN_HEBBIAN,
            )
        except Exception as e:
            logger.error(f"Failed to get outgoing edges for node {node_id}: {e}")
            return 0

        if len(outgoing_edges) <= self.config.top_m_edges:
            logger.debug(
                f"Node {node_id}: {len(outgoing_edges)} edges <= {self.config.top_m_edges}, no pruning needed"
            )
            return 0  # No pruning needed

        logger.debug(
            f"Node {node_id}: {len(outgoing_edges)} edges > {self.config.top_m_edges}, pruning weakest"
        )

        # Sort by weight (descending)
        edge_weights = [(str(edge.dst_id), edge.weight) for edge in outgoing_edges]
        edge_weights.sort(key=lambda x: x[1], reverse=True)

        # Remove weakest edges
        to_remove = edge_weights[self.config.top_m_edges :]
        removed_count = 0
        for dst_id, weight in to_remove:
            try:
                await self.graph.remove_edge(node_id, dst_id)
                removed_count += 1
                logger.debug(f"Pruned weak edge ({node_id}, {dst_id}) (weight={weight:.4f})")
            except Exception as e:
                logger.error(f"Failed to prune edge ({node_id}, {dst_id}): {e}")

        return removed_count
