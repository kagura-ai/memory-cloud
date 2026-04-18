"""Tests for Sleep Maintenance Phase 1: Edge Discovery.

Issue #103: Recency-weighted sampling, LLM edge proposals.
"""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services.sleep.edge_discovery import (
    BATCH_SIZE,
    CONFIDENCE_HISTOGRAM_KEYS,
    DIRECTED_EDGE_TYPES,
    DISCOVERY_EDGE_WEIGHT,
    SEMANTIC_SIMILARITY_SYNTHETIC_WEIGHT_THRESHOLD,
    SIMILARITY_MAX,
    SIMILARITY_MIN,
    BatchStats,
    EdgeDiscoveryPhase,
    _build_confidence_histogram,
    _is_synthetic_seed_edge,
)
from services.sleep.prompts import EDGE_DISCOVERY_PROMPT_REVISION
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


# ---------------------------------------------------------------------------
# #306 helpers — _llm_judge_batch direct testing
# ---------------------------------------------------------------------------


def _make_llm_response(edges, tokens=100):
    """Build a canned `complete_json` return value: (response_dict, tokens).

    Each entry in ``edges`` is a tuple of:
        (label_a, label_b, related: bool, edge_type: str, confidence: float)
    """
    return (
        {
            "edges": [
                {
                    "pair": [a, b],
                    "related": related,
                    "edge_type": edge_type,
                    "confidence": confidence,
                }
                for (a, b, related, edge_type, confidence) in edges
            ]
        },
        tokens,
    )


@pytest.fixture
def deterministic_shuffle(monkeypatch):
    """Disable random.shuffle in edge_discovery so label assignment is stable."""
    monkeypatch.setattr("services.sleep.edge_discovery.random.shuffle", lambda x: None)


@pytest.fixture
def llm_judge_phase(edge_phase, deterministic_shuffle):
    """edge_phase with `complete_json` ready to be stubbed and shuffle pinned."""
    edge_phase.llm_service.complete_json = AsyncMock()
    return edge_phase


def _make_batch_pair(n=2):
    """Return ``(mems, batch, memory_map)`` for deterministic A/B/... tests.

    `_llm_judge_batch` assigns labels by sorting all memory IDs by ``str(id)``
    before mapping them to A/B/.... The ``deterministic_shuffle`` fixture only
    disables the later `random.shuffle` used for display order; it does not
    control label assignment itself. This helper does not return labels; use
    ``_labels_for(memory_map)`` to derive the actual label assignment when
    constructing canned LLM responses.
    """
    mems = [_make_memory() for _ in range(n)]
    batch = [(mems[i].id, mems[i + 1].id, 0.75) for i in range(n - 1)]
    memory_map = {m.id: m for m in mems}
    return mems, batch, memory_map


