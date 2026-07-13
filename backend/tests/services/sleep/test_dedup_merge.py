"""Tests for Sleep Maintenance Phase 2: Dedup/Merge.

Issue #101: Union-Find clustering, LLM judgment, merge execution.
"""

from datetime import datetime
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
    config.sleep_dedup_supersede_enabled = False  # #1208: remove mode (default)
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

    def test_equal_importance_newer_wins(self):
        """#1195: at equal importance the NEWER memory wins, not cluster order."""
        phase = DedupMergePhase.__new__(DedupMergePhase)

        older = _make_memory(importance=0.5)
        older.created_at = datetime(2026, 6, 1)
        newer = _make_memory(importance=0.5)
        newer.created_at = datetime(2026, 7, 1)
        pair_scores = {tuple(sorted([older.id, newer.id], key=str)): 0.99}

        # older first in cluster order — the pre-#1195 tie-break picked it
        decisions = phase._rule_based_judge([older, newer], pair_scores)

        assert decisions == [(newer.id, older.id)]

    def test_equal_importance_edited_memory_wins(self):
        """#1198 review: recency is max(created_at, updated_at) — an in-place
        edited memory (old created_at, new updated_at) must beat a
        later-created stale duplicate."""
        phase = DedupMergePhase.__new__(DedupMergePhase)

        edited = _make_memory(importance=0.5)
        edited.created_at = datetime(2026, 6, 1)
        edited.updated_at = datetime(2026, 7, 15)
        stale_dup = _make_memory(importance=0.5)
        stale_dup.created_at = datetime(2026, 7, 1)
        stale_dup.updated_at = datetime(2026, 7, 1)
        pair_scores = {tuple(sorted([edited.id, stale_dup.id], key=str)): 0.99}

        decisions = phase._rule_based_judge([edited, stale_dup], pair_scores)

        assert decisions == [(edited.id, stale_dup.id)]


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


# ============================================================================
# #475: Embedding cost-grade instrumentation tests
# ============================================================================


class TestDedupMergeEmbeddingInstrumentation:
    """Phase 2 ``_find_similar_pairs`` accumulates embedding usage via
    ``embed_with_usage`` (#475 PR-1). Mirrors ``reindex.py`` semantics:
    calls increments +1 per invocation (cache hit included), tokens
    accumulates the API-billed count (cache hits contribute 0).
    """

    @pytest.mark.asyncio
    async def test_find_similar_pairs_accumulates_tokens(self, dedup_phase):
        """Happy path: embed_with_usage returns positive tokens; both
        counters move."""
        dedup_phase.embedding_service.embed_with_usage = AsyncMock(return_value=([0.1] * 768, 75))
        dedup_phase.embedding_service.provider = "openai"
        dedup_phase.embedding_service.model = "text-embedding-3-small"

        memories = [_make_memory(summary="alpha"), _make_memory(summary="beta")]

        with patch(
            "services.sleep.dedup_merge.search_memories_qdrant",
            AsyncMock(return_value=[]),
        ):
            await dedup_phase._find_similar_pairs(
                memories, "user-1", "ws-1", "ctx-1", threshold=0.92
            )

        assert dedup_phase._embedding_calls_used == 2
        assert dedup_phase._embedding_tokens_used == 150  # 75 + 75

    @pytest.mark.asyncio
    async def test_find_similar_pairs_cache_hit_counts_call_not_tokens(self, dedup_phase):
        """Cache-hit semantic: tokens=0 is correctly attributed (no API
        bill), but the call still counts (+1) for parity with reindex.py."""
        dedup_phase.embedding_service.embed_with_usage = AsyncMock(return_value=([0.1] * 768, 0))
        dedup_phase.embedding_service.provider = "openai"
        dedup_phase.embedding_service.model = "text-embedding-3-small"

        memories = [_make_memory(summary="cached")]

        with patch(
            "services.sleep.dedup_merge.search_memories_qdrant",
            AsyncMock(return_value=[]),
        ):
            await dedup_phase._find_similar_pairs(
                memories, "user-1", "ws-1", "ctx-1", threshold=0.92
            )

        assert dedup_phase._embedding_calls_used == 1
        assert dedup_phase._embedding_tokens_used == 0


