"""Tests for Sleep Maintenance Phase 2: Dedup/Merge.

Issue #101: Union-Find clustering, LLM judgment, merge execution.
"""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services.sleep.dedup_merge import (
    AUTO_MERGE_THRESHOLD,
    MAX_CLUSTER_SIZE,
    DedupMergePhase,
    UnionFind,
)
from services.sleep.reporter import SleepBudget

# ============================================================================
# UnionFind Tests
# ============================================================================


class TestUnionFind:
    """Test Union-Find correctness (critical for dedup safety)."""

    def test_single_pair(self):
        uf = UnionFind()
        a, b = uuid4(), uuid4()
        uf.union(a, b)
        clusters = uf.clusters()
        assert len(clusters) == 1
        assert clusters[0] == {a, b}

    def test_transitive_clustering(self):
        """A~B + B~C should cluster all three (Union-Find transitivity)."""
        uf = UnionFind()
        a, b, c = uuid4(), uuid4(), uuid4()
        uf.union(a, b)
        uf.union(b, c)
        clusters = uf.clusters()
        assert len(clusters) == 1
        assert clusters[0] == {a, b, c}

    def test_separate_clusters(self):
        uf = UnionFind()
        a, b, c, d = uuid4(), uuid4(), uuid4(), uuid4()
        uf.union(a, b)
        uf.union(c, d)
        clusters = uf.clusters()
        assert len(clusters) == 2
        cluster_sets = [frozenset(c) for c in clusters]
        assert frozenset({a, b}) in cluster_sets
        assert frozenset({c, d}) in cluster_sets

    def test_idempotent_union(self):
        """Unioning same pair multiple times is safe."""
        uf = UnionFind()
        a, b = uuid4(), uuid4()
        uf.union(a, b)
        uf.union(a, b)
        uf.union(b, a)
        clusters = uf.clusters()
        assert len(clusters) == 1

    def test_chain_of_five(self):
        """Chain A-B-C-D-E produces single cluster."""
        uf = UnionFind()
        ids = [uuid4() for _ in range(5)]
        for i in range(4):
            uf.union(ids[i], ids[i + 1])
        clusters = uf.clusters()
        assert len(clusters) == 1
        assert len(clusters[0]) == 5

    def test_empty(self):
        uf = UnionFind()
        assert uf.clusters() == []


# ============================================================================
# DedupMergePhase Tests
# ============================================================================


@pytest.fixture
def mock_db():
    return AsyncMock()


@pytest.fixture
def mock_llm():
    return AsyncMock()


@pytest.fixture
def dedup_phase(mock_db, mock_llm):
    with (
        patch("services.sleep.dedup_merge.NeuralEdgeRepository"),
        patch("services.sleep.dedup_merge.EmbeddingService"),
    ):
        phase = DedupMergePhase(mock_db, mock_llm)
        phase.edge_repo = AsyncMock()
        phase.embedding_service = AsyncMock()
    return phase


def _make_config(dedup_enabled=True, threshold=0.92, provider="openai", model="gpt-5-nano"):
    config = MagicMock()
    config.sleep_dedup_enabled = dedup_enabled
    config.sleep_dedup_similarity_threshold = threshold
    config.sleep_llm_provider = provider
    config.sleep_llm_model = model
    return config


def _make_memory(memory_id=None, summary="test", importance=0.5, tags=None):
    m = MagicMock()
    m.id = memory_id or uuid4()
    m.summary = summary
    m.type = "note"
    m.importance = importance
    m.tags = tags or []
    m.access_count = 1
    return m


class TestDedupMergePhase:
    """Test DedupMergePhase execution."""

    @pytest.mark.asyncio
    async def test_disabled_returns_skipped(self, dedup_phase):
        config = _make_config(dedup_enabled=False)
        budget = SleepBudget()

        result = await dedup_phase.execute(config, "user-1", "ws-1", "ctx-1", budget)

        assert result.skipped is True
        assert result.skip_reason == "dedup_disabled"

    @pytest.mark.asyncio
    async def test_too_few_memories(self, dedup_phase):
        config = _make_config()
        budget = SleepBudget()

        # Mock _fetch_active_memories directly
        dedup_phase._fetch_active_memories = AsyncMock(return_value=[_make_memory()])

        result = await dedup_phase.execute(config, "user-1", "ws-1", "ctx-1", budget)

        assert result.success is True
        assert result.details["message"] == "not_enough_memories"

    @pytest.mark.asyncio
    async def test_no_similar_pairs(self, dedup_phase):
        """No pairs above threshold → no work."""
        config = _make_config()
        budget = SleepBudget()

        mems = [_make_memory() for _ in range(3)]
        dedup_phase._fetch_active_memories = AsyncMock(return_value=mems)
        dedup_phase._find_similar_pairs = AsyncMock(return_value=[])

        result = await dedup_phase.execute(config, "user-1", "ws-1", "ctx-1", budget)

        assert result.details["message"] == "no_duplicate_candidates"