def _labels_for(memory_map):
    """Return {memory_id: label} matching `_llm_judge_batch`'s sorted assignment.

    `_llm_judge_batch` does `id_list = sorted(all_ids, key=str)` then assigns
    labels A, B, C, ... in that order. Tests that construct canned LLM
    responses must use labels derived from this same mapping — hardcoded
    ("A", "B") would only work by coincidence on UUIDs that happen to sort
    in insertion order. Loop 4 hallucination guard rejects pairs not in the
    requested batch, so wrong labels manifest as silent test failures.
    """
    return {mid: chr(ord("A") + i) for i, mid in enumerate(sorted(memory_map.keys(), key=str))}


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
        # #306: early-return paths zero-init metric keys.
        assert result.details["llm_accepted"] == 0
        assert result.details["llm_rejected"] == 0
        assert result.details["llm_call_failures"] == 0
        assert result.details["auto_accepted"] == 0
        assert result.details["edge_type_dist"] == {}
        assert result.details["avg_confidence"] == 0.0
        assert result.details["confidence_histogram"] == dict.fromkeys(CONFIDENCE_HISTOGRAM_KEYS, 0)
        assert result.details["llm_model"] == "gpt-5-nano"
        assert result.details["prompt_revision"] == EDGE_DISCOVERY_PROMPT_REVISION
        # PhD review additions (#306 follow-up): all summary stats zero, n=0
        assert result.details["median_confidence"] == 0.0
        assert result.details["p25_confidence"] == 0.0
        assert result.details["p75_confidence"] == 0.0
        assert result.details["confidence_n"] == 0
        assert result.details["confidence_imputed"] == 0

    @pytest.mark.asyncio
    async def test_no_candidates(self, edge_phase):
        config = _make_config()
        budget = SleepBudget()

        mems = [_make_memory() for _ in range(3)]
        edge_phase._sample_memories = AsyncMock(return_value=mems)
        edge_phase._find_candidates = AsyncMock(return_value=[])

        result = await edge_phase.execute(config, "user-1", "ws-1", "ctx-1", budget)

        assert result.details["message"] == "no_edge_candidates"
        # #306 zero-init.
        assert result.details["sampled"] == 3
        assert result.details["candidates"] == 0
        assert result.details["llm_accepted"] == 0
        assert result.details["auto_accepted"] == 0
        assert result.details["confidence_histogram"] == dict.fromkeys(CONFIDENCE_HISTOGRAM_KEYS, 0)
        assert result.details["prompt_revision"] == EDGE_DISCOVERY_PROMPT_REVISION

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
        # #306 zero-init.
        assert result.details["sampled"] == 3
        assert result.details["candidates"] == 1
        assert result.details["filtered"] == 0
        assert result.details["llm_accepted"] == 0
        assert result.details["auto_accepted"] == 0
        assert result.details["edge_type_dist"] == {}
        assert result.details["confidence_histogram"] == dict.fromkeys(CONFIDENCE_HISTOGRAM_KEYS, 0)

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
        # #306: auto-accept path increments `auto_accepted`, not `llm_accepted`.
        # avg_confidence / edge_type_dist / histogram stay zero (no pollution).
        assert result.details["auto_accepted"] == 1
        assert result.details["llm_accepted"] == 0
        assert result.details["llm_rejected"] == 0
        assert result.details["llm_call_failures"] == 0
        assert result.details["edge_type_dist"] == {}
        assert result.details["avg_confidence"] == 0.0
        assert result.details["confidence_histogram"] == dict.fromkeys(CONFIDENCE_HISTOGRAM_KEYS, 0)

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


# ===========================================================================
# Issue #306: _llm_judge_batch direct tests + execute() aggregation tests
# ===========================================================================


