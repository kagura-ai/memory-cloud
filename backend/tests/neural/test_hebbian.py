"""Tests for HebbianLearner."""

from datetime import datetime

import pytest

from neural.config import NeuralMemoryConfig
from neural.hebbian import HebbianLearner
from neural.models import ActivationState, MemoryKind, NeuralMemoryNode
from services.graph_service import GraphService


class TestHebbianLearner:
    """Test Hebbian learning algorithm."""

    @pytest.fixture
    def graph(self):
        """Create test graph."""
        graph = GraphService(user_id="test_user")
        graph.add_node("node1", "memory")
        graph.add_node("node2", "memory")
        graph.add_node("node3", "memory")
        return graph

    @pytest.fixture
    def config(self):
        """Create test config."""
        return NeuralMemoryConfig(
            learning_rate=0.1,
            decay_lambda=0.01,
            weight_max=3.0,
        )

    @pytest.fixture
    def learner(self, graph, config):
        """Create Hebbian learner."""
        return HebbianLearner(graph, config)

    def test_calculate_delta_weight(self, learner):
        """Test weight delta calculation."""
        delta = learner._calculate_delta_weight(
            activation_i=0.8,
            activation_j=0.9,
            confidence_i=1.0,
            confidence_j=1.0,
            current_weight=0.5,
        )

        # Delta = learning_rate * (a_i * C_i) * (a_j * C_j) - decay_lambda * w
        # Delta = 0.1 * (0.8 * 1.0) * (0.9 * 1.0) - 0.01 * 0.5
        # Delta = 0.1 * 0.72 - 0.005 = 0.072 - 0.005 = 0.067
        assert abs(delta - 0.067) < 0.001

    def test_queue_update(self, learner, graph):
        """Test queueing Hebbian updates."""
        activations = [
            ActivationState(node_id="node1", activation=0.8),
            ActivationState(node_id="node2", activation=0.9),
        ]

        nodes = {
            "node1": NeuralMemoryNode(
                id="node1",
                user_id="test_user",
                kind=MemoryKind.FACT,
                text="test1",
                embedding=[0.1] * 512,
                created_at=datetime.utcnow(),
                confidence=1.0,
            ),
            "node2": NeuralMemoryNode(
                id="node2",
                user_id="test_user",
                kind=MemoryKind.FACT,
                text="test2",
                embedding=[0.2] * 512,
                created_at=datetime.utcnow(),
                confidence=1.0,
            ),
        }

        learner.queue_update("test_user", activations, nodes)

        # Should queue 2 bidirectional updates (node1->node2, node2->node1)
        assert len(learner._update_queue["test_user"]) == 2

    def test_apply_updates(self, learner, graph):
        """Test applying Hebbian updates."""
        # Add edge
        graph.add_edge("node1", "node2", weight=0.5)

        # Queue update
        activations = [
            ActivationState(node_id="node1", activation=0.8),
            ActivationState(node_id="node2", activation=0.9),
        ]

        nodes = {
            "node1": NeuralMemoryNode(
                id="node1",
                user_id="test_user",
                kind=MemoryKind.FACT,
                text="test1",
                embedding=[0.1] * 512,
                created_at=datetime.utcnow(),
            ),
            "node2": NeuralMemoryNode(
                id="node2",
                user_id="test_user",
                kind=MemoryKind.FACT,
                text="test2",
                embedding=[0.2] * 512,
                created_at=datetime.utcnow(),
            ),
        }

        learner.queue_update("test_user", activations, nodes)

        # Apply updates
        edges_updated = learner.apply_updates("test_user")

        assert edges_updated > 0
        assert len(learner._update_queue["test_user"]) == 0  # Queue cleared

        # Check weight increased
        edge_data = graph.get_edge("node1", "node2")
        assert edge_data["weight"] > 0.5
