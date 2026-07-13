"""Tests for Sleep Maintenance Phase 2: Dedup/Merge.

Issue #101: Union-Find clustering, LLM judgment, merge execution.
"""

import math
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

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

    @pytest.mark.asyncio
    async def test_find_similar_pairs_populates_summary_vector_cache(self, dedup_phase):
        """#1231: the on-demand direct check reads ``_summary_vectors``;
        ``_find_similar_pairs`` must reset the cache, populate it for every
        successful embed, and omit failed ones (fail-closed upstream)."""
        ok = _make_memory(summary="alpha")
        bad = _make_memory(summary="beta")

        async def _embed(text, **_kwargs):
            if text == "beta":
                raise RuntimeError("provider down")
            return ([0.1] * 768, 75)

        dedup_phase.embedding_service.embed_with_usage = AsyncMock(side_effect=_embed)
        # Stale entry from a previous population must not survive the reset.
        dedup_phase._summary_vectors = {uuid4(): [9.9]}

        with patch(
            "services.sleep.dedup_merge.search_memories_qdrant",
            AsyncMock(return_value=[]),
        ):
            await dedup_phase._find_similar_pairs(
                [ok, bad], "user-1", "ws-1", "ctx-1", threshold=0.92
            )

        assert dedup_phase._summary_vectors == {ok.id: [0.1] * 768}


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
            # Plain assignments are fine — only reads are intercepted below.
            self.id = uuid4()
            self.summary = "test summary"
            self.type = "note"
            self.importance = importance
            self.tags = ["tag-a"]
            self.access_count = 1
            self.source_type = "manual"
            self.created_at = created_at
            self.updated_at = updated_at
            self._sealed = False

        def seal(self):
            self._sealed = True

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


async def _seal_both(w, l_, *args, **kwargs):  # noqa: ANN002, ANN003
    """Mocked _execute_merge side effect: seals both rows so any later
    attribute read models the post-merge expired-refresh crash (#1229)."""
    w.seal()
    l_.seal()


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

    @pytest.mark.asyncio
    async def test_shared_winner_across_decisions_snapshots_before_any_merge(self, dedup_phase):
        """#1229 (second crash site): decisions in one cluster share the
        winner; the FIRST merge's UPDATE expires attributes on that shared
        instance, so the SECOND decision's snapshot must already be taken.
        All audits are collected before any merge executes."""
        config = _make_config(provider="")
        budget = SleepBudget()
        newest = datetime(2026, 7, 1, 12, 0)
        older = datetime(2026, 6, 1, 12, 0)
        winner = _make_sealable_memory(created_at=newest, updated_at=newest)
        loser_a = _make_sealable_memory(created_at=older, updated_at=None)
        loser_b = _make_sealable_memory(created_at=older, updated_at=None)

        dedup_phase._fetch_active_memories = AsyncMock(return_value=[winner, loser_a, loser_b])
        dedup_phase._find_similar_pairs = AsyncMock(
            return_value=[
                (winner.id, loser_a.id, 0.99),
                (winner.id, loser_b.id, 0.99),
            ]
        )

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
        assert result.details["merged"] == 2
        assert reporter.add_action.await_count == 2
        for call in reporter.add_action.await_args_list:
            details = call.kwargs["details"]
            assert details["winner_recency"] == f"{newest:%Y-%m-%d %H:%M}"
            assert details["loser_recency"] == f"{older:%Y-%m-%d %H:%M}"