class TestLLMJudgeBatch:
    """Direct unit tests for _llm_judge_batch (#306).

    All tests stub `complete_json` and pin `random.shuffle` so the LLM-side
    contract — counts, edge_type validation, confidence clamping, exception
    handling — can be exercised hermetically.
    """

    @pytest.mark.asyncio
    async def test_all_accept(self, llm_judge_phase):
        config = _make_config()
        budget = SleepBudget()
        _, batch, memory_map = _make_batch_pair(n=3)
        labels = _labels_for(memory_map)

        llm_judge_phase.llm_service.complete_json.return_value = _make_llm_response(
            [
                (labels[batch[0][0]], labels[batch[0][1]], True, "related_to", 0.9),
                (labels[batch[1][0]], labels[batch[1][1]], True, "depends_on", 0.85),
            ]
        )

        confirmed, stats = await llm_judge_phase._llm_judge_batch(
            batch, memory_map, "user-1", "ctx-1", "ws-1", budget, config
        )

        assert len(confirmed) == 2
        assert stats.accepted == 2
        assert stats.rejected == 0
        assert stats.failures == 0
        assert stats.edge_type_counts == {"related_to": 1, "depends_on": 1}
        assert stats.confidences == [0.9, 0.85]

    @pytest.mark.asyncio
    async def test_all_reject(self, llm_judge_phase):
        config = _make_config()
        budget = SleepBudget()
        _, batch, memory_map = _make_batch_pair(n=3)
        labels = _labels_for(memory_map)

        llm_judge_phase.llm_service.complete_json.return_value = _make_llm_response(
            [
                (labels[batch[0][0]], labels[batch[0][1]], False, "related_to", 0.2),
                (labels[batch[1][0]], labels[batch[1][1]], False, "related_to", 0.3),
            ]
        )

        confirmed, stats = await llm_judge_phase._llm_judge_batch(
            batch, memory_map, "user-1", "ctx-1", "ws-1", budget, config
        )

        assert confirmed == []
        assert stats.accepted == 0
        assert stats.rejected == 2
        assert stats.failures == 0
        assert stats.edge_type_counts == {}
        assert stats.confidences == []

    @pytest.mark.asyncio
    async def test_mixed_accept_reject(self, llm_judge_phase):
        config = _make_config()
        budget = SleepBudget()
        _, batch, memory_map = _make_batch_pair(n=3)
        labels = _labels_for(memory_map)

        llm_judge_phase.llm_service.complete_json.return_value = _make_llm_response(
            [
                (labels[batch[0][0]], labels[batch[0][1]], True, "learned_from", 0.75),
                (labels[batch[1][0]], labels[batch[1][1]], False, "related_to", 0.4),
            ]
        )

        confirmed, stats = await llm_judge_phase._llm_judge_batch(
            batch, memory_map, "user-1", "ctx-1", "ws-1", budget, config
        )

        assert len(confirmed) == 1
        assert stats.accepted == 1
        assert stats.rejected == 1
        assert stats.edge_type_counts == {"learned_from": 1}
        assert stats.confidences == [0.75]

    @pytest.mark.asyncio
    async def test_invalid_edge_type_coerced_to_related_to(self, llm_judge_phase):
        """Unknown edge_type must be coerced to "related_to" and counted as such."""
        config = _make_config()
        budget = SleepBudget()
        _, batch, memory_map = _make_batch_pair(n=2)
        # Use _labels_for to derive labels from sorted-UUID order (matches
        # _llm_judge_batch's assignment). For n=2 with frozenset hallucination
        # guard, hardcoded ("A", "B") happens to work today (orientation-
        # agnostic), but using _labels_for is consistent with multi-pair tests
        # and robust to a future tightening of the guard semantics.
        labels = _labels_for(memory_map)

        llm_judge_phase.llm_service.complete_json.return_value = _make_llm_response(
            [(labels[batch[0][0]], labels[batch[0][1]], True, "garbage_type", 0.8)]
        )

        confirmed, stats = await llm_judge_phase._llm_judge_batch(
            batch, memory_map, "user-1", "ctx-1", "ws-1", budget, config
        )

        assert len(confirmed) == 1
        assert confirmed[0][2] == "related_to"
        assert stats.edge_type_counts == {"related_to": 1}
        # "garbage_type" must NOT appear in dist
        assert "garbage_type" not in stats.edge_type_counts

    @pytest.mark.asyncio
    async def test_invalid_pair_label_skipped(self, llm_judge_phase):
        """Labels outside the assigned A/B/... range are skipped silently and
        do NOT count toward accepted or rejected — they are malformed input."""
        config = _make_config()
        budget = SleepBudget()
        _, batch, memory_map = _make_batch_pair(n=2)
        labels = _labels_for(memory_map)

        llm_judge_phase.llm_service.complete_json.return_value = _make_llm_response(
            [
                (labels[batch[0][0]], labels[batch[0][1]], True, "related_to", 0.9),
                # Out-of-range labels — must be silently skipped.
                ("X", "Y", True, "related_to", 0.9),
            ]
        )

        confirmed, stats = await llm_judge_phase._llm_judge_batch(
            batch, memory_map, "user-1", "ctx-1", "ws-1", budget, config
        )

        assert len(confirmed) == 1
        assert stats.accepted == 1
        # Malformed pairs (bad labels) must NOT be counted as rejected — they
        # were never a valid input to begin with.
        assert stats.rejected == 0

    @pytest.mark.asyncio
    async def test_confidence_clamped_to_unit_range(self, llm_judge_phase):
        """Confidence values outside [0.0, 1.0] are clamped before storage."""
        config = _make_config()
        budget = SleepBudget()
        _, batch, memory_map = _make_batch_pair(n=3)
        labels = _labels_for(memory_map)

        llm_judge_phase.llm_service.complete_json.return_value = _make_llm_response(
            [
                (labels[batch[0][0]], labels[batch[0][1]], True, "related_to", 1.5),  # over
                (labels[batch[1][0]], labels[batch[1][1]], True, "related_to", -0.3),  # under
            ]
        )

        confirmed, stats = await llm_judge_phase._llm_judge_batch(
            batch, memory_map, "user-1", "ctx-1", "ws-1", budget, config
        )

        assert stats.accepted == 2
        assert stats.confidences == [1.0, 0.0]
        assert confirmed[0][3] == 1.0
        assert confirmed[1][3] == 0.0

    @pytest.mark.asyncio
    async def test_complete_json_raises_increments_failures(self, llm_judge_phase):
        """Exception in complete_json yields ([], BatchStats(failures=1))."""
        config = _make_config()
        budget = SleepBudget()
        _, batch, memory_map = _make_batch_pair(n=2)

        llm_judge_phase.llm_service.complete_json.side_effect = RuntimeError("LLM down")

        confirmed, stats = await llm_judge_phase._llm_judge_batch(
            batch, memory_map, "user-1", "ctx-1", "ws-1", budget, config
        )

        assert confirmed == []
        assert stats.accepted == 0
        assert stats.rejected == 0
        assert stats.failures == 1
        assert stats.edge_type_counts == {}
        assert stats.confidences == []
        # Loop 2 fix: failed attempts must consume budget to prevent runaway
        # retries. Pre-fix, budget.consume(llm_calls=1) ran only on success
        # → max_llm_calls would be ignored when the LLM is failing.
        assert budget.llm_calls_used == 1

    @pytest.mark.asyncio
    async def test_hallucinated_pair_rejected(self, llm_judge_phase):
        """Loop 4 fix: LLM may return pairs that were never in the requested
        batch (hallucination). Such pairs MUST be silently dropped — they do
        NOT contribute to accepted/rejected, do NOT create edges, and must
        not inflate observability metrics. Orientation-agnostic match: the
        LLM may flip the pair order; that is allowed and counted as a real
        response, but a fully unrequested pair is not.
        """
        config = _make_config()
        budget = SleepBudget()
        # 3 memories: A, B, C. We request only the (A, B) pair.
        mems = [_make_memory() for _ in range(3)]
        memory_map = {m.id: m for m in mems}
        # Sort to match production label assignment order. Only A and B are
        # used in the requested batch — C exists in memory_map but is NOT in
        # the batch, so any LLM-returned pair containing C must be rejected.
        sorted_ids = sorted(memory_map.keys(), key=str)
        a_id, b_id = sorted_ids[0], sorted_ids[1]
        batch = [(a_id, b_id, 0.75)]  # only (A, B) requested

        # LLM hallucinates: returns the requested (A, B) AND an unrequested (A, C).
        llm_judge_phase.llm_service.complete_json.return_value = _make_llm_response(
            [
                ("A", "B", True, "related_to", 0.9),
                ("A", "C", True, "related_to", 0.8),  # hallucinated — never asked
            ]
        )

        confirmed, stats = await llm_judge_phase._llm_judge_batch(
            batch, memory_map, "user-1", "ctx-1", "ws-1", budget, config
        )

        # Only the requested (A, B) is accepted; (A, C) is silently dropped.
        assert len(confirmed) == 1
        assert stats.accepted == 1
        assert stats.rejected == 0  # hallucinated pairs are dropped, not rejected
        assert stats.confidences == [0.9]
        assert stats.edge_type_counts == {"related_to": 1}

    @pytest.mark.asyncio
    async def test_dst_outside_memory_map_skips_pair_no_keyerror(self, llm_judge_phase):
        """Issue #369: when `_find_candidates` returns a pair whose `dst` is
        outside the sampled batch (the normal case in production with
        sample_size=30 and corpus≥100), `_llm_judge_batch` MUST NOT raise
        `KeyError` on `id_to_label[dst]`. Instead, the pair is silently
        skipped — it was never judged, so it counts toward neither accepted
        nor rejected nor failures.
        """
        config = _make_config()
        budget = SleepBudget()

        # Build a batch with 1 pair where src IS in memory_map but dst is NOT.
        mem_a = _make_memory()
        unknown_dst = uuid4()
        batch = [(mem_a.id, unknown_dst, 0.75)]
        memory_map = {mem_a.id: mem_a}

        # complete_json should NOT be called — the batch has no judgable pair
        # after filtering. We assert this below.
        confirmed, stats = await llm_judge_phase._llm_judge_batch(
            batch, memory_map, "user-1", "ctx-1", "ws-1", budget, config
        )

        # Pre-fix this raised KeyError before complete_json ever ran. Post-fix,
        # the pair is silently skipped and the batch returns the empty result.
        assert confirmed == []
        assert stats.accepted == 0
        assert stats.rejected == 0
        # CRITICAL: failures stays 0 — the LLM was never called, this is
        # a "nothing to judge" path, not a "judging failed" path.
        assert stats.failures == 0
        # complete_json was not invoked: skipped before the LLM call.
        llm_judge_phase.llm_service.complete_json.assert_not_called()


