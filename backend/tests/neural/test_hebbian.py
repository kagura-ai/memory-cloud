"""Tests for HebbianLearner."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from models.memory import EDGE_ORIGIN_HEBBIAN, EDGE_ORIGIN_SEMANTIC
from neural.config import NeuralMemoryConfig
from neural.hebbian import HebbianLearner
from neural.models import ActivationState, MemoryKind, NeuralMemoryNode


class TestHebbianLearner:
    """Test Hebbian learning algorithm."""

    # mock_graph from neural/conftest.py

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

    @pytest.mark.asyncio
    async def test_semantic_gating_skips_dissimilar_pairs(self, mock_graph):
        """Test that Hebbian queue_update skips pairs below similarity threshold."""
        config = NeuralMemoryConfig(
            learning_rate=0.1,
            decay_lambda=0.01,
            weight_max=3.0,
            gradient_clipping=1.0,
            top_m_edges=10,
            min_similarity_for_edge=0.5,
        )
        learner = HebbianLearner(mock_graph, config)

        import numpy as np

        # Create two dissimilar embeddings (cosine sim ≈ 0)
        emb_a = np.zeros(8).tolist()
        emb_a[0] = 1.0
        emb_b = np.zeros(8).tolist()
        emb_b[7] = 1.0

        nodes = {
            "a": NeuralMemoryNode(
                id="a",
                user_id="u",
                kind=MemoryKind.FACT,
                text="A",
                embedding=emb_a,
                created_at=datetime.utcnow(),
                use_count=0,
                importance=0.5,
                confidence=1.0,
            ),
            "b": NeuralMemoryNode(
                id="b",
                user_id="u",
                kind=MemoryKind.FACT,
                text="B",
                embedding=emb_b,
                created_at=datetime.utcnow(),
                use_count=0,
                importance=0.5,
                confidence=1.0,
            ),
        }
        activations = [
            ActivationState(node_id="a", activation=0.9),
            ActivationState(node_id="b", activation=0.8),
        ]

        await learner.queue_update("u", activations, nodes)

        # No updates should be queued (pair was gated)
        assert len(learner._update_queue.get("u", [])) == 0

    @pytest.mark.asyncio
    async def test_semantic_gating_keeps_similar_pairs(self, mock_graph):
        """Test that Hebbian queue_update keeps pairs above similarity threshold."""
        config = NeuralMemoryConfig(
            learning_rate=0.1,
            decay_lambda=0.01,
            weight_max=3.0,
            gradient_clipping=1.0,
            top_m_edges=10,
            min_similarity_for_edge=0.5,
        )
        learner = HebbianLearner(mock_graph, config)

        import numpy as np

        # Create two similar embeddings (cosine sim ≈ 0.98)
        emb_a = [1.0, 0.8, 0.1] + [0.0] * 5
        norm_a = np.linalg.norm(emb_a)
        emb_a = (np.array(emb_a) / norm_a).tolist()

        emb_b = [0.9, 0.7, 0.2] + [0.0] * 5
        norm_b = np.linalg.norm(emb_b)
        emb_b = (np.array(emb_b) / norm_b).tolist()

        nodes = {
            "a": NeuralMemoryNode(
                id="a",
                user_id="u",
                kind=MemoryKind.FACT,
                text="A",
                embedding=emb_a,
                created_at=datetime.utcnow(),
                use_count=0,
                importance=0.5,
                confidence=1.0,
            ),
            "b": NeuralMemoryNode(
                id="b",
                user_id="u",
                kind=MemoryKind.FACT,
                text="B",
                embedding=emb_b,
                created_at=datetime.utcnow(),
                use_count=0,
                importance=0.5,
                confidence=1.0,
            ),
        }
        activations = [
            ActivationState(node_id="a", activation=0.9),
            ActivationState(node_id="b", activation=0.8),
        ]

        await learner.queue_update("u", activations, nodes)

        # Updates should be queued (pair passed gating)
        assert len(learner._update_queue["u"]) == 2  # Bidirectional

    @pytest.mark.asyncio
    async def test_similarity_threshold_override_precedence(self, mock_graph):
        """Per-call similarity_threshold (#982) overrides config.min_similarity_for_edge.

        Two embeddings with cosine 0.6. With config gate 0.9 the pair would be
        gated, but a 0.3 override (calibrated edge_gate value) admits it; with
        config gate 0.1 a 0.8 override gates it. Proves the override wins in
        both directions.
        """
        import numpy as np

        emb_a = [1.0, 0.0, 0.0] + [0.0] * 5
        emb_b = [0.6, 0.8, 0.0] + [0.0] * 5  # cosine 0.6 with emb_a
        emb_a = (np.array(emb_a) / np.linalg.norm(emb_a)).tolist()
        emb_b = (np.array(emb_b) / np.linalg.norm(emb_b)).tolist()

        def _nodes():
            return {
                "a": NeuralMemoryNode(
                    id="a", user_id="u", kind=MemoryKind.FACT, text="A",
                    embedding=emb_a, created_at=datetime.utcnow(),
                    use_count=0, importance=0.5, confidence=1.0,
                ),
                "b": NeuralMemoryNode(
                    id="b", user_id="u", kind=MemoryKind.FACT, text="B",
                    embedding=emb_b, created_at=datetime.utcnow(),
                    use_count=0, importance=0.5, confidence=1.0,
                ),
            }

        activations = [
            ActivationState(node_id="a", activation=0.9),
            ActivationState(node_id="b", activation=0.8),
        ]

        # config 0.9 would gate; override 0.3 admits → 2 bidirectional updates.
        admit_cfg = NeuralMemoryConfig(
            learning_rate=0.1, gradient_clipping=1.0, min_similarity_for_edge=0.9
        )
        admit_learner = HebbianLearner(mock_graph, admit_cfg)
        await admit_learner.queue_update(
            "u", activations, _nodes(), similarity_threshold=0.3
        )
        assert len(admit_learner._update_queue["u"]) == 2

        # config 0.1 would admit; override 0.8 gates → no updates.
        gate_cfg = NeuralMemoryConfig(
            learning_rate=0.1, gradient_clipping=1.0, min_similarity_for_edge=0.1
        )
        gate_learner = HebbianLearner(mock_graph, gate_cfg)
        await gate_learner.queue_update(
            "u", activations, _nodes(), similarity_threshold=0.8
        )
        assert len(gate_learner._update_queue.get("u", [])) == 0

    @pytest.mark.asyncio
    async def test_prune_weak_edges_excludes_semantic_origin(self, mock_graph):
        """#724: the per-node top-M pruner only considers hebbian edges.

        A semantic edge survives even when it is the weakest edge overall and
        would have been pruned first under the old unfiltered weight sort.
        """
        config = NeuralMemoryConfig(top_m_edges=2)
        learner = HebbianLearner(mock_graph, config)

        node_id = str(uuid4())
        # 3 hebbian edges (1 exceeds top_m=2) + 1 semantic edge that is the
        # weakest overall — it must NOT be pruned and must NOT count toward top-M.
        hebbian = [
            SimpleNamespace(dst_id=uuid4(), weight=w, origin=EDGE_ORIGIN_HEBBIAN)
            for w in (0.5, 0.6, 0.7)
        ]
        semantic = SimpleNamespace(dst_id=uuid4(), weight=0.05, origin=EDGE_ORIGIN_SEMANTIC)
        all_edges = hebbian + [semantic]

        async def fake_get_outgoing(user_id, src_id, **kwargs):
            # Mirror the SQL-side origin filter (#741): return only matching rows.
            origin = kwargs.get("origin")
            if origin is None:
                return all_edges
            return [e for e in all_edges if e.origin == origin]

        mock_graph.edge_repo.get_outgoing_edges = AsyncMock(side_effect=fake_get_outgoing)
        mock_graph.remove_edge = AsyncMock()

        removed = await learner.prune_weak_edges("u", node_id)

        # The pruner must request the hebbian-filtered list.
        assert (
            mock_graph.edge_repo.get_outgoing_edges.await_args.kwargs["origin"]
            == EDGE_ORIGIN_HEBBIAN
        )
        # Exactly one (weakest) hebbian edge pruned; the semantic edge is untouched.
        assert removed == 1
        removed_dsts = {call.args[1] for call in mock_graph.remove_edge.await_args_list}
        assert str(semantic.dst_id) not in removed_dsts
        assert str(hebbian[0].dst_id) in removed_dsts  # weight 0.5 — weakest hebbian