class TestCrossPairMergeVeto:
    """#1229 (run-6 residual): union-find clusters chain transitively, so the
    judge can nominate a (winner, loser) pair that has NO direct similarity
    edge — run 6 merged pairs whose true pairwise cosine was 0.43-0.66 under
    a 0.92 threshold (e.g. "Falcon Guild standup" absorbed into "Team
    Duskmoor standup"), destroying distinct facts and breaching
    update.stale_only_zero.

    Contract: a merge decision may only execute when the pair's direct
    pairwise similarity is >=threshold — since #1231 the score comes from
    the candidate pairs OR an on-demand cosine over this run's cached
    summary vectors when the candidate cap missed the pair. Eligibility is
    deterministic; the LLM only chooses among eligible pairs.
    """

    def _phase_with_chain(self, dedup_phase, *, chain_cosine=0.55):
        """Three memories where a-b and b-c are direct pairs but a-c is not.

        Seeds the per-run summary-vector cache the way ``_find_similar_pairs``
        does in production (#1231): every processed memory has a vector, so
        the missing a-c edge gets an on-demand direct cosine (``chain_cosine``,
        default inside run 6's observed 0.43-0.66 band) instead of a blind
        membership veto.
        """
        mem_a = _make_memory(summary="fact A")
        mem_b = _make_memory(summary="fact A (near-dup)")
        mem_c = _make_memory(summary="fact C, transitively chained")
        dedup_phase._fetch_active_memories = AsyncMock(return_value=[mem_a, mem_b, mem_c])
        dedup_phase._find_similar_pairs = AsyncMock(
            return_value=[
                (mem_a.id, mem_b.id, 0.93),
                (mem_b.id, mem_c.id, 0.93),
            ]
        )
        dedup_phase._summary_vectors = {
            mem_a.id: [1.0, 0.0],
            mem_b.id: [0.93, math.sqrt(1 - 0.93**2)],
            mem_c.id: [chain_cosine, math.sqrt(1 - chain_cosine**2)],
        }
        dedup_phase._execute_merge = AsyncMock()
        return mem_a, mem_b, mem_c

    @pytest.mark.asyncio
    async def test_merge_without_direct_edge_is_vetoed(self, dedup_phase):
        """Run 6's failure: the judge picks the transitive-only pair whose
        true cosine (0.43-0.66) is far below the 0.92 threshold. Since #1231
        the missing edge is resolved by an on-demand direct cosine — the
        sub-threshold veto still fires, judged on the computed SCORE."""
        config = _make_config()
        mem_a, _mem_b, mem_c = self._phase_with_chain(dedup_phase)
        dedup_phase._judge_cluster = AsyncMock(return_value=[(mem_a.id, mem_c.id)])
        reporter = AsyncMock()

        result = await dedup_phase.execute(
            config, "user-1", "ws-1", "ctx-1", SleepBudget(), reporter=reporter, report_id=uuid4()
        )

        assert result.success is True
        assert result.details["merged"] == 0
        assert result.details["llm_merge_guarded"] == 1
        assert result.details["llm_merge_rescued"] == 0
        assert result.details["llm_merge_unverifiable"] == 0
        dedup_phase._execute_merge.assert_not_awaited()
        reporter.add_action.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_direct_edge_merge_still_executes(self, dedup_phase):
        config = _make_config()
        mem_a, mem_b, _mem_c = self._phase_with_chain(dedup_phase)
        dedup_phase._judge_cluster = AsyncMock(return_value=[(mem_a.id, mem_b.id)])

        result = await dedup_phase.execute(config, "user-1", "ws-1", "ctx-1", SleepBudget())

        assert result.success is True
        assert result.details["merged"] == 1
        assert result.details["llm_merge_guarded"] == 0
        dedup_phase._execute_merge.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_mixed_decisions_veto_only_the_edgeless_pair(self, dedup_phase):
        config = _make_config()
        mem_a, mem_b, mem_c = self._phase_with_chain(dedup_phase)
        dedup_phase._judge_cluster = AsyncMock(
            return_value=[(mem_a.id, mem_c.id), (mem_a.id, mem_b.id)]
        )

        result = await dedup_phase.execute(config, "user-1", "ws-1", "ctx-1", SleepBudget())

        assert result.details["merged"] == 1
        assert result.details["llm_merge_guarded"] == 1
        assert result.details["llm_merge_rescued"] == 0
        # The surviving merge is the direct pair, not the transitive one.
        (winner, loser, *_rest), _ = dedup_phase._execute_merge.await_args
        assert {winner.id, loser.id} == {mem_a.id, mem_b.id}

    @pytest.mark.asyncio
    async def test_below_threshold_direct_pair_is_vetoed(self, dedup_phase):
        """#1229 (run-7): the veto must judge the SCORE, not mere membership
        in the candidate list — when candidate generation regresses (the
        score_threshold filter was silently dropped for years), sub-threshold
        pairs flow in as 'direct edges' and membership alone waves them
        through."""
        config = _make_config(threshold=0.92)
        mem_a = _make_memory(summary="fact A")
        mem_b = _make_memory(summary="unrelated fact B")
        dedup_phase._fetch_active_memories = AsyncMock(return_value=[mem_a, mem_b])
        # Direct pair, but far below threshold — run 7 observed 0.49-0.57.
        dedup_phase._find_similar_pairs = AsyncMock(return_value=[(mem_a.id, mem_b.id, 0.57)])
        dedup_phase._execute_merge = AsyncMock()
        dedup_phase._judge_cluster = AsyncMock(return_value=[(mem_a.id, mem_b.id)])

        result = await dedup_phase.execute(config, "user-1", "ws-1", "ctx-1", SleepBudget())

        assert result.details["merged"] == 0
        assert result.details["llm_merge_guarded"] == 1
        dedup_phase._execute_merge.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_shadow_mode_merge_without_direct_edge_is_vetoed(self, dedup_phase):
        """#1208 shadow mode is equally destructive for recall (the loser is
        hidden by the supersedes edge) — the veto must cover it too."""
        config = _make_config()
        config.sleep_dedup_supersede_enabled = True
        mem_a, _mem_b, mem_c = self._phase_with_chain(dedup_phase)
        # Shadow mode pre-filters already-settled pairs via the DB; pass
        # everything through — the veto under test sits downstream of it.
        dedup_phase._filter_already_superseded_pairs = AsyncMock(
            side_effect=lambda pairs, _user: (pairs, 0)
        )
        dedup_phase._execute_shadow_merge = AsyncMock()
        dedup_phase._judge_cluster = AsyncMock(return_value=[(mem_a.id, mem_c.id)])

        result = await dedup_phase.execute(config, "user-1", "ws-1", "ctx-1", SleepBudget())

        assert result.details["merged"] == 0
        assert result.details["llm_merge_guarded"] == 1
        dedup_phase._execute_shadow_merge.assert_not_awaited()