class TestDirectedEdgeOrientation:
    """Issue #373: directional canonicalization in `_llm_judge_batch`.

    `related_to` is undirected — the LLM may return the pair in either order
    and the parser still accepts. `depends_on` and `learned_from` are directed
    — if the LLM flips the input pair order, the parser MUST silently drop
    that pair (same treatment as hallucinated/malformed pairs: no contribution
    to accepted/rejected/failures). Without this, edges would be silently
    stored in the wrong direction (PR #371 PhD-pl review finding).
    """

    def test_directed_edge_types_constant_matches_design(self):
        """Sanity check: the constant covers exactly the two directed types
        called out in the issue, so future additions to the schema must pass
        through this list deliberately."""
        assert DIRECTED_EDGE_TYPES == frozenset({"depends_on", "learned_from"})

    @pytest.mark.asyncio
    @pytest.mark.parametrize("directed_edge_type", ["depends_on", "learned_from"])
    async def test_directed_pair_flipped_silently_skipped(
        self, llm_judge_phase, directed_edge_type
    ):
        """Directed edge_type with LLM-flipped pair order must be silently
        dropped — not accepted (would store wrong direction), not rejected
        (the LLM did not say "not related"), and not counted as a failure
        (the LLM call itself succeeded). Same accounting as hallucinated /
        malformed pairs."""
        config = _make_config()
        budget = SleepBudget()
        _, batch, memory_map = _make_batch_pair(n=2)
        labels = _labels_for(memory_map)
        # Request (A, B); LLM returns the flipped (B, A) for a directed type.
        src_label = labels[batch[0][0]]
        dst_label = labels[batch[0][1]]

        llm_judge_phase.llm_service.complete_json.return_value = _make_llm_response(
            [(dst_label, src_label, True, directed_edge_type, 0.9)]
        )

        confirmed, stats = await llm_judge_phase._llm_judge_batch(
            batch, memory_map, "user-1", "ctx-1", "ws-1", budget, config
        )

        assert confirmed == []
        assert stats.accepted == 0
        assert stats.rejected == 0
        assert stats.failures == 0
        assert stats.edge_type_counts == {}
        assert stats.confidences == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("directed_edge_type", ["depends_on", "learned_from"])
    async def test_directed_pair_correct_order_accepted(self, llm_judge_phase, directed_edge_type):
        """Sanity: directed edge_type with the input pair order preserved is
        accepted normally. Guards against an over-eager direction filter that
        rejects every directed pair."""
        config = _make_config()
        budget = SleepBudget()
        _, batch, memory_map = _make_batch_pair(n=2)
        labels = _labels_for(memory_map)
        src_label = labels[batch[0][0]]
        dst_label = labels[batch[0][1]]

        llm_judge_phase.llm_service.complete_json.return_value = _make_llm_response(
            [(src_label, dst_label, True, directed_edge_type, 0.9)]
        )

        confirmed, stats = await llm_judge_phase._llm_judge_batch(
            batch, memory_map, "user-1", "ctx-1", "ws-1", budget, config
        )

        assert len(confirmed) == 1
        assert stats.accepted == 1
        assert stats.rejected == 0
        assert stats.edge_type_counts == {directed_edge_type: 1}
        # src/dst preserved in the order the LLM returned (which equals the
        # input order, by construction of this test).
        assert confirmed[0][0] == batch[0][0]
        assert confirmed[0][1] == batch[0][1]
        assert confirmed[0][2] == directed_edge_type

    @pytest.mark.asyncio
    async def test_undirected_pair_flipped_still_accepted(self, llm_judge_phase):
        """Undirected `related_to` is unaffected by #373 — the LLM may flip
        the pair order and the parser still accepts. The orientation-agnostic
        frozenset match (existing hallucination guard) is preserved for this
        edge_type."""
        config = _make_config()
        budget = SleepBudget()
        _, batch, memory_map = _make_batch_pair(n=2)
        labels = _labels_for(memory_map)
        src_label = labels[batch[0][0]]
        dst_label = labels[batch[0][1]]

        llm_judge_phase.llm_service.complete_json.return_value = _make_llm_response(
            [(dst_label, src_label, True, "related_to", 0.9)]
        )

        confirmed, stats = await llm_judge_phase._llm_judge_batch(
            batch, memory_map, "user-1", "ctx-1", "ws-1", budget, config
        )

        assert len(confirmed) == 1
        assert stats.accepted == 1
        assert stats.edge_type_counts == {"related_to": 1}
        # The parser uses the LLM's pair order verbatim for undirected edges,
        # so src/dst here reflect the flipped order. Semantically equivalent
        # to (src=A, dst=B) for the consumer because `related_to` is symmetric.
        assert confirmed[0][0] == batch[0][1]
        assert confirmed[0][1] == batch[0][0]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "raw_edge_type",
        [
            ["depends_on"],  # list — common LLM "I returned an array" mistake
            {"type": "depends_on"},  # dict — schema confusion
            42,  # int — wrong type entirely
            None,  # explicit null
        ],
    )
    async def test_non_string_edge_type_does_not_crash(self, llm_judge_phase, raw_edge_type):
        """LLM may return a non-string `edge_type` (list, dict, int, null) due
        to schema confusion. Pre-fix, `raw_type in valid_edge_types` raised
        `TypeError` on unhashables, aborting parsing of EVERY edge in the
        batch (including valid ones). The `isinstance(raw_type, str)` guard
        coerces the bad row to `related_to` and lets the rest of the response
        parse normally. Addresses Copilot review on PR #380."""
        config = _make_config()
        budget = SleepBudget()
        _, batch, memory_map = _make_batch_pair(n=3)
        labels = _labels_for(memory_map)

        # Build the response by hand — _make_llm_response only takes str.
        llm_judge_phase.llm_service.complete_json.return_value = (
            {
                "edges": [
                    {
                        "pair": [labels[batch[0][0]], labels[batch[0][1]]],
                        "related": True,
                        "edge_type": raw_edge_type,
                        "confidence": 0.8,
                    },
                    # Valid follow-up row — must still be parsed even though
                    # the first row had a non-string edge_type.
                    {
                        "pair": [labels[batch[1][0]], labels[batch[1][1]]],
                        "related": True,
                        "edge_type": "related_to",
                        "confidence": 0.9,
                    },
                ]
            },
            100,
        )

        confirmed, stats = await llm_judge_phase._llm_judge_batch(
            batch, memory_map, "user-1", "ctx-1", "ws-1", budget, config
        )

        # Both rows accepted: bad-row coerced to related_to, valid row passes
        # through. Pre-fix, the TypeError would have aborted parsing → 0 edges.
        assert len(confirmed) == 2
        assert stats.accepted == 2
        # Both end up as related_to (one coerced, one explicit).
        assert stats.edge_type_counts == {"related_to": 2}

    @pytest.mark.asyncio
    async def test_invalid_edge_type_flipped_treated_as_undirected(self, llm_judge_phase):
        """An unknown edge_type is coerced to `related_to` (existing #306
        behavior). Coercion happens BEFORE the directional check, so a
        flipped pair with `garbage_type` lands in the undirected branch and
        is accepted. This makes coercion direction-safe by construction —
        operators don't get a silent-drop surprise from unknown types."""
        config = _make_config()
        budget = SleepBudget()
        _, batch, memory_map = _make_batch_pair(n=2)
        labels = _labels_for(memory_map)
        src_label = labels[batch[0][0]]
        dst_label = labels[batch[0][1]]

        llm_judge_phase.llm_service.complete_json.return_value = _make_llm_response(
            [(dst_label, src_label, True, "garbage_type", 0.8)]
        )

        confirmed, stats = await llm_judge_phase._llm_judge_batch(
            batch, memory_map, "user-1", "ctx-1", "ws-1", budget, config
        )

        assert len(confirmed) == 1
        assert confirmed[0][2] == "related_to"
        assert stats.edge_type_counts == {"related_to": 1}


