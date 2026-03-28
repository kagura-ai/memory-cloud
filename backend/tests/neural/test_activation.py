"""Tests for ActivationSpreader."""

import pytest

from neural.activation import ActivationSpreader
from neural.config import NeuralMemoryConfig
from services.graph_service import GraphService


class TestActivationSpreader:
    """Test ActivationSpreader graph-based activation propagation."""

    @pytest.fixture
    def config(self):
        """Create test config."""
        return NeuralMemoryConfig(
            spread_hops=2,
            spread_decay=0.8,
            spread_threshold=0.1,
        )

    @pytest.fixture
    def graph(self):
        """Create test graph with nodes and edges."""
        graph = GraphService(user_id="test_user")

        # Create a simple network:
        #   node1 --0.9--> node2 --0.7--> node3
        #   node1 --0.5--> node4
        graph.add_node("node1", "memory", {"user_id": "test_user"})
        graph.add_node("node2", "memory", {"user_id": "test_user"})
        graph.add_node("node3", "memory", {"user_id": "test_user"})
        graph.add_node("node4", "memory", {"user_id": "test_user"})

        graph.add_edge("node1", "node2", weight=0.9)
        graph.add_edge("node2", "node3", weight=0.7)
        graph.add_edge("node1", "node4", weight=0.5)

        return graph

    @pytest.fixture
    def spreader(self, graph, config):
        """Create ActivationSpreader."""
        return ActivationSpreader(graph, config)

    def test_init(self, graph, config):
        """Test ActivationSpreader initialization."""
        spreader = ActivationSpreader(graph, config)
        assert spreader.graph == graph
        assert spreader.config == config

    def test_spread_zero_hops(self, spreader):
        """Test spreading with max_hops=0 (no propagation)."""
        seed_activations = {"node1": 1.0, "node2": 0.8}

        results = spreader.spread(seed_activations, max_hops=0)

        # Should return only seed nodes
        assert len(results) == 2

        # Check seed nodes are present
        node_ids = {r.node_id for r in results}
        assert "node1" in node_ids
        assert "node2" in node_ids

        # Check activations match seeds
        for result in results:
            assert result.activation == seed_activations[result.node_id]
            assert result.hop == 0

    def test_spread_one_hop(self, spreader):
        """Test spreading with 1 hop."""
        seed_activations = {"node1": 1.0}

        results = spreader.spread(seed_activations, max_hops=1)

        # Should activate node1 (seed) + neighbors (node2, node4)
        node_ids = {r.node_id for r in results}
        assert "node1" in node_ids  # Seed
        assert "node2" in node_ids  # Neighbor
        assert "node4" in node_ids  # Neighbor

        # Check activations are calculated correctly
        # activation(node2) = activation(node1) * decay * weight(1→2)
        # = 1.0 * 0.8 * 0.9 = 0.72
        node2_result = next(r for r in results if r.node_id == "node2")
        expected_activation = 1.0 * 0.8 * 0.9  # seed * decay * weight
        assert abs(node2_result.activation - expected_activation) < 0.001
        assert node2_result.hop == 1
        assert node2_result.source_node_id == "node1"

        # node4: 1.0 * 0.8 * 0.5 = 0.4
        node4_result = next(r for r in results if r.node_id == "node4")
        expected_activation = 1.0 * 0.8 * 0.5
        assert abs(node4_result.activation - expected_activation) < 0.001

    def test_spread_two_hops(self, spreader):
        """Test spreading with 2 hops."""
        seed_activations = {"node1": 1.0}

        results = spreader.spread(seed_activations, max_hops=2)

        # Should reach node3 in hop 2
        node_ids = {r.node_id for r in results}
        assert "node3" in node_ids

        # Check node3 activation
        # node3 activated from node2
        # activation(node2) = 1.0 * 0.8 * 0.9 = 0.72 (hop 1)
        # activation(node3) = 0.72 * 0.8 * 0.7 = 0.4032 (hop 2)
        node3_result = next(r for r in results if r.node_id == "node3")
        expected_activation = (1.0 * 0.8 * 0.9) * 0.8 * 0.7
        assert abs(node3_result.activation - expected_activation) < 0.001
        assert node3_result.hop == 2
        assert node3_result.source_node_id == "node2"

    def test_spread_threshold_filtering(self, spreader):
        """Test that activations below threshold are filtered."""
        # Set high threshold
        spreader.config.spread_threshold = 0.5

        seed_activations = {"node1": 1.0}
        results = spreader.spread(seed_activations, max_hops=1)

        # node2: 1.0 * 0.8 * 0.9 = 0.72 (above threshold, should be included)
        # node4: 1.0 * 0.8 * 0.5 = 0.4 (below threshold, should be filtered)
        node_ids = {r.node_id for r in results}
        assert "node2" in node_ids
        assert "node4" not in node_ids

    def test_spread_multiple_seeds(self, spreader):
        """Test spreading from multiple seed nodes."""
        seed_activations = {"node1": 1.0, "node2": 0.5}

        results = spreader.spread(seed_activations, max_hops=1)

        # node3 should receive activation from node2
        node_ids = {r.node_id for r in results}
        assert "node3" in node_ids

        # node3: 0.5 * 0.8 * 0.7 = 0.28
        node3_result = next(r for r in results if r.node_id == "node3")
        expected_activation = 0.5 * 0.8 * 0.7
        assert abs(node3_result.activation - expected_activation) < 0.001

    def test_spread_results_sorted_by_activation(self, spreader):
        """Test that results are sorted by activation (descending)."""
        seed_activations = {"node1": 1.0}
        results = spreader.spread(seed_activations, max_hops=1)

        # Check sorted descending
        for i in range(len(results) - 1):
            assert results[i].activation >= results[i + 1].activation

    def test_spread_user_filtering(self, spreader, graph):
        """Test user_id filtering for GDPR compliance."""
        # Add node from different user
        graph.add_node("other_user_node", "memory", {"user_id": "other_user"})
        graph.add_edge("node1", "other_user_node", weight=0.9)

        seed_activations = {"node1": 1.0}
        results = spreader.spread(seed_activations, max_hops=1, user_id="test_user")

        # other_user_node should be filtered out
        node_ids = {r.node_id for r in results}
        assert "other_user_node" not in node_ids

    def test_get_association_score_direct_neighbor(self, spreader):
        """Test association score calculation for direct neighbor."""
        score = spreader.get_association_score(
            seed_nodes=["node1"],
            target_node="node2",
            max_hops=1,
        )

        # Should be 1.0 * 0.8 * 0.9 = 0.72
        expected_score = 1.0 * 0.8 * 0.9
        assert abs(score - expected_score) < 0.001

    def test_get_association_score_distant_node(self, spreader):
        """Test association score for node 2 hops away."""
        score = spreader.get_association_score(
            seed_nodes=["node1"],
            target_node="node3",
            max_hops=2,
        )

        # Should be (1.0 * 0.8 * 0.9) * 0.8 * 0.7 = 0.4032
        expected_score = (1.0 * 0.8 * 0.9) * 0.8 * 0.7
        assert abs(score - expected_score) < 0.001

    def test_get_association_score_unreachable(self, spreader, graph):
        """Test association score for unreachable node."""
        # Add disconnected node
        graph.add_node("isolated", "memory")

        score = spreader.get_association_score(
            seed_nodes=["node1"],
            target_node="isolated",
            max_hops=2,
        )

        assert score == 0.0

    def test_get_association_score_no_seeds(self, spreader):
        """Test association score with no seed nodes."""
        score = spreader.get_association_score(
            seed_nodes=[],
            target_node="node2",
        )

        assert score == 0.0

    def test_find_related_nodes(self, spreader):
        """Test finding related nodes."""
        related = spreader.find_related_nodes(
            seed_nodes=["node1"],
            top_k=3,
            max_hops=2,
            exclude_seeds=True,
        )

        # Should return (node_id, activation_score) tuples
        assert len(related) <= 3
        assert all(isinstance(r, tuple) for r in related)
        assert all(len(r) == 2 for r in related)

        # node1 should be excluded (seed)
        node_ids = [r[0] for r in related]
        assert "node1" not in node_ids

        # Should be sorted by activation (descending)
        activations = [r[1] for r in related]
        assert activations == sorted(activations, reverse=True)

    def test_find_related_nodes_include_seeds(self, spreader):
        """Test finding related nodes including seeds."""
        related = spreader.find_related_nodes(
            seed_nodes=["node1"],
            top_k=5,
            max_hops=1,
            exclude_seeds=False,
        )

        # node1 should be included with activation 1.0
        node_ids = [r[0] for r in related]
        assert "node1" in node_ids

        # node1 should have highest activation (1.0)
        node1_activation = next(r[1] for r in related if r[0] == "node1")
        assert node1_activation == 1.0

    def test_visualize_activation_graph(self, spreader):
        """Test activation graph visualization data generation."""
        seed_activations = {"node1": 1.0}

        viz_data = spreader.visualize_activation_graph(
            seed_activations=seed_activations,
            max_hops=2,
        )

        # Check structure
        assert "nodes" in viz_data
        assert "edges" in viz_data
        assert "metadata" in viz_data

        # Check nodes
        assert len(viz_data["nodes"]) > 0
        assert "node1" in viz_data["nodes"]
        assert viz_data["nodes"]["node1"]["is_seed"] is True
        assert viz_data["nodes"]["node1"]["activation"] == 1.0
        assert viz_data["nodes"]["node1"]["hop"] == 0

        # Check metadata
        assert viz_data["metadata"]["seed_count"] == 1
        assert viz_data["metadata"]["total_activated"] > 0
        assert viz_data["metadata"]["max_hops"] == 2

        # Check edges
        assert len(viz_data["edges"]) > 0
        for edge in viz_data["edges"]:
            assert "source" in edge
            assert "target" in edge
            assert "weight" in edge

    def test_spread_max_activation_from_multiple_paths(self, spreader, graph):
        """Test that max activation is kept when node is reached via multiple paths."""
        # Create diamond structure:
        #   node1 --0.9--> node2 --0.8--> node5
        #   node1 --0.5--> node4 --0.9--> node5
        graph.add_node("node5", "memory", {"user_id": "test_user"})
        graph.add_edge("node2", "node5", weight=0.8)
        graph.add_edge("node4", "node5", weight=0.9)

        seed_activations = {"node1": 1.0}
        results = spreader.spread(seed_activations, max_hops=2)

        # node5 receives activation from both node2 and node4
        # Path 1 (via node2): 1.0 * 0.8 * 0.9 * 0.8 * 0.8 = 0.4608
        # Path 2 (via node4): 1.0 * 0.8 * 0.5 * 0.8 * 0.9 = 0.288
        # Should keep max = 0.4608

        node5_result = next(r for r in results if r.node_id == "node5")
        path1_activation = (1.0 * 0.8 * 0.9) * 0.8 * 0.8

        # Should be close to path1 (higher activation)
        assert abs(node5_result.activation - path1_activation) < 0.001