class TestDenseClusterDirectCheck:
    """#1231: ``_find_similar_pairs`` caps its neighbor search at limit=10,
    so in a dense cluster (>10 mutual near-duplicates — templated corpora
    like daily standup notes) a genuinely >=threshold pair can be absent
    from ``pair_scores`` while union-find still chains its members. The
    #1229 veto used to fire on the missing edge and block a legitimate
    merge.

    Contract: eligibility is still judged on the SCORE, never on
    membership — a missing edge gets an on-demand direct cosine computed
    from this run's cached summary vectors (zero extra embedding/Qdrant
    calls). Vetoes with no computable score stay fail-closed but are
    counted separately (``llm_merge_unverifiable``) so
    ``llm_merge_guarded`` stays a pure sub-threshold signal; rescued
    merges surface as ``llm_merge_rescued``.
    """

    def _phase_with_missing_edge(self, dedup_phase, *, direct_cosine):
        """a-b and b-c are candidate pairs; a-c is judge-nominated but
        missing from pair_scores, with cos(a, c) == direct_cosine."""
        mem_a = _make_memory(summary="standup note 1")
        mem_b = _make_memory(summary="standup note 2")
        mem_c = _make_memory(summary="standup note 3")
        dedup_phase._fetch_active_memories = AsyncMock(return_value=[mem_a, mem_b, mem_c])
        dedup_phase._find_similar_pairs = AsyncMock(
            return_value=[
                (mem_a.id, mem_b.id, 0.95),
                (mem_b.id, mem_c.id, 0.95),
            ]
        )
        dedup_phase._summary_vectors = {
            mem_a.id: [1.0, 0.0],
            mem_b.id: [0.95, math.sqrt(1 - 0.95**2)],
            mem_c.id: [direct_cosine, math.sqrt(1 - direct_cosine**2)],
        }
        dedup_phase._execute_merge = AsyncMock()
        dedup_phase._judge_cluster = AsyncMock(return_value=[(mem_a.id, mem_c.id)])
        return mem_a, mem_b, mem_c

    @pytest.mark.asyncio
    async def test_missing_pair_with_high_direct_cosine_is_rescued(self, dedup_phase):
        config = _make_config(threshold=0.92)
        mem_a, _b, mem_c = self._phase_with_missing_edge(dedup_phase, direct_cosine=0.95)
        # Snapshot pair_scores AT judge time: the dict is shared and the
        # merge loop's defense-in-depth backfill mutates it later, so
        # asserting on await_args.args[1] post-hoc would be vacuous.
        scores_seen_by_judge: dict = {}

        async def _judge_and_snapshot(_cluster_memories, pair_scores_arg, *_a, **_k):
            scores_seen_by_judge.update(pair_scores_arg)
            return [(mem_a.id, mem_c.id)]

        dedup_phase._judge_cluster = AsyncMock(side_effect=_judge_and_snapshot)

        result = await dedup_phase.execute(config, "user-1", "ws-1", "ctx-1", SleepBudget())

        assert result.success is True
        assert result.details["merged"] == 1
        assert result.details["llm_merge_rescued"] == 1
        assert result.details["llm_merge_guarded"] == 0
        assert result.details["llm_merge_unverifiable"] == 0
        dedup_phase._execute_merge.assert_awaited_once()
        # Pre-fill runs BEFORE judging: the judge (LLM prompt / rule path)
        # was shown the true cosine, not the 0.0 missing-pair fallback.
        key = tuple(sorted([mem_a.id, mem_c.id], key=str))
        assert scores_seen_by_judge[key] == pytest.approx(0.95)

    @pytest.mark.asyncio
    async def test_prefill_lets_rule_based_judge_auto_merge_missing_pair(self, dedup_phase):
        """#1231 non-LLM path: ``_rule_based_judge`` reads pair_scores
        directly, so a saturated-away pair used to default to 0.0 and could
        never reach AUTO_MERGE_THRESHOLD. The per-cluster pre-fill supplies
        the true cosine before judging."""
        config = _make_config(provider="")  # rule-based judge
        newer = datetime(2026, 7, 1, 12, 0)
        older = datetime(2026, 6, 1, 12, 0)
        mem_a = _make_memory(summary="dup 1", importance=0.9)
        mem_b = _make_memory(summary="dup 2", importance=0.6)
        mem_c = _make_memory(summary="dup 3", importance=0.5)
        for m in (mem_a, mem_b, mem_c):
            m.created_at = older
            m.updated_at = None
        mem_a.updated_at = newer
        dedup_phase._fetch_active_memories = AsyncMock(return_value=[mem_a, mem_b, mem_c])
        # Saturated discovery: (a, c) is missing despite true cosine 1.0.
        dedup_phase._find_similar_pairs = AsyncMock(
            return_value=[
                (mem_a.id, mem_b.id, 0.99),
                (mem_b.id, mem_c.id, 0.99),
            ]
        )
        dedup_phase._summary_vectors = {m.id: [1.0, 0.0] for m in (mem_a, mem_b, mem_c)}
        dedup_phase._execute_merge = AsyncMock()

        result = await dedup_phase.execute(config, "user-1", "ws-1", "ctx-1", SleepBudget())

        # All three pairs auto-merge at >=0.98, including the backfilled one.
        assert result.details["merged"] == 3
        assert result.details["llm_merge_rescued"] == 1
        assert result.details["llm_merge_guarded"] == 0
        assert result.details["llm_merge_unverifiable"] == 0

    @pytest.mark.asyncio
    async def test_rescued_merge_audit_records_computed_similarity(self, dedup_phase):
        """The on-demand score is backfilled into pair_scores so the audit
        record carries the true similarity, not the 0.0 fallback."""
        config = _make_config(threshold=0.92)
        self._phase_with_missing_edge(dedup_phase, direct_cosine=0.95)
        reporter = AsyncMock()

        result = await dedup_phase.execute(
            config, "user-1", "ws-1", "ctx-1", SleepBudget(), reporter=reporter, report_id=uuid4()
        )

        assert result.details["merged"] == 1
        details = reporter.add_action.await_args.kwargs["details"]
        assert details["similarity"] == pytest.approx(0.95)

    @pytest.mark.asyncio
    async def test_missing_pair_without_vectors_stays_fail_closed(self, dedup_phase):
        """No vectors (embed failed) → the score is unknowable this run:
        veto, but counted as unverifiable, NOT as a sub-threshold guard."""
        config = _make_config(threshold=0.92)
        self._phase_with_missing_edge(dedup_phase, direct_cosine=0.95)
        dedup_phase._summary_vectors = {}

        result = await dedup_phase.execute(config, "user-1", "ws-1", "ctx-1", SleepBudget())

        assert result.details["merged"] == 0
        assert result.details["llm_merge_unverifiable"] == 1
        assert result.details["llm_merge_guarded"] == 0
        assert result.details["llm_merge_rescued"] == 0
        dedup_phase._execute_merge.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dense_cluster_every_judge_merge_executes(self, dedup_phase):
        """AC regression: >10 members, all mutual near-duplicates; the
        saturated candidate search only surfaces chain neighbors, yet every
        judge-proposed merge executes — no false-positive vetoes."""
        config = _make_config(threshold=0.92)
        # UUID(int=i) stringifies in index order → deterministic split.
        mems = [_make_memory(memory_id=UUID(int=i), summary=f"standup {i}") for i in range(12)]
        dedup_phase._fetch_active_memories = AsyncMock(return_value=mems)
        # Saturated discovery: each memory only surfaces its chain neighbor.
        chain = [(mems[i].id, mems[i + 1].id, 0.99) for i in range(11)]
        dedup_phase._find_similar_pairs = AsyncMock(return_value=chain)
        # All 12 are true near-duplicates of one fact: identical vectors.
        dedup_phase._summary_vectors = {m.id: [1.0, 0.0] for m in mems}
        dedup_phase._execute_merge = AsyncMock()

        async def _nominate_star(cluster_memories, *_args, **_kwargs):
            ordered = sorted(cluster_memories, key=lambda m: str(m.id))
            return [(ordered[0].id, m.id) for m in ordered[1:]]

        dedup_phase._judge_cluster = AsyncMock(side_effect=_nominate_star)

        result = await dedup_phase.execute(config, "user-1", "ws-1", "ctx-1", SleepBudget())

        # MAX_CLUSTER_SIZE=5 splits the 12-chain into {0-4}, {5-9}, {10,11}:
        # 4 + 4 + 1 star nominations, all of which execute.
        assert result.details["merged"] == 9
        assert result.details["llm_merge_guarded"] == 0
        assert result.details["llm_merge_unverifiable"] == 0
        # Off-chain nominations (0,2),(0,3),(0,4),(5,7),(5,8),(5,9) are
        # absent from pair_scores → rescued by the direct check.
        assert result.details["llm_merge_rescued"] == 6
        assert dedup_phase._execute_merge.await_count == 9