class TestRuleBasedJudge:
    """Test rule-based (LLM-off) dedup logic."""

    def test_high_similarity_auto_merge(self):
        """Pairs above AUTO_MERGE_THRESHOLD are auto-merged."""
        phase = DedupMergePhase.__new__(DedupMergePhase)

        mem_a = _make_memory(importance=0.8)
        mem_b = _make_memory(importance=0.5)
        pair_scores = {tuple(sorted([mem_a.id, mem_b.id], key=str)): 0.99}

        decisions = phase._rule_based_judge([mem_a, mem_b], pair_scores)

        assert len(decisions) == 1
        # Higher importance wins
        assert decisions[0][0] == mem_a.id
        assert decisions[0][1] == mem_b.id

    def test_below_threshold_no_merge(self):
        """Pairs below AUTO_MERGE_THRESHOLD are not merged."""
        phase = DedupMergePhase.__new__(DedupMergePhase)

        mem_a = _make_memory()
        mem_b = _make_memory()
        pair_scores = {tuple(sorted([mem_a.id, mem_b.id], key=str)): 0.95}

        decisions = phase._rule_based_judge([mem_a, mem_b], pair_scores)

        assert len(decisions) == 0


class TestParseDedupResponse:
    """Test LLM response parsing with ID validation."""

    def test_valid_merge_response(self):
        phase = DedupMergePhase.__new__(DedupMergePhase)

        id_a, id_b = uuid4(), uuid4()
        label_to_id = {"A": id_a, "B": id_b}

        response = {
            "judgments": [
                {
                    "pair": ["A", "B"],
                    "verdict": "merge",
                    "winner": "A",
                    "confidence": 0.95,
                    "reason": "B is subset of A",
                }
            ]
        }

        decisions = phase._parse_dedup_response(response, label_to_id)
        assert len(decisions) == 1
        assert decisions[0] == (id_a, id_b)

    def test_keep_both_response(self):
        phase = DedupMergePhase.__new__(DedupMergePhase)

        label_to_id = {"A": uuid4(), "B": uuid4()}

        response = {
            "judgments": [
                {
                    "pair": ["A", "B"],
                    "verdict": "keep_both",
                    "winner": None,
                    "confidence": 0.8,
                    "reason": "different information",
                }
            ]
        }

        decisions = phase._parse_dedup_response(response, label_to_id)
        assert len(decisions) == 0

    def test_invalid_label_ignored(self):
        """Labels not in label_to_id are safely ignored (hallucination protection)."""
        phase = DedupMergePhase.__new__(DedupMergePhase)

        label_to_id = {"A": uuid4(), "B": uuid4()}

        response = {
            "judgments": [
                {
                    "pair": ["A", "Z"],  # Z doesn't exist
                    "verdict": "merge",
                    "winner": "A",
                    "confidence": 0.9,
                    "reason": "hallucinated",
                }
            ]
        }

        decisions = phase._parse_dedup_response(response, label_to_id)
        assert len(decisions) == 0

    def test_empty_response(self):
        phase = DedupMergePhase.__new__(DedupMergePhase)
        decisions = phase._parse_dedup_response({}, {"A": uuid4()})
        assert len(decisions) == 0


class TestClusterSizeCap:
    """Test that oversized clusters are deferred."""

    def test_max_cluster_size_constant(self):
        """Verify the safety cap value."""
        assert MAX_CLUSTER_SIZE == 5

    def test_auto_merge_threshold_constant(self):
        """Verify the auto-merge threshold."""
        assert AUTO_MERGE_THRESHOLD == 0.98