class TestExecuteAggregation:
    """`execute()` aggregates BatchStats across multiple batches (#306)."""

    @pytest.mark.asyncio
    async def test_multi_batch_aggregation(self, llm_judge_phase):
        """With BATCH_SIZE=5 and 6 candidates, _llm_judge_batch is called twice
        and execute() aggregates accepted/rejected/edge_type/confidences."""
        config = _make_config()
        budget = SleepBudget()

        # Build 7 memories so we get 6 pairs (= 2 batches at BATCH_SIZE=5)
        mems = [_make_memory() for _ in range(7)]
        candidates = [(mems[i].id, mems[i + 1].id, 0.75) for i in range(6)]

        llm_judge_phase._sample_memories = AsyncMock(return_value=mems)
        llm_judge_phase._find_candidates = AsyncMock(return_value=candidates)
        llm_judge_phase._filter_existing_edges = AsyncMock(return_value=candidates)
        llm_judge_phase.edge_repo.create_or_update_edge = AsyncMock()

        # Stub _llm_judge_batch directly so we control batch_stats per call.
        async def fake_judge(batch, *args, **kwargs):
            if len(batch) == 5:
                # First batch: 3 accepted, 2 rejected
                stats = BatchStats(
                    accepted=3,
                    rejected=2,
                    edge_type_counts={"related_to": 2, "depends_on": 1},
                    confidences=[0.9, 0.7, 0.55],
                )
                confirmed = [
                    (batch[0][0], batch[0][1], "related_to", 0.9),
                    (batch[1][0], batch[1][1], "related_to", 0.7),
                    (batch[2][0], batch[2][1], "depends_on", 0.55),
                ]
                return confirmed, stats
            # Second batch: 1 accepted, 0 rejected
            stats = BatchStats(
                accepted=1,
                rejected=0,
                edge_type_counts={"learned_from": 1},
                confidences=[0.95],
            )
            confirmed = [(batch[0][0], batch[0][1], "learned_from", 0.95)]
            return confirmed, stats

        llm_judge_phase._llm_judge_batch = fake_judge

        result = await llm_judge_phase.execute(config, "user-1", "ws-1", "ctx-1", budget)

        assert result.details["llm_accepted"] == 4
        assert result.details["llm_rejected"] == 2
        assert result.details["llm_call_failures"] == 0
        assert result.details["auto_accepted"] == 0
        assert result.details["edge_type_dist"] == {
            "related_to": 2,
            "depends_on": 1,
            "learned_from": 1,
        }
        # avg = (0.9 + 0.7 + 0.55 + 0.95) / 4 = 0.775
        assert result.details["avg_confidence"] == pytest.approx(0.775)
        # 0.55 → "0.5-0.7", 0.7 → "0.7-0.85", 0.9 → "0.85-1.0", 0.95 → "0.85-1.0"
        assert result.details["confidence_histogram"] == {
            "0.0-0.5": 0,
            "0.5-0.7": 1,
            "0.7-0.85": 1,
            "0.85-1.0": 2,
        }
        assert result.details["llm_model"] == "gpt-5-nano"
        assert result.details["prompt_revision"] == EDGE_DISCOVERY_PROMPT_REVISION
        # PhD review additions (#306 follow-up): 5-number summary + sample size
        # confidences = [0.9, 0.7, 0.55, 0.95] → sorted [0.55, 0.7, 0.9, 0.95]
        # statistics.quantiles(n=4, method="inclusive") on n=4:
        #   position formula: (n-1) * i/4 where n = len(data)
        #   i=1: pos 0.75 → 0.55 + 0.75*(0.7-0.55) = 0.6625
        #   i=2: pos 1.50 → 0.70 + 0.50*(0.9-0.70) = 0.8000 (median)
        #   i=3: pos 2.25 → 0.90 + 0.25*(0.95-0.9) = 0.9125
        # Inclusive method ensures small-N quartiles stay in [min(data), max(data)].
        assert result.details["median_confidence"] == pytest.approx(0.8)
        assert result.details["p25_confidence"] == pytest.approx(0.6625)
        assert result.details["p75_confidence"] == pytest.approx(0.9125)
        assert result.details["confidence_n"] == 4
        assert result.details["confidence_imputed"] == 0