class TestSettledPairRenomination:
    """#1232: shadow mode strips already-settled pairs (existing supersedes
    edge) from the candidate list BEFORE pair_scores is built, but members
    can still co-cluster via third parties and the judge may re-nominate
    the settled pair. The skip is correct — but it must be counted under
    its own key (``settled_decisions_skipped``), not ``llm_merge_guarded``,
    and #1231's on-demand direct check must NOT rescue it into a re-merge:
    in shadow mode that would re-write audit rows every run and corrupt
    undo (the re-merge takes no prior_edge snapshot, so rolling back the
    later run deletes the edge the first run legitimately created).
    """

    @pytest.mark.asyncio
    async def test_settled_renomination_skipped_not_guarded_not_rescued(self, dedup_phase, mock_db):
        config = _make_config()
        config.sleep_dedup_supersede_enabled = True
        mem_a = _make_memory(summary="fact v2")
        mem_b = _make_memory(summary="fact v1")
        mem_c = _make_memory(summary="fact v1.5")
        dedup_phase._fetch_active_memories = AsyncMock(return_value=[mem_a, mem_b, mem_c])
        # a-b is already settled; discovery still surfaces it plus the
        # third-party chains that re-cluster a and b through c.
        dedup_phase._find_similar_pairs = AsyncMock(
            return_value=[
                (mem_a.id, mem_b.id, 0.95),
                (mem_a.id, mem_c.id, 0.95),
                (mem_b.id, mem_c.id, 0.95),
            ]
        )
        # All three embedded: the direct check COULD compute cos(a, b)=1.0 —
        # the settled guard must fire before any rescue.
        dedup_phase._summary_vectors = {m.id: [1.0, 0.0] for m in (mem_a, mem_b, mem_c)}
        # Real _filter_already_superseded_pairs runs: the supersedes-edge
        # query returns the settled (a, b) row.
        edge_rows = MagicMock()
        edge_rows.all.return_value = [(mem_a.id, mem_b.id)]
        mock_db.execute.return_value = edge_rows
        dedup_phase._execute_shadow_merge = AsyncMock()
        dedup_phase._execute_merge = AsyncMock()
        dedup_phase._judge_cluster = AsyncMock(return_value=[(mem_a.id, mem_b.id)])

        result = await dedup_phase.execute(config, "user-1", "ws-1", "ctx-1", SleepBudget())

        assert result.success is True
        assert result.details["merged"] == 0
        assert result.details["settled_pairs_skipped"] == 1  # candidate filter
        assert result.details["settled_decisions_skipped"] == 1  # judge veto
        assert result.details["llm_merge_guarded"] == 0
        assert result.details["llm_merge_rescued"] == 0
        assert result.details["llm_merge_unverifiable"] == 0
        dedup_phase._execute_shadow_merge.assert_not_awaited()
        dedup_phase._execute_merge.assert_not_awaited()


