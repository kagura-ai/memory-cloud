"""Unified scoring system for neural memory retrieval.

Combines multiple signals into a composite score:
- Semantic similarity (embedding cosine)
- Graph association (activation spreading)
- Temporal recency (exponential decay)
- Importance (use count + LLM rating)
- Trust/confidence (source verification)
- Redundancy penalty (MMR-style diversity)

Formula:
    score(i|q) = α·sim(q,i) + β·assoc(q→i) + γ·recency(i)
                 + δ·importance(i) + ε·trust(i) - ζ·redundancy(i)
"""

import logging
import math
from datetime import datetime

from utils.datetime import utcnow

from .activation import ActivationSpreader
from .config import NeuralMemoryConfig
from .models import NeuralMemoryNode, RecallResult
from .utils import (
    IMPORTANCE_FREQUENCY_WEIGHT,
    IMPORTANCE_STORED_WEIGHT,
    LOG_FREQUENCY_REFERENCE_COUNT,
    SECONDS_PER_DAY,
)

logger = logging.getLogger(__name__)


class UnifiedScorer:
    """Unified scoring for neural memory retrieval.

    Combines semantic, graph, temporal, and trust signals into a
    single relevance score for ranking retrieved memories.
    """

    def __init__(
        self,
        config: NeuralMemoryConfig,
        activation_spreader: ActivationSpreader,
    ) -> None:
        """Initialize unified scorer.

        Args:
            config: Neural memory configuration
            activation_spreader: Activation spreader for graph association scores
        """
        self.config = config
        self.activation_spreader = activation_spreader

    async def score_candidates(
        self,
        query_embedding: list[float],
        candidates: list[tuple[NeuralMemoryNode, float]],
        seed_nodes: list[str] | None = None,
        selected_nodes: list[NeuralMemoryNode] | None = None,
    ) -> list[RecallResult]:
        """Score candidate nodes using unified scoring.

        Args:
            query_embedding: Query vector (for semantic similarity)
            candidates: List of (node, similarity_score) tuples from primary retrieval
            seed_nodes: Optional seed nodes for association scoring
            selected_nodes: Already selected nodes (for redundancy penalty)

        Returns:
            List of RecallResult objects with scores and breakdowns
        """
        if not candidates:
            return []

        results = []
        current_time = utcnow()
        selected_embeddings = [node.embedding for node in selected_nodes] if selected_nodes else []

        # Get normalized weights
        weights = self.config.scoring_weights_normalized

        # ============================================================================
        # BUG FIX #83-7: Optimize activation spreading (O(k²) → O(k))
        # ============================================================================
        # Problem: get_association_score() was called for EVERY candidate in the loop,
        #          and each call internally runs spread() on the entire graph.
        #          For 100 candidates, this meant 100 graph traversals!
        #
        # Solution: Run spread() ONCE before the loop, build a lookup map.
        #
        # Performance Impact:
        #          Before: O(N_candidates × N_graph_nodes)
        #          After:  O(N_graph_nodes) + O(N_candidates) = O(N_graph_nodes)
        #
        # Example: 100 candidates, 1000 node graph, 2 hops
        #          Before: 100 × 1000 = 100,000 node visits
        #          After:  1 × 1000 = 1,000 node visits (100x speedup!)
        # ============================================================================

        # Pre-compute activation spreading (once for all candidates) - Issue #84: async
        if seed_nodes and self.config.beta > 0:
            spread_results = await self.activation_spreader.spread(
                seed_activations=dict.fromkeys(seed_nodes, 1.0),
                max_hops=None,  # Use config default
            )
            # Build lookup map: node_id → activation score
            assoc_map = {s.node_id: s.activation for s in spread_results}
        else:
            assoc_map = {}

        for node, sim_score in candidates:
            # 1. Semantic similarity (already computed)
            semantic_score = sim_score

            # 2. Graph association (lookup from pre-computed map)
            assoc_score = assoc_map.get(node.id, 0.0)

            # 3. Recency (temporal decay)
            recency_score = self._calculate_recency_score(node, current_time)

            # 4. Importance (use count + stored importance)
            importance_score = self._calculate_importance_score(node)

            # 5. Trust/confidence
            trust_score = node.confidence

            # 6. Redundancy penalty (MMR-style)
            redundancy_penalty = self._calculate_redundancy_penalty(node, selected_embeddings)

            # Composite score
            composite_score = (
                weights["alpha"] * semantic_score
                + weights["beta"] * assoc_score
                + weights["gamma"] * recency_score
                + weights["delta"] * importance_score
                + weights["epsilon"] * trust_score
                - weights["zeta"] * redundancy_penalty
            )

            # Clamp to [0, 1]
            composite_score = max(0.0, min(1.0, composite_score))

            results.append(
                RecallResult(
                    node=node,
                    score=composite_score,
                    components={
                        "semantic": semantic_score,
                        "association": assoc_score,
                        "recency": recency_score,
                        "importance": importance_score,
                        "trust": trust_score,
                        "redundancy_penalty": redundancy_penalty,
                    },
                )
            )

        # Sort by score (descending)
        results.sort(key=lambda x: x.score, reverse=True)

        logger.debug(
            f"Scored {len(results)} candidates, "
            f"top score: {results[0].score:.4f}, "
            f"bottom score: {results[-1].score:.4f}"
        )

        return results

    def _calculate_recency_score(self, node: NeuralMemoryNode, current_time: datetime) -> float:
        """Calculate recency score with exponential decay.

        Formula:
            recency = exp(-Δt / τ)

        Where:
            Δt = time since last use (in days)
            τ = time constant (config.recency_tau_days)

        Args:
            node: Memory node
            current_time: Current timestamp

        Returns:
            Recency score [0, 1]
        """
        if not node.last_used_at:
            # Never used - use creation time
            reference_time = node.created_at
        else:
            reference_time = node.last_used_at

        # Calculate age in days
        # Handle timezone-aware and naive datetimes
        try:
            age_delta = current_time - reference_time
        except TypeError:
            # Mixed timezone-aware and naive - make both naive for comparison
            if current_time.tzinfo is not None:
                current_time_naive = current_time.replace(tzinfo=None)
            else:
                current_time_naive = current_time

            if reference_time.tzinfo is not None:
                reference_time_naive = reference_time.replace(tzinfo=None)
            else:
                reference_time_naive = reference_time

            age_delta = current_time_naive - reference_time_naive

        age_days = age_delta.total_seconds() / SECONDS_PER_DAY

        # Exponential decay
        tau_days = self.config.recency_tau_days
        recency_score = math.exp(-age_days / tau_days)

        return recency_score

    def _calculate_importance_score(self, node: NeuralMemoryNode) -> float:
        """Calculate importance score.

        Combines:
        - Stored importance (LLM self-rating, updated via EMA)
        - Use frequency (log-scaled)

        Formula:
            importance = 0.7 · stored_importance + 0.3 · log_frequency

        Args:
            node: Memory node

        Returns:
            Importance score [0, 1]
        """
        # Stored importance (already in [0, 1])
        stored_importance = node.importance

        # Use frequency (log-scaled to [0, 1])
        if node.use_count > 0:
            # Log scale: log(1+count) / log(1+ref) normalizes based on reference count
            log_frequency = math.log(1 + node.use_count) / math.log(
                1 + LOG_FREQUENCY_REFERENCE_COUNT
            )
            log_frequency = min(1.0, log_frequency)  # Clamp to [0, 1]
        else:
            log_frequency = 0.0

        # Weighted combination
        importance_score = (
            IMPORTANCE_STORED_WEIGHT * stored_importance
            + IMPORTANCE_FREQUENCY_WEIGHT * log_frequency
        )

        return importance_score

    def _calculate_redundancy_penalty(
        self, node: NeuralMemoryNode, selected_embeddings: list[list[float]]
    ) -> float:
        """Calculate redundancy penalty (MMR-style diversity).

        Penalizes nodes that are too similar to already selected nodes,
        promoting diversity in results.

        Formula:
            redundancy = max_j sim(emb_i, emb_j) for j in selected

        Args:
            node: Candidate node
            selected_embeddings: Embeddings of already selected nodes

        Returns:
            Redundancy penalty [0, 1] (0 = unique, 1 = duplicate)
        """
        if not selected_embeddings:
            return 0.0  # No penalty if nothing selected yet

        # Calculate cosine similarity with all selected nodes
        similarities = [
            self._cosine_similarity(node.embedding, selected_emb)
            for selected_emb in selected_embeddings
        ]

        # Max similarity = redundancy
        max_similarity = max(similarities)

        return max_similarity

    def _cosine_similarity(self, emb1: list[float], emb2: list[float]) -> float:
        """Calculate cosine similarity between two embeddings.

        Args:
            emb1: First embedding
            emb2: Second embedding

        Returns:
            Cosine similarity clamped to [0, 1]
        """
        from .utils import cosine_similarity

        return cosine_similarity(emb1, emb2)

    def mmr_rerank(
        self,
        query_embedding: list[float],
        results: list[RecallResult],
        lambda_param: float = 0.5,
        top_k: int = 10,
    ) -> list[RecallResult]:
        """Re-rank results using Maximal Marginal Relevance (MMR).

        MMR balances relevance and diversity by iteratively selecting
        the next result that maximizes:
            MMR = λ · relevance - (1-λ) · max_similarity_to_selected

        Args:
            query_embedding: Query vector
            results: Initial ranked results
            lambda_param: Trade-off parameter [0, 1]
                          (1 = pure relevance, 0 = pure diversity)
            top_k: Number of results to return

        Returns:
            Re-ranked results (top-k)
        """
        if not results or len(results) <= 1:
            return results[:top_k]

        selected = []
        remaining = results.copy()

        for _ in range(min(top_k, len(results))):
            if not remaining:
                break

            # Calculate MMR scores for all remaining candidates
            mmr_scores = []
            for result in remaining:
                # Relevance (use composite score)
                relevance = result.score

                # Diversity (max similarity to selected)
                if selected:
                    max_sim = max(
                        self._cosine_similarity(result.node.embedding, sel.node.embedding)
                        for sel in selected
                    )
                else:
                    max_sim = 0.0

                # MMR formula
                mmr_score = lambda_param * relevance - (1 - lambda_param) * max_sim

                mmr_scores.append((result, mmr_score))

            # Select best MMR score
            mmr_scores.sort(key=lambda x: x[1], reverse=True)
            best_result, best_mmr = mmr_scores[0]

            selected.append(best_result)
            remaining.remove(best_result)

            logger.debug(
                f"MMR selected: {best_result.node.id} "
                f"(relevance={best_result.score:.4f}, MMR={best_mmr:.4f})"
            )

        return selected