class TestConfidenceImputed:
    """Confirm that NaN/Inf confidence values are tracked, not silently lost (#306)."""

    @pytest.mark.asyncio
    async def test_nan_confidence_imputed_and_counted(self, llm_judge_phase):
        """LLM returning NaN confidence is replaced with 0.5, and the counter
        increments. Prevents silent imputation from masking prompt/model issues."""
        config = _make_config()
        budget = SleepBudget()
        _, batch, memory_map = _make_batch_pair(n=3)
        labels = _labels_for(memory_map)

        llm_judge_phase.llm_service.complete_json.return_value = _make_llm_response(
            [
                (labels[batch[0][0]], labels[batch[0][1]], True, "related_to", float("nan")),
                (labels[batch[1][0]], labels[batch[1][1]], True, "related_to", float("inf")),
            ]
        )

        confirmed, stats = await llm_judge_phase._llm_judge_batch(
            batch, memory_map, "user-1", "ctx-1", "ws-1", budget, config
        )

        assert stats.accepted == 2
        assert stats.confidence_imputed == 2
        # Both confidences imputed to 0.5 → fall in [0.5, 0.7) bucket
        assert stats.confidences == [0.5, 0.5]


class TestMetricsAliasing:
    """Defensive copy in _metrics_from_agg prevents result.details from
    aliasing the live BatchStats / histogram (#306 PhD-review fix).

    Note: this contract is verified directly via _metrics_from_agg unit test
    rather than an end-to-end execute() test — the unit test captures the
    source data structures and proves mutation isolation, which is the
    precise invariant the defensive copy provides. An end-to-end test that
    only mutates the result dict cannot demonstrate aliasing was prevented.
    """

    def test_summarize_confidences_inclusive_quartiles_bounded(self):
        """Loop FB fix: small-N quartiles MUST stay within [min(data), max(data)].

        statistics.quantiles default ("exclusive" Tukey method) extrapolates
        outside the data range for n=2 and n=3 — uninterpretable when the
        underlying data is clamped to [0, 1]. We use method="inclusive" to
        guarantee p25/p75 ∈ [min, max] of the observed confidences.
        """
        from services.sleep.edge_discovery import _summarize_confidences

        # n=2 case
        s2 = _summarize_confidences([0.5, 0.9])
        assert 0.5 <= s2.p25 <= 0.9
        assert 0.5 <= s2.p75 <= 0.9
        # Inclusive on n=2: positions are (n-1)*i/4 = 1*0.25 = 0.25 and 1*0.75 = 0.75
        # Q1 = 0.5 + 0.25*(0.9-0.5) = 0.6; Q3 = 0.5 + 0.75*0.4 = 0.8
        assert s2.p25 == pytest.approx(0.6)
        assert s2.p75 == pytest.approx(0.8)

        # n=3 case
        s3 = _summarize_confidences([0.5, 0.7, 0.9])
        assert 0.5 <= s3.p25 <= 0.9
        assert 0.5 <= s3.p75 <= 0.9
        # Inclusive on n=3: positions are 2*0.25=0.5 and 2*0.75=1.5
        # Q1 = 0.5 + 0.5*(0.7-0.5) = 0.6; Q3 = 0.7 + 0.5*(0.9-0.7) = 0.8
        assert s3.p25 == pytest.approx(0.6)
        assert s3.p75 == pytest.approx(0.8)

        # n=1 → quartiles collapse to median
        s1 = _summarize_confidences([0.7])
        assert s1.p25 == 0.7
        assert s1.median == 0.7
        assert s1.p75 == 0.7

    def test_metrics_from_agg_returns_independent_dicts(self):
        """Direct test: _metrics_from_agg copies edge_type_counts and the
        histogram so callers can mutate freely."""
        from services.sleep.edge_discovery import (
            _build_confidence_histogram,
            _metrics_from_agg,
            _summarize_confidences,
        )

        agg = BatchStats(
            accepted=2,
            edge_type_counts={"related_to": 1, "depends_on": 1},
            confidences=[0.6, 0.9],
        )
        hist = _build_confidence_histogram(agg.confidences)
        summary = _summarize_confidences(agg.confidences)
        config = _make_config()

        emitted = _metrics_from_agg(agg, 0, summary, hist, config)

        # Mutate the emitted dict's mutable fields.
        emitted["edge_type_dist"]["NEW_KEY"] = 42
        emitted["confidence_histogram"]["0.0-0.5"] = 999

        # Source data MUST be unchanged (defensive copy worked).
        assert "NEW_KEY" not in agg.edge_type_counts
        assert hist["0.0-0.5"] == 0


