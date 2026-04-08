"""Tests for Sleep Maintenance Phase 1: Edge Discovery.

Issue #103: Recency-weighted sampling, LLM edge proposals.
"""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services.sleep.edge_discovery import (
    BATCH_SIZE,
    DISCOVERY_EDGE_WEIGHT,
    SEMANTIC_SIMILARITY_SYNTHETIC_WEIGHT_THRESHOLD,
    SIMILARITY_MAX,
    SIMILARITY_MIN,
    EdgeDiscoveryPhase,
    _is_synthetic_seed_edge,
)
from services.sleep.reporter import SleepBudget


@pytest.fixture
def mock_db():
    return AsyncMock()


@pytest.fixture
def mock_llm():
    return AsyncMock()


@pytest.fixture
def edge_phase(mock_db, mock_llm):
    with (
        patch("services.sleep.edge_discovery.NeuralEdgeRepository"),
        patch("services.sleep.edge_discovery.EmbeddingService"),
    ):
        phase = EdgeDiscoveryPhase(mock_db, mock_llm)
        phase.edge_repo = AsyncMock()
        phase.embedding_service = AsyncMock()
    return phase


def _make_config(enabled=True, sample_size=10, provider="openai", model="gpt-5-nano"):
    config = MagicMock()
    config.sleep_edge_discovery_enabled = enabled
    config.sleep_edge_discovery_sample_size = sample_size
    config.sleep_llm_provider = provider
    config.sleep_llm_model = model
    return config


def _make_memory(memory_id=None, summary="test", importance=0.5):
    m = MagicMock()
    m.id = memory_id or uuid4()
    m.summary = summary
    m.type = "note"
    m.importance = importance
    m.tags = []
    return m


def _make_edge(edge_type, weight, dst_id=None):
    """Build a minimal edge double with only the attributes the filter reads."""
    e = MagicMock()
    e.edge_type = edge_type
    e.weight = weight
    e.dst_id = dst_id or uuid4()
    return e


class TestEdgeDiscoveryPhase:
    """Test EdgeDiscoveryPhase execution."""

    @pytest.mark.asyncio
    async def test_disabled_returns_skipped(self, edge_phase):
        config = _make_config(enabled=False)
        budget = SleepBudget()

        result = await edge_phase.execute(config, "user-1", "ws-1", "ctx-1", budget)

        assert result.skipped is True
        assert result.skip_reason == "edge_discovery_disabled"

    @pytest.mark.asyncio
    async def test_no_memories(self, edge_phase):
        config = _make_config()
        budget = SleepBudget()

        edge_phase._sample_memories = AsyncMock(return_value=[])

        result = await edge_phase.execute(config, "user-1", "ws-1", "ctx-1", budget)

        assert result.details["message"] == "no_memories_to_sample"

    @pytest.mark.asyncio
    async def test_no_candidates(self, edge_phase):
        config = _make_config()
        budget = SleepBudget()

        mems = [_make_memory() for _ in range(3)]
        edge_phase._sample_memories = AsyncMock(return_value=mems)
        edge_phase._find_candidates = AsyncMock(return_value=[])

        result = await edge_phase.execute(config, "user-1", "ws-1", "ctx-1", budget)

        assert result.details["message"] == "no_edge_candidates"

    @pytest.mark.asyncio
    async def test_all_already_connected(self, edge_phase):
        config = _make_config()
        budget = SleepBudget()

        mems = [_make_memory() for _ in range(3)]
        candidates = [(mems[0].id, mems[1].id, 0.75)]

        edge_phase._sample_memories = AsyncMock(return_value=mems)
        edge_phase._find_candidates = AsyncMock(return_value=candidates)
        edge_phase._filter_existing_edges = AsyncMock(return_value=[])

        result = await edge_phase.execute(config, "user-1", "ws-1", "ctx-1", budget)

        assert result.details["message"] == "all_candidates_already_connected"

    @pytest.mark.asyncio
    async def test_llm_off_accepts_all(self, edge_phase):
        """Without LLM, all candidates are accepted with default edge_type."""
        config = _make_config(provider="")  # LLM disabled
        budget = SleepBudget()

        mem_a = _make_memory()
        mem_b = _make_memory()
        candidates = [(mem_a.id, mem_b.id, 0.75)]

        edge_phase._sample_memories = AsyncMock(return_value=[mem_a, mem_b])
        edge_phase._find_candidates = AsyncMock(return_value=candidates)
        edge_phase._filter_existing_edges = AsyncMock(return_value=candidates)
        edge_phase.edge_repo.create_or_update_edge = AsyncMock()

        result = await edge_phase.execute(config, "user-1", "ws-1", "ctx-1", budget)

        assert result.details["edges_created"] == 1
        edge_phase.edge_repo.create_or_update_edge.assert_called_once()
        call_kwargs = edge_phase.edge_repo.create_or_update_edge.call_args[1]
        assert call_kwargs["weight"] == DISCOVERY_EDGE_WEIGHT
        assert call_kwargs["edge_type"] == "related_to"

    @pytest.mark.asyncio
    async def test_budget_limits_processing(self, edge_phase):
        """Budget exhaustion stops processing."""
        config = _make_config(provider="openai")
        budget = SleepBudget(max_llm_calls=0)  # Already exhausted

        mem_a = _make_memory()
        mem_b = _make_memory()
        candidates = [(mem_a.id, mem_b.id, 0.75)]

        edge_phase._sample_memories = AsyncMock(return_value=[mem_a, mem_b])
        edge_phase._find_candidates = AsyncMock(return_value=candidates)
        edge_phase._filter_existing_edges = AsyncMock(return_value=candidates)

        result = await edge_phase.execute(config, "user-1", "ws-1", "ctx-1", budget)

        assert result.details["edges_created"] == 0


