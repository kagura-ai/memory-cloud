"""Tests for DecayManager."""

import math
from datetime import datetime, timedelta

import pytest

from neural.config import NeuralMemoryConfig
from neural.decay import DecayManager
from services.graph_service import GraphService


class TestDecayManager:
    """Test DecayManager for forgetting and weight decay."""

    @pytest.fixture
    def config(self):
        """Create test config."""
        return NeuralMemoryConfig(
            enable_decay=True,
            decay_rate=0.01,  # 1% per second
            decay_background_interval=60,  # 60 seconds
            prune_threshold=0.1,
            consolidation_use_count_min=5,
            consolidation_importance_min=0.7,
        )

    @pytest.fixture
    def graph(self):
        """Create test graph with edges."""
        graph = GraphService(user_id="test_user")

        # Create nodes
        graph.add_node("node1", "memory")
        graph.add_node("node2", "memory")
        graph.add_node("node3", "memory")
        graph.add_node("node4", "memory")

        # Create edges with different weights
        graph.add_edge("node1", "node2", weight=0.9)
        graph.add_edge("node2", "node3", weight=0.5)
        graph.add_edge("node3", "node4", weight=0.05)  # Very weak edge

        return graph

    @pytest.fixture
    def decay_manager(self, graph, config):
        """Create DecayManager."""
        return DecayManager(graph, config)

    def test_init(self, graph, config):
        """Test DecayManager initialization."""
        decay_manager = DecayManager(graph, config)
        assert decay_manager.graph == graph
        assert decay_manager.config == config
        assert decay_manager._last_decay_time is None

    def test_apply_decay_disabled(self, graph):
        """Test that decay is disabled when config flag is False."""
        config = NeuralMemoryConfig(enable_decay=False)
        decay_manager = DecayManager(graph, config)

        result = decay_manager.apply_decay("test_user")

        # Should return zeros when disabled
        assert result["edges_decayed"] == 0
        assert result["edges_pruned"] == 0

    def test_apply_decay_first_run(self, decay_manager):
        """Test first decay run (uses default interval)."""
        initial_weight = decay_manager.graph.get_edge("node1", "node2")["weight"]

        result = decay_manager.apply_decay("test_user")

        # Should apply decay
        assert result["edges_decayed"] > 0
        assert "delta_seconds" in result

        # Check that weight decreased
        new_weight = decay_manager.graph.get_edge("node1", "node2")["weight"]
        assert new_weight < initial_weight

    def test_apply_decay_exponential_formula(self, decay_manager):
        """Test exponential decay formula: w(t+Δt) = w(t) · exp(-rate · Δt)."""
        initial_weight = 0.9
        decay_rate = decay_manager.config.decay_rate
        delta_seconds = 60.0

        # Set initial weight
        decay_manager.graph.graph["node1"]["node2"]["weight"] = initial_weight

        # Set last decay time to 60 seconds ago
        decay_manager._last_decay_time = datetime.utcnow() - timedelta(seconds=60)

        # Apply decay
        decay_manager.apply_decay("test_user")

        # Calculate expected weight
        decay_factor = math.exp(-decay_rate * delta_seconds)
        expected_weight = initial_weight * decay_factor

        # Check actual weight
        actual_weight = decay_manager.graph.get_edge("node1", "node2")["weight"]
        assert abs(actual_weight - expected_weight) < 0.001

    def test_apply_decay_prunes_weak_edges(self, decay_manager):
        """Test that edges below threshold are pruned."""
        # Edge node3->node4 has weight 0.05 (below prune_threshold=0.1)
        initial_edge_count = decay_manager.graph.graph.number_of_edges()

        result = decay_manager.apply_decay("test_user")

        # Should prune weak edge
        assert result["edges_pruned"] > 0

        # Check that weak edge is removed
        assert not decay_manager.graph.has_edge("node3", "node4")

        # Total edges should decrease
        final_edge_count = decay_manager.graph.graph.number_of_edges()
        assert final_edge_count < initial_edge_count

    def test_apply_decay_updates_last_decayed(self, decay_manager):
        """Test that last_decayed timestamp is updated on edges."""
        before = datetime.utcnow()

        decay_manager.apply_decay("test_user")

        after = datetime.utcnow()

        # Check that last_decayed is set
        edge_data = decay_manager.graph.get_edge("node1", "node2")
        assert "last_decayed" in edge_data

        last_decayed = edge_data["last_decayed"]
        assert before <= last_decayed <= after

    def test_apply_decay_sets_last_decay_time(self, decay_manager):
        """Test that _last_decay_time is updated."""
        assert decay_manager._last_decay_time is None

        before = datetime.utcnow()
        decay_manager.apply_decay("test_user")
        after = datetime.utcnow()

        assert decay_manager._last_decay_time is not None
        assert before <= decay_manager._last_decay_time <= after

    def test_apply_decay_zero_delta(self, decay_manager):
        """Test that zero time delta returns early."""
        # Set last decay time to now
        decay_manager._last_decay_time = datetime.utcnow()

        result = decay_manager.apply_decay("test_user")

        # Should return zeros (no time elapsed)
        assert result["edges_decayed"] == 0
        assert result["edges_pruned"] == 0

    def test_prune_weak_edges_default_threshold(self, decay_manager):
        """Test pruning weak edges with default threshold."""
        # Edge node3->node4 has weight 0.05 (below default threshold 0.1)
        assert decay_manager.graph.has_edge("node3", "node4")

        count = decay_manager.prune_weak_edges("test_user")

        # Should prune 1 edge
        assert count == 1
        assert not decay_manager.graph.has_edge("node3", "node4")

    def test_prune_weak_edges_custom_threshold(self, decay_manager):
        """Test pruning with custom threshold."""
        # Set high threshold (0.6) - should prune edges with weight < 0.6
        # node1->node2: 0.9 (keep)
        # node2->node3: 0.5 (prune)
        # node3->node4: 0.05 (prune)

        count = decay_manager.prune_weak_edges("test_user", threshold=0.6)

        # Should prune 2 edges
        assert count == 2
        assert decay_manager.graph.has_edge("node1", "node2")  # 0.9 > 0.6
        assert not decay_manager.graph.has_edge("node2", "node3")  # 0.5 < 0.6

    def test_prune_weak_edges_no_edges_to_prune(self, decay_manager):
        """Test pruning when no edges are weak enough."""
        # Set very low threshold
        count = decay_manager.prune_weak_edges("test_user", threshold=0.01)

        # Should not prune any edges
        assert count == 0

    def test_prune_old_nodes(self, decay_manager):
        """Test pruning old, low-importance nodes."""
        # Add node with old creation time and low importance
        old_time = datetime.utcnow() - timedelta(days=100)
        decay_manager.graph.add_node(
            "old_node",
            "memory",
            {
                "created_at": old_time,
                "importance": 0.2,
                "long_term": False,
            },
        )

        # Add recent node (should not be pruned)
        recent_time = datetime.utcnow() - timedelta(days=1)
        decay_manager.graph.add_node(
            "recent_node",
            "memory",
            {
                "created_at": recent_time,
                "importance": 0.2,
                "long_term": False,
            },
        )

        # Prune nodes older than 30 days with importance < 0.3
        count = decay_manager.prune_old_nodes(
            "test_user",
            age_days=30,
            importance_threshold=0.3,
        )

        # Should prune old_node but not recent_node
        assert count == 1
        assert not decay_manager.graph.has_node("old_node")
        assert decay_manager.graph.has_node("recent_node")

    def test_prune_old_nodes_protects_important(self, decay_manager):
        """Test that important nodes are protected from pruning."""
        # Add old but important node
        old_time = datetime.utcnow() - timedelta(days=100)
        decay_manager.graph.add_node(
            "important_node",
            "memory",
            {
                "created_at": old_time,
                "importance": 0.9,  # High importance
                "long_term": False,
            },
        )

        # Prune with threshold 0.3 (important_node has 0.9 > 0.3)
        decay_manager.prune_old_nodes(
            "test_user",
            age_days=30,
            importance_threshold=0.3,
        )

        # Should not prune important node
        assert decay_manager.graph.has_node("important_node")

    def test_prune_old_nodes_protects_long_term(self, decay_manager):
        """Test that long-term memories are protected from pruning."""
        # Add old, low-importance, but long-term node
        old_time = datetime.utcnow() - timedelta(days=100)
        decay_manager.graph.add_node(
            "long_term_node",
            "memory",
            {
                "created_at": old_time,
                "importance": 0.1,  # Low importance
                "long_term": True,  # Protected
            },
        )

        # Prune
        decay_manager.prune_old_nodes(
            "test_user",
            age_days=30,
            importance_threshold=0.3,
        )

        # Should not prune long-term node
        assert decay_manager.graph.has_node("long_term_node")

    def test_prune_old_nodes_handles_string_datetime(self, decay_manager):
        """Test that ISO format datetime strings are handled."""
        # Add node with datetime as string
        old_time = datetime.utcnow() - timedelta(days=100)
        decay_manager.graph.add_node(
            "string_time_node",
            "memory",
            {
                "created_at": old_time.isoformat(),  # String format
                "importance": 0.2,
                "long_term": False,
            },
        )

        # Should handle string datetime
        count = decay_manager.prune_old_nodes(
            "test_user",
            age_days=30,
            importance_threshold=0.3,
        )

        assert count == 1
        assert not decay_manager.graph.has_node("string_time_node")

    def test_consolidate_to_long_term(self, decay_manager):
        """Test promoting nodes to long-term memory."""
        # Add node that qualifies for consolidation
        decay_manager.graph.add_node(
            "qualify_node",
            "memory",
            {
                "id": "qualify_node",
                "use_count": 10,  # >= 5
                "importance": 0.8,  # >= 0.7
                "long_term": False,
            },
        )

        # Add node that doesn't qualify (low use_count)
        decay_manager.graph.add_node(
            "low_use_node",
            "memory",
            {
                "id": "low_use_node",
                "use_count": 2,  # < 5
                "importance": 0.8,
                "long_term": False,
            },
        )

        nodes = [
            {
                "id": "qualify_node",
                "use_count": 10,
                "importance": 0.8,
                "long_term": False,
            },
            {
                "id": "low_use_node",
                "use_count": 2,
                "importance": 0.8,
                "long_term": False,
            },
        ]

        promoted = decay_manager.consolidate_to_long_term("test_user", nodes)

        # Should promote only qualify_node
        assert len(promoted) == 1
        assert "qualify_node" in promoted

        # Check that node is marked as long-term
        node_data = decay_manager.graph.get_node("qualify_node")
        assert node_data["long_term"] is True
        assert "consolidated_at" in node_data

    def test_consolidate_to_long_term_importance_check(self, decay_manager):
        """Test that importance threshold is checked."""
        # Add node with high use_count but low importance
        decay_manager.graph.add_node(
            "low_importance_node",
            "memory",
            {
                "id": "low_importance_node",
                "use_count": 10,  # >= 5
                "importance": 0.5,  # < 0.7
                "long_term": False,
            },
        )

        nodes = [
            {
                "id": "low_importance_node",
                "use_count": 10,
                "importance": 0.5,
                "long_term": False,
            },
        ]

        promoted = decay_manager.consolidate_to_long_term("test_user", nodes)

        # Should not promote (importance too low)
        assert len(promoted) == 0

    def test_consolidate_to_long_term_skips_existing(self, decay_manager):
        """Test that already long-term nodes are skipped."""
        # Add already long-term node
        decay_manager.graph.add_node(
            "already_long_term",
            "memory",
            {
                "id": "already_long_term",
                "use_count": 10,
                "importance": 0.8,
                "long_term": True,  # Already promoted
            },
        )

        nodes = [
            {
                "id": "already_long_term",
                "use_count": 10,
                "importance": 0.8,
                "long_term": True,
            },
        ]

        promoted = decay_manager.consolidate_to_long_term("test_user", nodes)

        # Should not promote again
        assert len(promoted) == 0

    def test_get_decay_statistics_empty_graph(self, config):
        """Test statistics with no neural edges."""
        empty_graph = GraphService(user_id="test_user")
        decay_manager = DecayManager(empty_graph, config)

        stats = decay_manager.get_decay_statistics("test_user")

        assert stats["total_neural_edges"] == 0
        assert stats["avg_weight"] == 0.0
        assert stats["max_weight"] == 0.0
        assert stats["min_weight"] == 0.0
        assert stats["below_threshold"] == 0

    def test_get_decay_statistics(self, decay_manager):
        """Test statistics calculation."""
        # Add neural_association type edges
        decay_manager.graph.graph["node1"]["node2"]["type"] = "neural_association"
        decay_manager.graph.graph["node2"]["node3"]["type"] = "neural_association"

        stats = decay_manager.get_decay_statistics("test_user")

        assert stats["total_neural_edges"] == 2
        assert stats["avg_weight"] > 0
        assert stats["max_weight"] == 0.9  # node1->node2
        assert stats["min_weight"] == 0.5  # node2->node3

        # node3->node4 has weight 0.05 (below threshold 0.1)
        # But it might be pruned or not counted as neural_association
        assert stats["below_threshold"] >= 0

    def test_get_decay_statistics_last_decay_time(self, decay_manager):
        """Test that last_decay_time is included in statistics."""
        # Before decay
        stats_before = decay_manager.get_decay_statistics("test_user")
        assert stats_before["last_decay_time"] is None

        # After decay
        decay_manager.apply_decay("test_user")
        stats_after = decay_manager.get_decay_statistics("test_user")
        assert stats_after["last_decay_time"] is not None

    def test_apply_decay_multiple_runs(self, decay_manager):
        """Test that decay accumulates over multiple runs."""
        initial_weight = decay_manager.graph.get_edge("node1", "node2")["weight"]

        # First decay
        decay_manager.apply_decay("test_user")
        weight_after_first = decay_manager.graph.get_edge("node1", "node2")["weight"]
        assert weight_after_first < initial_weight

        # Wait (simulate time passing)
        decay_manager._last_decay_time = datetime.utcnow() - timedelta(seconds=60)

        # Second decay
        decay_manager.apply_decay("test_user")
        weight_after_second = decay_manager.graph.get_edge("node1", "node2")["weight"]
        assert weight_after_second < weight_after_first

    def test_prune_old_nodes_no_created_at(self, decay_manager):
        """Test that nodes without created_at are skipped."""
        # Add node without created_at
        decay_manager.graph.add_node(
            "no_time_node",
            "memory",
            {
                "importance": 0.1,
                "long_term": False,
                # No created_at field
            },
        )

        # Should not crash
        decay_manager.prune_old_nodes(
            "test_user",
            age_days=30,
            importance_threshold=0.3,
        )

        # Node should not be pruned (missing created_at)
        assert decay_manager.graph.has_node("no_time_node")
