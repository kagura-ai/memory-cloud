"""Tests for UnifiedScorer."""

import math
from datetime import datetime, timedelta

import pytest

from neural.activation import ActivationSpreader
from neural.config import NeuralMemoryConfig
from neural.models import MemoryKind, NeuralMemoryNode, SourceKind
from neural.scoring import UnifiedScorer


class TestUnifiedScorer:
    """Test UnifiedScorer composite scoring system."""

    @pytest.fixture
    def config(self):
        """Create test config."""
        return NeuralMemoryConfig(
            alpha=0.4,
            beta=0.2,
            gamma=0.15,
            delta=0.15,
            epsilon=0.1,
            zeta=0.1,
            recency_tau_days=30.0,
            spread_hops=2,
            spread_decay=0.8,
            spread_threshold=0.1,
        )

    # mock_graph from neural/conftest.py

    @pytest.fixture
    def activation_spreader(self, mock_graph, config):
        """Create ActivationSpreader with mock graph."""
        return ActivationSpreader(mock_graph, config)

    @pytest.fixture
    def scorer(self, config, activation_spreader):
        """Create UnifiedScorer."""
        return UnifiedScorer(config, activation_spreader)

    @pytest.fixture
    def sample_node(self):
        """Create sample memory node."""
        return NeuralMemoryNode(
            id="node1",
            user_id="test_user",
            kind=MemoryKind.FACT,
            text="Sample memory",
            embedding=[0.1] * 512,
            created_at=datetime.utcnow() - timedelta(days=10),
            last_used_at=datetime.utcnow() - timedelta(days=1),
            use_count=5,
            importance=0.8,
            confidence=1.0,
            source=SourceKind.USER,
        )

    def test_init(self, config, activation_spreader):
        """Test UnifiedScorer initialization."""
        scorer = UnifiedScorer(config, activation_spreader)
        assert scorer.config == config
        assert scorer.activation_spreader == activation_spreader

    def test_calculate_recency_score_recent(self, scorer):
        """Test recency score for recently used memory."""
        node = NeuralMemoryNode(
            id="node1",
            user_id="test_user",
            kind=MemoryKind.FACT,
            text="Recent memory",
            embedding=[0.1] * 512,
            created_at=datetime.utcnow() - timedelta(days=10),
            last_used_at=datetime.utcnow() - timedelta(days=1),
            use_count=5,
            importance=0.8,
            confidence=1.0,
        )

        current_time = datetime.utcnow()
        recency_score = scorer._calculate_recency_score(node, current_time)
        expected_score = math.exp(-1 / 30.0)
        assert abs(recency_score - expected_score) < 0.01

    def test_calculate_recency_score_old(self, scorer):
        """Test recency score for old memory."""
        node = NeuralMemoryNode(
            id="node1",
            user_id="test_user",
            kind=MemoryKind.FACT,
            text="Old memory",
            embedding=[0.1] * 512,
            created_at=datetime.utcnow() - timedelta(days=100),
            last_used_at=datetime.utcnow() - timedelta(days=90),
            use_count=5,
            importance=0.8,
            confidence=1.0,
        )

        current_time = datetime.utcnow()
        recency_score = scorer._calculate_recency_score(node, current_time)
        expected_score = math.exp(-90 / 30.0)
        assert abs(recency_score - expected_score) < 0.01

    def test_calculate_recency_score_never_used(self, scorer):
        """Test recency score for never-used memory."""
        node = NeuralMemoryNode(
            id="node1",
            user_id="test_user",
            kind=MemoryKind.FACT,
            text="Never used memory",
            embedding=[0.1] * 512,
            created_at=datetime.utcnow() - timedelta(days=5),
            last_used_at=None,
            use_count=0,
            importance=0.5,
            confidence=1.0,
        )

        current_time = datetime.utcnow()
        recency_score = scorer._calculate_recency_score(node, current_time)
        expected_score = math.exp(-5 / 30.0)
        assert abs(recency_score - expected_score) < 0.01

    def test_calculate_importance_score_high_use_count(self, scorer):
        """Test importance score with high use count."""
        node = NeuralMemoryNode(
            id="node1",
            user_id="test_user",
            kind=MemoryKind.FACT,
            text="Frequently used memory",
            embedding=[0.1] * 512,
            created_at=datetime.utcnow(),
            use_count=100,
            importance=0.5,
            confidence=1.0,
        )

        importance_score = scorer._calculate_importance_score(node)
        assert importance_score > 0.6

    def test_calculate_importance_score_no_use(self, scorer):
        """Test importance score with no use count."""
        node = NeuralMemoryNode(
            id="node1",
            user_id="test_user",
            kind=MemoryKind.FACT,
            text="Unused memory",
            embedding=[0.1] * 512,
            created_at=datetime.utcnow(),
            use_count=0,
            importance=0.8,
            confidence=1.0,
        )

        importance_score = scorer._calculate_importance_score(node)
        expected_score = 0.7 * 0.8
        assert abs(importance_score - expected_score) < 0.01

    def test_cosine_similarity(self, scorer):
        """Test cosine similarity calculation."""
        emb1 = [1.0, 0.0, 0.0]
        emb2 = [1.0, 0.0, 0.0]
        assert abs(scorer._cosine_similarity(emb1, emb2) - 1.0) < 0.001

        emb3 = [0.0, 1.0, 0.0]
        assert abs(scorer._cosine_similarity(emb1, emb3) - 0.0) < 0.001

        emb4 = [-1.0, 0.0, 0.0]
        assert scorer._cosine_similarity(emb1, emb4) == 0.0

    def test_cosine_similarity_zero_norm(self, scorer):
        """Test cosine similarity with zero-norm vector."""
        assert scorer._cosine_similarity([1.0, 0.0], [0.0, 0.0]) == 0.0

    def test_calculate_redundancy_penalty_no_selected(self, scorer, sample_node):
        """Test redundancy penalty with no selected nodes."""
        assert scorer._calculate_redundancy_penalty(sample_node, []) == 0.0

    def test_calculate_redundancy_penalty_similar(self, scorer):
        """Test redundancy penalty with similar selected nodes."""
        node = NeuralMemoryNode(
            id="node1",
            user_id="test_user",
            kind=MemoryKind.FACT,
            text="Memory 1",
            embedding=[1.0, 0.0] + [0.0] * 510,
            created_at=datetime.utcnow(),
            use_count=0,
            importance=0.5,
            confidence=1.0,
        )
        penalty = scorer._calculate_redundancy_penalty(node, [[1.0, 0.0] + [0.0] * 510])
        assert penalty > 0.9

    def test_calculate_redundancy_penalty_dissimilar(self, scorer):
        """Test redundancy penalty with dissimilar nodes."""
        node = NeuralMemoryNode(
            id="node1",
            user_id="test_user",
            kind=MemoryKind.FACT,
            text="Memory 1",
            embedding=[1.0, 0.0, 0.0],
            created_at=datetime.utcnow(),
            use_count=0,
            importance=0.5,
            confidence=1.0,
        )
        penalty = scorer._calculate_redundancy_penalty(node, [[0.0, 1.0, 0.0]])
        assert penalty < 0.1

    @pytest.mark.asyncio
    async def test_score_candidates_empty_list(self, scorer):
        """Test scoring empty candidate list."""
        results = await scorer.score_candidates(
            query_embedding=[0.1] * 512,
            candidates=[],
        )
        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_score_candidates_single_candidate(self, scorer, sample_node):
        """Test scoring single candidate."""
        candidates = [(sample_node, 0.9)]

        results = await scorer.score_candidates(
            query_embedding=[0.1] * 512,
            candidates=candidates,
        )

        assert len(results) == 1
        result = results[0]

        assert result.node == sample_node
        assert 0.0 <= result.score <= 1.0
        assert "semantic" in result.components
        assert "association" in result.components
        assert "recency" in result.components
        assert "importance" in result.components
        assert "trust" in result.components
        assert result.components["semantic"] == 0.9
        assert result.components["trust"] == sample_node.confidence

    @pytest.mark.asyncio
    async def test_score_candidates_sorted_by_score(self, scorer):
        """Test that results are sorted by score (descending)."""
        node1 = NeuralMemoryNode(
            id="node1",
            user_id="test_user",
            kind=MemoryKind.FACT,
            text="Memory 1",
            embedding=[0.1] * 512,
            created_at=datetime.utcnow(),
            use_count=10,
            importance=0.9,
            confidence=1.0,
        )
        node2 = NeuralMemoryNode(
            id="node2",
            user_id="test_user",
            kind=MemoryKind.FACT,
            text="Memory 2",
            embedding=[0.2] * 512,
            created_at=datetime.utcnow(),
            use_count=2,
            importance=0.3,
            confidence=1.0,
        )

        candidates = [(node1, 0.9), (node2, 0.5)]
        results = await scorer.score_candidates(
            query_embedding=[0.1] * 512,
            candidates=candidates,
        )
        assert results[0].score >= results[1].score

    @pytest.mark.asyncio
    async def test_score_candidates_composite_formula(self, scorer):
        """Test that composite score follows the formula."""
        node = NeuralMemoryNode(
            id="node1",
            user_id="test_user",
            kind=MemoryKind.FACT,
            text="Memory",
            embedding=[0.1] * 512,
            created_at=datetime.utcnow(),
            use_count=5,
            importance=0.8,
            confidence=0.9,
        )

        results = await scorer.score_candidates(
            query_embedding=[0.1] * 512,
            candidates=[(node, 0.85)],
        )

        result = results[0]
        components = result.components
        weights = scorer.config.scoring_weights_normalized

        expected_score = (
            weights["alpha"] * components["semantic"]
            + weights["beta"] * components["association"]
            + weights["gamma"] * components["recency"]
            + weights["delta"] * components["importance"]
            + weights["epsilon"] * components["trust"]
            - weights["zeta"] * components["redundancy_penalty"]
        )
        expected_score = max(0.0, min(1.0, expected_score))
        assert abs(result.score - expected_score) < 0.001

    def test_scoring_weights_normalized(self, config):
        """Test that scoring weights are normalized (excluding zeta penalty).

        zeta is a redundancy penalty, not a scoring weight,
        so it is excluded from normalization.
        """
        weights = config.scoring_weights_normalized
        scoring_sum = (
            weights["alpha"]
            + weights["beta"]
            + weights["gamma"]
            + weights["delta"]
            + weights["epsilon"]
        )
        assert abs(scoring_sum - 1.0) < 0.001
        assert all(w >= 0 for w in weights.values())

    def test_mmr_rerank_empty_list(self, scorer):
        """Test MMR reranking with empty list."""
        results = scorer.mmr_rerank(
            query_embedding=[0.1] * 512, results=[], lambda_param=0.5, top_k=10
        )
        assert len(results) == 0

    def test_mmr_rerank_single_result(self, scorer, sample_node):
        """Test MMR reranking with single result."""
        from neural.models import RecallResult

        results = [RecallResult(node=sample_node, score=0.9)]
        reranked = scorer.mmr_rerank(
            query_embedding=[0.1] * 512, results=results, lambda_param=0.5, top_k=10
        )
        assert len(reranked) == 1

    def test_mmr_rerank_respects_top_k(self, scorer):
        """Test that MMR respects top_k limit."""
        from neural.models import RecallResult

        results = []
        for i in range(5):
            node = NeuralMemoryNode(
                id=f"node{i}",
                user_id="test_user",
                kind=MemoryKind.FACT,
                text=f"Memory {i}",
                embedding=[float(i) / 5] * 512,
                created_at=datetime.utcnow(),
                use_count=5,
                importance=0.5,
                confidence=1.0,
            )
            results.append(RecallResult(node=node, score=0.9 - i * 0.1))

        reranked = scorer.mmr_rerank(
            query_embedding=[0.1] * 512, results=results, lambda_param=0.5, top_k=3
        )
        assert len(reranked) == 3
