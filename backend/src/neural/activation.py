"""Activation spreading algorithm.

Implements graph-based activation propagation for associative retrieval.
Starting from seed nodes (primary retrieval results), activation spreads
through the graph with decay, enabling discovery of related memories.

Formula (for 1-hop spread):
    activation(j) = Σ_i activation(i) · decay · weight(i→j)

References:
    - Spreading activation in semantic networks
    - Hopfield Networks is All You Need (arXiv:2008.02217)
"""

import logging
from collections import defaultdict
from typing import Any

from src.services.graph_service import GraphService as GraphMemory

from .config import NeuralMemoryConfig
from .models import ActivationState

logger = logging.getLogger(__name__)


class ActivationSpreader:
    """Activation spreading manager for graph-based associative retrieval."""

    def __init__(
        self,
        graph: GraphMemory,
        config: NeuralMemoryConfig,
    ) -> None:
        """Initialize activation spreader.

        Args:
            graph: Graph memory instance
            config: Neural memory configuration
        """
        self.graph = graph
        self.config = config

    async def spread(
        self,
        seed_activations: dict[str, float],
        max_hops: int | None = None,
        user_id: str | None = None,
    ) -> list[ActivationState]:
        """Spread activation from seed nodes through the graph.

        Args:
            seed_activations: Map of node_id -> initial activation [0, 1]
            max_hops: Maximum hops to propagate (default: config.spread_hops)
            user_id: User ID for filtering (SISA-compliant sharding)

        Returns:
            List of ActivationState objects for all activated nodes
        """
        max_hops = max_hops if max_hops is not None else self.config.spread_hops

        if max_hops == 0:
            # No spreading, return only seed nodes
            return [
                ActivationState(node_id=nid, activation=act, hop=0)
                for nid, act in seed_activations.items()
            ]

        # Initialize activation map
        # Format: {node_id: {"activation": float, "hop": int, "source": str}}
        all_activations: dict[str, dict[str, Any]] = {}
        for nid, act in seed_activations.items():
            all_activations[nid] = {"activation": act, "hop": 0, "source": None}

        current_layer = seed_activations.copy()

        # Propagate for max_hops iterations
        for hop in range(1, max_hops + 1):
            next_layer = await self._propagate_one_hop(
                current_layer=current_layer,
                all_activations=all_activations,
                hop=hop,
                user_id=user_id,
            )

            if not next_layer:
                logger.debug(f"Activation spreading stopped at hop {hop} (no new activations)")
                break

            current_layer = next_layer

        # Convert to ActivationState list
        results = [
            ActivationState(
                node_id=nid,
                activation=data["activation"],
                hop=data["hop"],
                source_node_id=data["source"],
            )
            for nid, data in all_activations.items()
        ]

        # Sort by activation (descending)
        results.sort(key=lambda x: x.activation, reverse=True)

        logger.info(
            f"Activation spread complete: {len(results)} nodes activated "
            f"({len(seed_activations)} seeds, {max_hops} hops)"
        )

        return results

    async def _propagate_one_hop(
        self,
        current_layer: dict[str, float],
        all_activations: dict[str, dict[str, Any]],
        hop: int,
        user_id: str | None,
    ) -> dict[str, float]:
        """Propagate activation from current layer to neighbors.

        Args:
            current_layer: Map of node_id -> activation for current hop
            all_activations: Accumulated activations (will be updated in-place)
            hop: Current hop number (distance from seeds)
            user_id: User ID for filtering

        Returns:
            Map of node_id -> activation for next layer
        """
        next_layer: dict[str, float] = defaultdict(float)

        for src_id, src_activation in current_layer.items():
            # Get outgoing edges (neighbors) - Issue #84: SQL backend
            try:
                from uuid import UUID

                src_uuid = UUID(src_id) if isinstance(src_id, str) else src_id
                outgoing_edges = await self.graph.edge_repo.get_outgoing_edges(
                    self.graph.user_id, src_uuid
                )
            except Exception as e:
                logger.debug(f"Failed to get neighbors for {src_id}: {e}")
                continue

            for edge in outgoing_edges:
                dst_id = str(edge.dst_id)
                weight = edge.weight

                # GDPR compliance: user_id filtering now handled at SQL level
                # (NeuralEdgeRepository filters by user_id)

                # Calculate propagated activation
                # activation(dst) += activation(src) * decay * weight(src→dst)
                propagated_activation = src_activation * self.config.spread_decay * weight

                # Check threshold
                if propagated_activation < self.config.spread_threshold:
                    continue

                # Accumulate activation (sum from multiple paths)
                next_layer[dst_id] += propagated_activation

                # Update global activation map (keep max activation)
                if dst_id in all_activations:
                    # Node already activated in a previous hop - keep max
                    if propagated_activation > all_activations[dst_id]["activation"]:
                        all_activations[dst_id] = {
                            "activation": propagated_activation,
                            "hop": hop,
                            "source": src_id,
                        }
                else:
                    # First time activating this node
                    all_activations[dst_id] = {
                        "activation": propagated_activation,
                        "hop": hop,
                        "source": src_id,
                    }

        logger.debug(
            f"Hop {hop}: propagated to {len(next_layer)} new nodes "
            f"(from {len(current_layer)} sources)"
        )

        return dict(next_layer)

    def get_association_score(
        self,
        seed_nodes: list[str],
        target_node: str,
        max_hops: int | None = None,
    ) -> float:
        """Calculate association score between seed nodes and a target node.

        This is used in the unified scoring function (beta · assoc(q→i)).

        Args:
            seed_nodes: List of seed node IDs (e.g., primary retrieval results)
            target_node: Target node ID to score
            max_hops: Maximum hops to consider (default: config.spread_hops)

        Returns:
            Association score [0, 1]
            (0 = not reachable, 1 = direct neighbor with high weight)
        """
        if not seed_nodes:
            return 0.0

        # Initialize with uniform activation
        seed_activations = dict.fromkeys(seed_nodes, 1.0)

        # Spread activation
        all_activations = self.spread(
            seed_activations=seed_activations,
            max_hops=max_hops,
        )

        # Find target node in results
        for activation_state in all_activations:
            if activation_state.node_id == target_node:
                return activation_state.activation

        return 0.0

    def find_related_nodes(
        self,
        seed_nodes: list[str],
        top_k: int = 10,
        max_hops: int | None = None,
        exclude_seeds: bool = True,
    ) -> list[tuple[str, float]]:
        """Find nodes related to seed nodes via activation spreading.

        Args:
            seed_nodes: List of seed node IDs
            top_k: Number of top related nodes to return
            max_hops: Maximum hops (default: config.spread_hops)
            exclude_seeds: Whether to exclude seed nodes from results

        Returns:
            List of (node_id, activation_score) tuples, sorted by score descending
        """
        seed_activations = dict.fromkeys(seed_nodes, 1.0)
        all_activations = self.spread(seed_activations=seed_activations, max_hops=max_hops)

        # Filter and sort
        results = [
            (state.node_id, state.activation)
            for state in all_activations
            if not (exclude_seeds and state.node_id in seed_nodes)
        ]

        results.sort(key=lambda x: x[1], reverse=True)

        return results[:top_k]

    async def visualize_activation_graph(
        self,
        seed_activations: dict[str, float],
        max_hops: int | None = None,
        output_path: str | None = None,
    ) -> dict[str, Any]:
        """Visualize activation spreading (for debugging/analysis).

        Args:
            seed_activations: Initial activations
            max_hops: Maximum hops
            output_path: Optional path to save visualization (requires matplotlib)

        Returns:
            Dict with visualization data (nodes, edges, activations)
        """
        all_activations = self.spread(seed_activations=seed_activations, max_hops=max_hops)

        # Build visualization data
        viz_data = {
            "nodes": {},
            "edges": [],
            "metadata": {
                "seed_count": len(seed_activations),
                "total_activated": len(all_activations),
                "max_hops": max_hops or self.config.spread_hops,
            },
        }

        # Add nodes with activation values
        for state in all_activations:
            viz_data["nodes"][state.node_id] = {
                "activation": state.activation,
                "hop": state.hop,
                "is_seed": state.node_id in seed_activations,
            }

        # Add edges (only between activated nodes) - Issue #84: SQL backend
        activated_ids = {state.node_id for state in all_activations}
        for node_id in activated_ids:
            try:
                from uuid import UUID

                node_uuid = UUID(node_id) if isinstance(node_id, str) else node_id
                outgoing_edges = await self.graph.edge_repo.get_outgoing_edges(
                    self.graph.user_id, node_uuid
                )
            except Exception:
                continue

            for edge in outgoing_edges:
                neighbor_id = str(edge.dst_id)
                if neighbor_id in activated_ids:
                    viz_data["edges"].append(
                        {
                            "source": node_id,
                            "target": neighbor_id,
                            "weight": edge.weight,
                        }
                    )

        logger.info(
            f"Visualization data: {len(viz_data['nodes'])} nodes, {len(viz_data['edges'])} edges"
        )

        # TODO: Implement actual plotting with matplotlib if output_path is provided
        # if output_path:
        #     import matplotlib.pyplot as plt
        #     ...

        return viz_data