class TestDirectPairSimilarity:
    """#1231: the on-demand cosine used when a judge-nominated pair is
    missing from pair_scores. Fail-closed: any unavailable or degenerate
    input returns None (the caller vetoes), never a fabricated score."""

    def test_identical_vectors_score_one(self, dedup_phase):
        a, b = uuid4(), uuid4()
        dedup_phase._summary_vectors = {a: [0.6, 0.8], b: [0.6, 0.8]}
        assert dedup_phase._direct_pair_similarity(a, b) == pytest.approx(1.0)

    def test_orthogonal_vectors_score_zero(self, dedup_phase):
        a, b = uuid4(), uuid4()
        dedup_phase._summary_vectors = {a: [1.0, 0.0], b: [0.0, 1.0]}
        assert dedup_phase._direct_pair_similarity(a, b) == pytest.approx(0.0)

    def test_unnormalized_vectors_use_full_cosine(self, dedup_phase):
        """Defensive: do not assume unit-norm embeddings — compute
        dot/(|a||b|) so the score matches Qdrant's cosine definition."""
        a, b = uuid4(), uuid4()
        dedup_phase._summary_vectors = {a: [2.0, 0.0], b: [0.5, 0.0]}
        assert dedup_phase._direct_pair_similarity(a, b) == pytest.approx(1.0)

    def test_missing_vector_returns_none(self, dedup_phase):
        a, b = uuid4(), uuid4()
        dedup_phase._summary_vectors = {a: [1.0, 0.0]}
        assert dedup_phase._direct_pair_similarity(a, b) is None

    def test_zero_norm_returns_none(self, dedup_phase):
        a, b = uuid4(), uuid4()
        dedup_phase._summary_vectors = {a: [0.0, 0.0], b: [1.0, 0.0]}
        assert dedup_phase._direct_pair_similarity(a, b) is None

    def test_dimension_mismatch_returns_none(self, dedup_phase):
        a, b = uuid4(), uuid4()
        dedup_phase._summary_vectors = {a: [1.0, 0.0, 0.0], b: [1.0, 0.0]}
        assert dedup_phase._direct_pair_similarity(a, b) is None

    def test_nan_component_returns_none(self, dedup_phase):
        """A NaN component (degraded embedding provider) must not fail
        OPEN: cos=NaN makes `NaN < threshold` False, which would wave the
        merge through with an unknowable score and write NaN into the
        audit JSON. Non-finite → None → unverifiable veto."""
        a, b = uuid4(), uuid4()
        dedup_phase._summary_vectors = {a: [math.nan, 1.0], b: [1.0, 0.0]}
        assert dedup_phase._direct_pair_similarity(a, b) is None

    def test_inf_component_returns_none(self, dedup_phase):
        a, b = uuid4(), uuid4()
        dedup_phase._summary_vectors = {a: [math.inf, 0.0], b: [1.0, 0.0]}
        assert dedup_phase._direct_pair_similarity(a, b) is None