class TestConstants:
    """Verify edge discovery constants."""

    def test_similarity_range(self):
        assert SIMILARITY_MIN == 0.6
        assert SIMILARITY_MAX == 0.9
        assert SIMILARITY_MIN < SIMILARITY_MAX

    def test_discovery_weight(self):
        assert DISCOVERY_EDGE_WEIGHT == 0.5

    def test_batch_size(self):
        assert BATCH_SIZE == 5

    def test_synthetic_threshold(self):
        """Issue #248: pins the threshold at 0.5. Must sit comfortably above
        the default knn_seed_weight (0.3) so cold-start seeds do not block
        Sleep Edge Discovery."""
        assert SEMANTIC_SIMILARITY_SYNTHETIC_WEIGHT_THRESHOLD == 0.5


class TestIsSyntheticSeedEdge:
    """Unit tests for the synthetic-seed edge classifier (Issue #248)."""

    def test_knn_seed_default_weight_is_synthetic(self):
        """Default knn_seed_weight=0.3 must be classified as synthetic —
        this is the in-production value that #248 was triggered by."""
        edge = _make_edge("semantic_similarity", 0.3)
        assert _is_synthetic_seed_edge(edge) is True

    def test_high_weight_semantic_similarity_is_not_synthetic(self):
        edge = _make_edge("semantic_similarity", 0.8)
        assert _is_synthetic_seed_edge(edge) is False

    def test_at_threshold_is_not_synthetic(self):
        """Threshold is strict (< 0.5); exactly 0.5 is treated as real."""
        edge = _make_edge("semantic_similarity", SEMANTIC_SIMILARITY_SYNTHETIC_WEIGHT_THRESHOLD)
        assert _is_synthetic_seed_edge(edge) is False

    def test_related_to_is_never_synthetic(self):
        edge = _make_edge("related_to", 0.1)
        assert _is_synthetic_seed_edge(edge) is False

    def test_neural_association_is_never_synthetic(self):
        """Hebbian co-activation edges are real even at low weight."""
        edge = _make_edge("neural_association", 0.05)
        assert _is_synthetic_seed_edge(edge) is False

    def test_depends_on_is_never_synthetic(self):
        edge = _make_edge("depends_on", 0.1)
        assert _is_synthetic_seed_edge(edge) is False

    def test_learned_from_is_never_synthetic(self):
        edge = _make_edge("learned_from", 0.1)
        assert _is_synthetic_seed_edge(edge) is False