# ============================================================================
# #1229: merge audit must snapshot fields BEFORE the merge executes
# ============================================================================


class _PostMergeAccess(RuntimeError):
    """Stand-in for sqlalchemy MissingGreenlet: reading an expired attribute
    after the merge UPDATE triggers a synchronous refresh under the async
    engine. Deliberately NOT AttributeError — getattr(..., default) must not
    swallow it (the real MissingGreenlet is not swallowed either)."""


def _make_sealable_memory(*, created_at, updated_at, importance=0.5):
    """A Memory stand-in whose data attributes raise after ``seal()``.

    Models the #1229 failure: the loser's soft-delete UPDATE fires
    ``onupdate=func.now()``, expiring ``updated_at`` on the in-session
    instance; any later read attempts a sync refresh → MissingGreenlet.
    """

    class _Sealable:
        def __init__(self):
            d = object.__getattribute__(self, "__dict__")
            d.update(
                id=uuid4(),
                summary="test summary",
                type="note",
                importance=importance,
                tags=["tag-a"],
                access_count=1,
                source_type="manual",
                created_at=created_at,
                updated_at=updated_at,
                _sealed=False,
            )

        def seal(self):
            object.__getattribute__(self, "__dict__")["_sealed"] = True

        def __getattribute__(self, name):
            d = object.__getattribute__(self, "__dict__")
            if d.get("_sealed") and name in (
                "created_at",
                "updated_at",
                "tags",
                "summary",
                "source_type",
                "type",
                "importance",
            ):
                raise _PostMergeAccess(f"post-merge attribute access: {name}")
            return object.__getattribute__(self, name)

    return _Sealable()


class TestMergeAuditSnapshot:
    """#1229: dedup died on its first merge because the audit block read
    ``loser`` attributes AFTER ``_execute_merge`` soft-deleted the row —
    ``onupdate=func.now()`` expires ``updated_at``, and the resulting sync
    refresh raises MissingGreenlet under the async engine. The whole phase
    then failed (success=false) while the run still graded 'completed',
    and the unmerged near-dup pairs leaked into consolidation (stale_only=12).

    Contract: every audit field is snapshotted from the PRE-merge state
    (what the decision actually saw — the #1209 intent), and no
    winner/loser attribute is read after the merge executes.
    """

    @pytest.mark.asyncio
    async def test_no_attribute_reads_after_merge(self, dedup_phase):
        config = _make_config(provider="")  # rule-based judge — no LLM needed
        budget = SleepBudget()
        newer = datetime(2026, 7, 1, 12, 0)
        older = datetime(2026, 6, 1, 12, 0)
        winner = _make_sealable_memory(created_at=newer, updated_at=newer)
        loser = _make_sealable_memory(created_at=older, updated_at=None)

        dedup_phase._fetch_active_memories = AsyncMock(return_value=[winner, loser])
        dedup_phase._find_similar_pairs = AsyncMock(return_value=[(winner.id, loser.id, 0.99)])

        async def _seal_both(w, l_, *args, **kwargs):  # noqa: ANN002, ANN003
            w.seal()
            l_.seal()

        dedup_phase._execute_merge = AsyncMock(side_effect=_seal_both)
        reporter = AsyncMock()

        result = await dedup_phase.execute(
            config,
            "user-1",
            "ws-1",
            "ctx-1",
            budget,
            reporter=reporter,
            report_id=uuid4(),
        )

        assert result.success is True
        assert result.details["merged"] == 1
        details = reporter.add_action.await_args.kwargs["details"]
        # Pre-merge snapshot: the loser's recency is its created_at (its
        # updated_at was None before the merge bumped it).
        assert details["loser_recency"] == f"{older:%Y-%m-%d %H:%M}"
        assert details["winner_recency"] == f"{newer:%Y-%m-%d %H:%M}"
        assert details["winner_tags"] == ["tag-a"]
        assert details["loser_summary"] == "test summary"
        assert details["mode"] == "remove"