class TestConfidenceHistogram:
    """Bucket boundary semantics for `_build_confidence_histogram` (#306).

    Convention: right-open `[a, b)` for the first three buckets, last bucket
    `[0.85, 1.0]` inclusive at 1.0. Boundary values 0.5 / 0.7 / 0.85 fall into
    the higher bucket.
    """

    def test_empty_returns_all_zero(self):
        h = _build_confidence_histogram([])
        assert h == dict.fromkeys(CONFIDENCE_HISTOGRAM_KEYS, 0)

    def test_boundary_0_5_goes_to_upper_bucket(self):
        assert _build_confidence_histogram([0.5])["0.5-0.7"] == 1
        assert _build_confidence_histogram([0.5])["0.0-0.5"] == 0

    def test_boundary_0_7_goes_to_upper_bucket(self):
        assert _build_confidence_histogram([0.7])["0.7-0.85"] == 1
        assert _build_confidence_histogram([0.7])["0.5-0.7"] == 0

    def test_boundary_0_85_goes_to_upper_bucket(self):
        assert _build_confidence_histogram([0.85])["0.85-1.0"] == 1
        assert _build_confidence_histogram([0.85])["0.7-0.85"] == 0

    def test_one_point_zero_included_in_last_bucket(self):
        """Last bucket is right-inclusive — 1.0 must not fall off the end."""
        assert _build_confidence_histogram([1.0])["0.85-1.0"] == 1

    def test_zero_goes_to_first_bucket(self):
        assert _build_confidence_histogram([0.0])["0.0-0.5"] == 1

    def test_distribution_count(self):
        h = _build_confidence_histogram([0.1, 0.4, 0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 1.0])
        assert h == {
            "0.0-0.5": 2,  # 0.1, 0.4
            "0.5-0.7": 2,  # 0.5, 0.6
            "0.7-0.85": 2,  # 0.7, 0.8
            "0.85-1.0": 3,  # 0.85, 0.9, 1.0
        }