class TestFilterExistingEdges:
    """_filter_existing_edges is now edge_type-aware (Issue #248).

    Background: k-NN cold-start seeding (#224/#238) births every new memory
    with low-weight `semantic_similarity` edges to its 0.4-0.9 neighbors.
    Before this fix, those synthetic edges caused edge discovery to filter
    out nearly every candidate before reaching the LLM judge, yielding 0
    edges created per sleep run in production.
    """

    @pytest.mark.asyncio
    async def test_low_weight_semantic_similarity_does_not_block(self, edge_phase):
        """A pair with only a k-NN seed edge is re-judged by discovery."""
        src = uuid4()
        dst = uuid4()
        seed_edge = _make_edge("semantic_similarity", 0.3, dst_id=dst)
        edge_phase.edge_repo.get_outgoing_edges = AsyncMock(return_value=[seed_edge])

        filtered = await edge_phase._filter_existing_edges(
            [(src, dst, 0.75)], "user-1", "ws-1", "ctx-1"
        )

        assert filtered == [(src, dst, 0.75)]

    @pytest.mark.asyncio
    async def test_high_weight_semantic_similarity_blocks(self, edge_phase):
        """A strong semantic_similarity edge is treated as a real connection."""
        src = uuid4()
        dst = uuid4()
        strong_edge = _make_edge("semantic_similarity", 0.8, dst_id=dst)
        edge_phase.edge_repo.get_outgoing_edges = AsyncMock(return_value=[strong_edge])

        filtered = await edge_phase._filter_existing_edges(
            [(src, dst, 0.75)], "user-1", "ws-1", "ctx-1"
        )

        assert filtered == []

    @pytest.mark.asyncio
    async def test_related_to_blocks_regardless_of_weight(self, edge_phase):
        """Meaningful edge types always block, even at low weight."""
        src = uuid4()
        dst = uuid4()
        real_edge = _make_edge("related_to", 0.1, dst_id=dst)
        edge_phase.edge_repo.get_outgoing_edges = AsyncMock(return_value=[real_edge])

        filtered = await edge_phase._filter_existing_edges(
            [(src, dst, 0.75)], "user-1", "ws-1", "ctx-1"
        )

        assert filtered == []

    @pytest.mark.asyncio
    async def test_neural_association_blocks(self, edge_phase):
        """Hebbian co-activation edges always block."""
        src = uuid4()
        dst = uuid4()
        hebbian = _make_edge("neural_association", 0.2, dst_id=dst)
        edge_phase.edge_repo.get_outgoing_edges = AsyncMock(return_value=[hebbian])

        filtered = await edge_phase._filter_existing_edges(
            [(src, dst, 0.75)], "user-1", "ws-1", "ctx-1"
        )

        assert filtered == []

    @pytest.mark.asyncio
    async def test_no_existing_edges_passes_through(self, edge_phase):
        """Pairs with no existing edges are always kept."""
        src = uuid4()
        dst = uuid4()
        edge_phase.edge_repo.get_outgoing_edges = AsyncMock(return_value=[])

        filtered = await edge_phase._filter_existing_edges(
            [(src, dst, 0.75)], "user-1", "ws-1", "ctx-1"
        )

        assert filtered == [(src, dst, 0.75)]

    @pytest.mark.asyncio
    async def test_seed_edge_to_other_neighbor_does_not_block_candidate(self, edge_phase):
        """A seed edge to a *different* neighbor must not leak and block
        the current candidate (regression guard on set-building logic)."""
        src = uuid4()
        dst = uuid4()
        other = uuid4()
        seed_to_other = _make_edge("semantic_similarity", 0.3, dst_id=other)
        edge_phase.edge_repo.get_outgoing_edges = AsyncMock(return_value=[seed_to_other])

        filtered = await edge_phase._filter_existing_edges(
            [(src, dst, 0.75)], "user-1", "ws-1", "ctx-1"
        )

        assert filtered == [(src, dst, 0.75)]

    @pytest.mark.asyncio
    async def test_real_edge_to_other_neighbor_does_not_block_candidate(self, edge_phase):
        """A real edge to a *different* neighbor must not block the current
        candidate either."""
        src = uuid4()
        dst = uuid4()
        other = uuid4()
        real_to_other = _make_edge("related_to", 0.8, dst_id=other)
        edge_phase.edge_repo.get_outgoing_edges = AsyncMock(return_value=[real_to_other])

        filtered = await edge_phase._filter_existing_edges(
            [(src, dst, 0.75)], "user-1", "ws-1", "ctx-1"
        )

        assert filtered == [(src, dst, 0.75)]
