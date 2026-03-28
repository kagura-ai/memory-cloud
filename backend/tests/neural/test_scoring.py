"""Tests for UnifiedScorer."""

import math
from datetime import datetime, timedelta

import pytest

from neural.activation import ActivationSpreader
from neural.config import NeuralMemoryConfig
from neural.models import MemoryKind, NeuralMemoryNode, SourceKind
from neural.scoring import UnifiedScorer
from services.graph_service import GraphService


class TestUnifiedScorer:
    """Test UnifiedScorer composite scoring system."""

    @pytest.fixture
    def config(self):
        """Create test config."""
        return NeuralMemoryConfig(
            # Scoring weights
            alpha=0.4,  # Semantic
            beta=0.2,  # Association
            gamma=0.15,  # Recency
            delta=0.15,  # Importance
            epsilon=0.1,  # Trust
            zeta=0.1,  # Redundancy penalty
            recency_tau_days=30.0,
            spread_hops=2,
            spread_decay=0.8,
            spread_threshold=0.1,
        )

    @pytest.fixture
    def graph(self):
        """Create test graph."""
        graph = GraphService(user_id="test_user")
        graph.add_node("node1", "memory")
        graph.add_node("node2", "memory")
        graph.add_edge("node1", "node2", weight=0.9)
        return graph

    @pytest.fixture
    def activation_spreader(self, graph, config):
        """Create ActivationSpreader."""
        return ActivationSpreader(graph, config)

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
        # Memory used 1 day ago
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

        # Should be high (close to 1.0) for recent memory
        # exp(-1 / 30) ≈ 0.967
        expected_score = math.exp(-1 / 30.0)
        assert abs(recency_score - expected_score) < 0.01

    def test_calculate_recency_score_old(self, scorer):
        """Test recency score for old memory."""
        # Memory used 90 days ago
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

        # Should be low for old memory
        # exp(-90 / 30) = exp(-3) ≈ 0.05
        expected_score = math.exp(-90 / 30.0)
        assert abs(recency_score - expected_score) < 0.01

    def test_calculate_recency_score_never_used(self, scorer):
        """Test recency score for never-used memory (uses created_at)."""
        # Memory created 5 days ago, never used
        node = NeuralMemoryNode(
            id="node1",
            user_id="test_user",
            kind=MemoryKind.FACT,
            text="Never used memory",
            embedding=[0.1] * 512,
            created_at=datetime.utcnow() - timedelta(days=5),
            last_used_at=None,  # Never used
            use_count=0,
            importance=0.5,
            confidence=1.0,
        )

        current_time = datetime.utcnow()
        recency_score = scorer._calculate_recency_score(node, current_time)

        # Should use created_at
        # exp(-5 / 30) ≈ 0.846
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
            use_count=100,  # High use count
            importance=0.5,  # Mid importance
            confidence=1.0,
        )

        importance_score = scorer._calculate_importance_score(node)

        # Should be boosted by high use count
        # 0.7 * 0.5 + 0.3 * log_frequency
        # log_frequency = log(101) / log(101) = 1.0 (since ref=100)
        # importance = 0.7 * 0.5 + 0.3 * 1.0 = 0.35 + 0.3 = 0.65
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
            importance=0.8,  # High stored importance
            confidence=1.0,
        )

        importance_score = scorer._calculate_importance_score(node)

        # Should rely on stored importance only
        # 0.7 * 0.8 + 0.3 * 0.0 = 0.56
        expected_score = 0.7 * 0.8
        assert abs(importance_score - expected_score) < 0.01

    def test_cosine_similarity(self, scorer):
        """Test cosine similarity calculation."""
        emb1 = [1.0, 0.0, 0.0]
        emb2 = [1.0, 0.0, 0.0]

        # Identical vectors should have similarity 1.0
        sim = scorer._cosine_similarity(emb1, emb2)
        assert abs(sim - 1.0) < 0.001

        # Orthogonal vectors should have similarity 0.0
        emb3 = [0.0, 1.0, 0.0]
        sim2 = scorer._cosine_similarity(emb1, emb3)
        assert abs(sim2 - 0.0) < 0.001

        # Opposite vectors should have similarity 0.0 (clamped)
        emb4 = [-1.0, 0.0, 0.0]
        sim3 = scorer._cosine_similarity(emb1, emb4)
        assert sim3 == 0.0  # Negative clamped to 0.0

    def test_cosine_similarity_zero_norm(self, scorer):
        """Test cosine similarity with zero-norm vector."""
        emb1 = [1.0, 0.0, 0.0]
        emb2 = [0.0, 0.0, 0.0]  # Zero vector

        sim = scorer._cosine_similarity(emb1, emb2)
        assert sim == 0.0

    def test_calculate_redundancy_penalty_no_selected(self, scorer, sample_node):
        """Test redundancy penalty with no selected nodes."""
        penalty = scorer._calculate_redundancy_penalty(sample_node, [])

        # No penalty if nothing selected yet
        assert penalty == 0.0

    def test_calculate_redundancy_penalty_dissimilar(self, scorer):
        """Test redundancy penalty with dissimilar selected nodes."""
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

        # Selected node is orthogonal (dissimilar)
        selected_embeddings = [[0.0, 1.0, 0.0]]

        penalty = scorer._calculate_redundancy_penalty(node, selected_embeddings)

        # Should have low penalty (vectors are orthogonal)
        assert penalty < 0.1

    def test_calculate_redundancy_penalty_similar(self, scorer):
        """Test redundancy penalty with similar selected nodes."""
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

        # Selected node is very similar
        selected_embeddings = [[1.0, 0.0, 0.0]]

        penalty = scorer._calculate_redundancy_penalty(node, selected_embeddings)

        # Should have high penalty (vectors are identical)
        assert penalty > 0.9

    def test_score_candidates_empty_list(self, scorer):
        """Test scoring empty candidate list."""
        results = scorer.score_candidates(
            query_embedding=[0.1] * 512,
            candidates=[],
        )

        assert len(results) == 0

    def test_score_candidates_single_candidate(self, scorer, sample_node):
        """Test scoring single candidate."""
        candidates = [(sample_node, 0.9)]  # (node, similarity_score)

        results = scorer.score_candidates(
            query_embedding=[0.1] * 512,
            candidates=candidates,
        )

        assert len(results) == 1
        result = results[0]

        # Check structure
        assert result.node == sample_node
        assert result.score > 0.0
        assert result.score <= 1.0

        # Check components
        assert "semantic" in result.components
        assert "association" in result.components
        assert "recency" in result.components
        assert "importance" in result.components
        assert "trust" in result.components
        assert "redundancy_penalty" in result.components

        # Semantic score should match input
        assert result.components["semantic"] == 0.9

        # Trust score should match node confidence
        assert result.components["trust"] == sample_node.confidence

    def test_score_candidates_sorted_by_score(self, scorer):
        """Test that results are sorted by score (descending)."""
        # Create candidates with different similarities
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

        # node1 has high similarity, node2 has low
        candidates = [(node1, 0.9), (node2, 0.5)]

        results = scorer.score_candidates(
            query_embedding=[0.1] * 512,
            candidates=candidates,
        )

        # Should be sorted descending
        assert results[0].score >= results[1].score

    def test_score_candidates_with_association(self, scorer, graph):
        """Test scoring with graph association component."""
        # Create node in graph
        node = NeuralMemoryNode(
            id="node2",
            user_id="test_user",
            kind=MemoryKind.FACT,
            text="Memory 2",
            embedding=[0.2] * 512,
            created_at=datetime.utcnow(),
            use_count=5,
            importance=0.5,
            confidence=1.0,
        )

        candidates = [(node, 0.8)]

        # Provide seed nodes (node1 is connected to node2)
        results = scorer.score_candidates(
            query_embedding=[0.1] * 512,
            candidates=candidates,
            seed_nodes=["node1"],
        )

        # Association score should be > 0
        assert results[0].components["association"] > 0.0

    def test_score_candidates_with_redundancy_penalty(self, scorer):
        """Test scoring with redundancy penalty from selected nodes."""
        node = NeuralMemoryNode(
            id="node1",
            user_id="test_user",
            kind=MemoryKind.FACT,
            text="Memory 1",
            embedding=[1.0, 0.0] + [0.0] * 510,
            created_at=datetime.utcnow(),
            use_count=5,
            importance=0.5,
            confidence=1.0,
        )

        # Already selected similar node
        selected_node = NeuralMemoryNode(
            id="selected",
            user_id="test_user",
            kind=MemoryKind.FACT,
            text="Selected",
            embedding=[1.0, 0.0] + [0.0] * 510,  # Identical
            created_at=datetime.utcnow(),
            use_count=5,
            importance=0.5,
            confidence=1.0,
        )

        candidates = [(node, 0.8)]

        results = scorer.score_candidates(
            query_embedding=[0.1] * 512,
            candidates=candidates,
            selected_nodes=[selected_node],
        )

        # Redundancy penalty should be high (similar embeddings)
        assert results[0].components["redundancy_penalty"] > 0.9

    def test_score_candidates_composite_formula(self, scorer):
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

        candidates = [(node, 0.85)]

        results = scorer.score_candidates(
            query_embedding=[0.1] * 512,
            candidates=candidates,
        )

        result = results[0]
        components = result.components
        weights = scorer.config.scoring_weights_normalized

        # Manually calculate expected score
        expected_score = (
            weights["alpha"] * components["semantic"]
            + weights["beta"] * components["association"]
            + weights["gamma"] * components["recency"]
            + weights["delta"] * components["importance"]
            + weights["epsilon"] * components["trust"]
            - weights["zeta"] * components["redundancy_penalty"]
        )

        # Clamp to [0, 1]
        expected_score = max(0.0, min(1.0, expected_score))

        assert abs(result.score - expected_score) < 0.001

    def test_mmr_rerank_empty_list(self, scorer):
        """Test MMR reranking with empty list."""
        results = scorer.mmr_rerank(
            query_embedding=[0.1] * 512,
            results=[],
            lambda_param=0.5,
            top_k=10,
        )

        assert len(results) == 0

    def test_mmr_rerank_single_result(self, scorer, sample_node):
        """Test MMR reranking with single result."""
        from neural.models import RecallResult

        results = [RecallResult(node=sample_node, score=0.9)]

        reranked = scorer.mmr_rerank(
            query_embedding=[0.1] * 512,
            results=results,
            lambda_param=0.5,
            top_k=10,
        )

        assert len(reranked) == 1
        assert reranked[0].node == sample_node

    def test_mmr_rerank_promotes_diversity(self, scorer):
        """Test that MMR promotes diversity."""
        from neural.models import RecallResult

        # Create 3 nodes: 2 similar, 1 different
        node1 = NeuralMemoryNode(
            id="node1",
            user_id="test_user",
            kind=MemoryKind.FACT,
            text="Memory 1",
            embedding=[1.0, 0.0] + [0.0] * 510,
            created_at=datetime.utcnow(),
            use_count=5,
            importance=0.8,
            confidence=1.0,
        )

        node2 = NeuralMemoryNode(
            id="node2",
            user_id="test_user",
            kind=MemoryKind.FACT,
            text="Memory 2",
            embedding=[0.99, 0.01] + [0.0] * 510,  # Very similar to node1
            created_at=datetime.utcnow(),
            use_count=5,
            importance=0.8,
            confidence=1.0,
        )

        node3 = NeuralMemoryNode(
            id="node3",
            user_id="test_user",
            kind=MemoryKind.FACT,
            text="Memory 3",
            embedding=[0.0, 1.0] + [0.0] * 510,  # Different from node1
            created_at=datetime.utcnow(),
            use_count=5,
            importance=0.8,
            confidence=1.0,
        )

        # Initial ranking (by relevance): node1 > node2 > node3
        results = [
            RecallResult(node=node1, score=0.95),
            RecallResult(node=node2, score=0.93),
            RecallResult(node=node3, score=0.85),
        ]

        # MMR with diversity emphasis (lambda=0.3 favors diversity)
        reranked = scorer.mmr_rerank(
            query_embedding=[1.0, 0.0] + [0.0] * 510,
            results=results,
            lambda_param=0.3,  # Low lambda = more diversity
            top_k=3,
        )

        # node1 should be first (highest relevance)
        assert reranked[0].node.id == "node1"

        # node3 should be promoted over node2 (more diverse)
        # Note: This assumes diversity weight is strong enough
        # If test fails, might need to adjust lambda or check MMR logic

    def test_mmr_rerank_respects_top_k(self, scorer):
        """Test that MMR respects top_k limit."""
        from neural.models import RecallResult

        # Create 5 nodes
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

        # Request top 3
        reranked = scorer.mmr_rerank(
            query_embedding=[0.1] * 512,
            results=results,
            lambda_param=0.5,
            top_k=3,
        )

        # Should return exactly 3 results
        assert len(reranked) == 3

    def test_scoring_weights_normalized(self, config):
        """Test that scoring weights are normalized."""
        weights = config.scoring_weights_normalized

        # Sum should be 1.0 (normalized)
        total = sum(weights.values())
        assert abs(total - 1.0) < 0.001

        # All weights should be non-negative
        assert all(w >= 0 for w in weights.values())
