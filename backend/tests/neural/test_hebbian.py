"""Tests for HebbianLearner."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from neural.config import NeuralMemoryConfig
from neural.hebbian import HebbianLearner
from neural.models import ActivationState, MemoryKind, NeuralMemoryNode


class TestHebbianLearner:
    """Test Hebbian learning algorithm."""

    @pytest.fixture
    def mock_graph(self):
        """Create mock graph service."""
        graph = MagicMock()
        graph.user_id = "test_user"
        graph.db = MagicMock()
        graph.edge_repo = MagicMock()
        graph.edge_repo.get_edge_weight = AsyncMock(return_value=0.5)
        graph.edge_repo.update_edge_weight = AsyncMock(return_value=True)
        graph.edge_repo.create_edge = AsyncMock()
        graph.edge_repo.get_outgoing_edges_count = AsyncMock(return_value=5)
        graph.edge_repo.prune_weakest_edges = AsyncMock(return_value=0)
        # _get_current_weight uses graph.get_edge()
        graph.get_edge = AsyncMock(return_value={"weight": 0.5})
        graph.has_edge = AsyncMock(return_value=True)
        graph.remove_edge = AsyncMock()
        graph.update_edge = AsyncMock()
        return graph

    @pytest.fixture
    def config(self):
        """Create test config."""
        return NeuralMemoryConfig(
            learning_rate=0.1,
            decay_lambda=0.01,
            weight_max=3.0,
            gradient_clipping=1.0,
            top_m_edges=10,
        )

    @pytest.fixture
    def learner(self, mock_graph, config):
        """Create Hebbian learner."""
        return HebbianLearner(mock_graph, config)

    def test_init(self, mock_graph, config):
        """Test HebbianLearner initialization."""
        learner = HebbianLearner(mock_graph, config)
        assert learner.graph == mock_graph
        assert learner.config == config

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
        expected = 0.1 * 0.8 * 0.9 - 0.01 * 0.5
        assert abs(delta - expected) < 0.0001

    def test_calculate_delta_weight_zero_activation(self, learner):
        """Test delta weight with zero activation."""
        delta = learner._calculate_delta_weight(
            activation_i=0.0,
            activation_j=0.9,
            confidence_i=1.0,
            confidence_j=1.0,
            current_weight=0.5,
        )

        # Delta = 0.1 * 0 * 0.9 - 0.01 * 0.5 = -0.005
        expected = -0.01 * 0.5
        assert abs(delta - expected) < 0.0001

    def test_calculate_delta_weight_low_confidence(self, learner):
        """Test delta weight with low confidence (trust modulation)."""
        delta = learner._calculate_delta_weight(
            activation_i=1.0,
            activation_j=1.0,
            confidence_i=0.5,
            confidence_j=0.5,
            current_weight=0.5,
        )

        # Delta = 0.1 * (1.0 * 0.5) * (1.0 * 0.5) - 0.01 * 0.5
        # = 0.1 * 0.25 - 0.005 = 0.025 - 0.005 = 0.02
        expected = 0.1 * 0.25 - 0.01 * 0.5
        assert abs(delta - expected) < 0.0001

    @pytest.mark.asyncio
    async def test_queue_update(self, learner, mock_graph):
        """Test queuing Hebbian updates for co-activated nodes."""
        activations = [
            ActivationState(node_id="node1", activation=0.8),
            ActivationState(node_id="node2", activation=0.9),
        ]

        nodes = {
            "node1": NeuralMemoryNode(
                id="node1",
                user_id="test_user",
                kind=MemoryKind.FACT,
                text="Memory 1",
                embedding=[0.1] * 512,
                created_at=datetime.utcnow(),
                use_count=5,
                importance=0.8,
                confidence=1.0,
            ),
            "node2": NeuralMemoryNode(
                id="node2",
                user_id="test_user",
                kind=MemoryKind.FACT,
                text="Memory 2",
                embedding=[0.2] * 512,
                created_at=datetime.utcnow(),
                use_count=3,
                importance=0.6,
                confidence=1.0,
            ),
        }

        await learner.queue_update("test_user", activations, nodes)

        # Should queue bidirectional updates
        assert len(learner._update_queue["test_user"]) == 2

    @pytest.mark.asyncio
    async def test_apply_updates_empty(self, learner):
        """Test applying updates with empty queue."""
        result = await learner.apply_updates("test_user")
        assert result == 0

    @pytest.mark.asyncio
    async def test_apply_updates(self, learner, mock_graph):
        """Test applying queued updates."""
        activations = [
            ActivationState(node_id="node1", activation=0.8),
            ActivationState(node_id="node2", activation=0.9),
        ]

        nodes = {
            "node1": NeuralMemoryNode(
                id="node1",
                user_id="test_user",
                kind=MemoryKind.FACT,
                text="Memory 1",
                embedding=[0.1] * 512,
                created_at=datetime.utcnow(),
                use_count=5,
                importance=0.8,
                confidence=1.0,
            ),
            "node2": NeuralMemoryNode(
                id="node2",
                user_id="test_user",
                kind=MemoryKind.FACT,
                text="Memory 2",
                embedding=[0.2] * 512,
                created_at=datetime.utcnow(),
                use_count=3,
                importance=0.6,
                confidence=1.0,
            ),
        }

        await learner.queue_update("test_user", activations, nodes)
        result = await learner.apply_updates("test_user")

        # Should have applied updates
        assert result >= 0

    def test_clip_gradients(self, learner):
        """Test gradient clipping."""
        edge_deltas = {
            ("n1", "n2"): 5.0,  # Should be clipped
            ("n3", "n4"): 0.1,  # Within bounds
        }

        clipped = learner._clip_gradients(edge_deltas)

        # Large delta should be clipped
        assert abs(clipped[("n1", "n2")]) <= learner.config.gradient_clipping
        # Small delta should be preserved
        assert abs(clipped[("n3", "n4")] - 0.1) < 0.0001
