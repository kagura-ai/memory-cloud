"""Tests for Sleep Maintenance Phase 1: Edge Discovery.

Issue #103: Recency-weighted sampling, LLM edge proposals.
"""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services.sleep.edge_discovery import (
    BATCH_SIZE,
    DISCOVERY_EDGE_WEIGHT,
    SIMILARITY_MAX,
    SIMILARITY_MIN,
    EdgeDiscoveryPhase,
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
