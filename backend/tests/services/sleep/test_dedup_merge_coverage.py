"""Coverage-focused tests for Sleep Maintenance Phase 2: Dedup/Merge.

Complements ``test_dedup_merge.py`` by targeting the branches it does not
exercise: ``UnionFind`` internals (path compression, union-by-rank, self-union,
``find`` lazy-init), ``_find_similar_pairs`` filtering / dedup / exception
handling, ``_llm_judge`` (success + failure), ``_judge_cluster`` routing,
``_execute_merge`` against a REAL ``db_session`` (tag union, soft-delete, qdrant
delete failure swallow, missing winner/loser early-return), and the
``execute()`` orchestration paths (deferred clusters, budget exhaustion, reporter
audit rows, rule-based end-to-end merge).

Target module: ``services.sleep.dedup_merge``.
"""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import select

from models.memory import Memory
from services.sleep.dedup_merge import (
    AUTO_MERGE_THRESHOLD,
    MAX_CLUSTER_SIZE,
    DedupMergePhase,
    UnionFind,
)
from services.sleep.reporter import SleepBudget
from utils.datetime import utcnow

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_db():
    return AsyncMock()


@pytest.fixture
def mock_llm():
    return AsyncMock()


@pytest.fixture
def dedup_phase(mock_db, mock_llm):
    """A DedupMergePhase with collaborators mocked (no real I/O)."""
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
    config.sleep_max_memories_per_run = 500
    return config


def _make_memory(memory_id=None, summary="test", importance=0.5, tags=None, mtype="note"):
    m = MagicMock()
    m.id = memory_id or uuid4()
    m.summary = summary
    m.type = mtype
    m.importance = importance
    m.tags = tags or []
    m.access_count = 1
    return m


def _make_llm_response(parsed):
    """Build a stand-in LLMResponse exposing the fields _llm_judge reads."""
    resp = MagicMock()
    resp.parsed = parsed
    resp.total_tokens = 120
    resp.input_tokens = 80
    resp.output_tokens = 40
    resp.cached_input_tokens = 0
    resp.provider = "openai"
    resp.model = "gpt-5-nano"
    resp.tokenizer_version = "v1"
    return resp


async def _make_db_memory(db_session, *, summary="m", tags=None, importance=0.5):
    """Persist a minimal active Memory row and return it."""
    mem = Memory(
        id=uuid4(),
        user_id="dedup-user",
        summary=summary,
        content="full content",
        type="note",
        importance=importance,
        tags=tags,
        client="pytest",
        scope="working",
    )
    db_session.add(mem)
    await db_session.flush()
    return mem


# ---------------------------------------------------------------------------
# UnionFind internals
# ---------------------------------------------------------------------------


class TestUnionFindInternals:
    """Path compression, union-by-rank, lazy-init, self-union."""

    def test_find_lazy_inits_unknown_element_as_own_root(self):
        """find() on a never-seen element registers it as its own root, rank 0."""
        uf = UnionFind()
        x = uuid4()
        assert uf.find(x) == x
        assert uf.parent[x] == x
        assert uf.rank[x] == 0

    def test_self_union_is_noop(self):
        """union(x, x) keeps a single root and does not bump rank (rx == ry)."""
        uf = UnionFind()
        x = uuid4()
        uf.union(x, x)
        assert uf.find(x) == x
        assert uf.rank[x] == 0
        assert uf.clusters() == [{x}]

    def test_connected_pair_shares_root(self):
        """After union, both members resolve to the same root via find()."""
        uf = UnionFind()
        a, b = uuid4(), uuid4()
        uf.union(a, b)
        assert uf.find(a) == uf.find(b)

    def test_rank_increments_only_on_equal_rank_union(self):
        """Merging two rank-0 singletons yields a root of rank 1."""
        uf = UnionFind()
        a, b = uuid4(), uuid4()
        uf.union(a, b)
        root = uf.find(a)
        assert uf.rank[root] == 1
        # The non-root keeps rank 0.
        non_root = b if root == a else a
        assert uf.rank[non_root] == 0

    def test_union_by_rank_attaches_lower_under_higher(self):
        """A rank-1 tree absorbs a rank-0 singleton without growing in rank."""
        uf = UnionFind()
        a, b, c = uuid4(), uuid4(), uuid4()
        uf.union(a, b)  # root R has rank 1
        root_before = uf.find(a)
        uf.union(a, c)  # attach singleton c under the taller tree
        assert uf.find(c) == root_before
        # Rank unchanged because the shorter tree was attached under the taller.
        assert uf.rank[root_before] == 1

    def test_path_compression_flattens_parent_pointers(self):
        """find() rewrites parent pointers to point directly at the root."""
        uf = UnionFind()
        a, b, c = uuid4(), uuid4(), uuid4()
        uf.union(a, b)
        uf.union(b, c)
        root = uf.find(a)
        # Force compression on every element.
        for node in (a, b, c):
            uf.find(node)
        # Each element now points straight at the root (depth 1).
        for node in (a, b, c):
            assert uf.parent[node] == root

    def test_union_swaps_when_first_arg_has_lower_rank(self):
        """union(low_rank, high_rank) takes the rx<ry swap branch so the taller
        tree stays the root."""
        uf = UnionFind()
        a, b = uuid4(), uuid4()
        uf.union(a, b)  # root R has rank 1
        root = uf.find(a)
        c = uuid4()
        uf.find(c)  # register c as a rank-0 singleton
        # x=c (rank 0) < y=root (rank 1) → swap so root absorbs c.
        uf.union(c, root)
        assert uf.find(c) == root
        assert uf.rank[root] == 1  # unchanged: shorter tree attached under taller

    def test_transitive_three_then_disjoint(self):
        """A~B~C cluster plus an untouched element gives two clusters."""
        uf = UnionFind()
        a, b, c = uuid4(), uuid4(), uuid4()
        uf.union(a, b)
        uf.union(b, c)
        lonely = uf.find(uuid4())  # registers a separate singleton
        clusters = uf.clusters()
        sizes = sorted(len(s) for s in clusters)
        assert sizes == [1, 3]
        assert any(lonely in s and len(s) == 1 for s in clusters)


# ---------------------------------------------------------------------------
# _find_similar_pairs branch coverage
# ---------------------------------------------------------------------------


class TestFindSimilarPairs:
    """Hit-filtering, self-skip, out-of-set skip, dedup, exception swallow."""

    async def test_skips_self_and_unknown_hits_keeps_valid_pair(self, dedup_phase):
        a = _make_memory(summary="alpha")
        b = _make_memory(summary="beta")
        stranger = uuid4()  # not in the memory set → must be skipped
        dedup_phase.embedding_service.embed_with_usage = AsyncMock(return_value=([0.1] * 8, 5))
        dedup_phase.embedding_service.provider = "openai"
        dedup_phase.embedding_service.model = "text-embedding-3-small"

        def fake_search(**kwargs):
            return [
                {"id": str(a.id), "score": 0.99},  # self-hit → skipped
                {"id": str(stranger), "score": 0.95},  # not in set → skipped
                {"id": str(b.id), "score": 0.97},  # valid neighbour
            ]

        with patch(
            "services.sleep.dedup_merge.search_memories_qdrant",
            AsyncMock(side_effect=lambda **kw: fake_search(**kw)),
        ):
            pairs = await dedup_phase._find_similar_pairs([a, b], "u", "ws", "ctx", threshold=0.9)

        # Only (a, b) survives. b's own search returns the same list, but the
        # canonical sorted key dedups the reverse pair.
        assert len(pairs) == 1
        ida, idb, score = pairs[0]
        assert {ida, idb} == {a.id, b.id}
        assert score == 0.97

    async def test_exception_per_memory_is_swallowed_and_continues(self, dedup_phase):
        """A failing embed for one memory is logged, not raised; others proceed."""
        a = _make_memory(summary="boom")
        b = _make_memory(summary="ok")

        async def embed(summary, **kwargs):
            if summary == "boom":
                raise RuntimeError("embedding backend down")
            return ([0.2] * 8, 7)

        dedup_phase.embedding_service.embed_with_usage = AsyncMock(side_effect=embed)
        dedup_phase.embedding_service.provider = "openai"
        dedup_phase.embedding_service.model = "text-embedding-3-small"

        with patch(
            "services.sleep.dedup_merge.search_memories_qdrant",
            AsyncMock(return_value=[]),
        ):
            pairs = await dedup_phase._find_similar_pairs([a, b], "u", "ws", "ctx", threshold=0.9)

        # a raised before incrementing counters; b succeeded once.
        assert pairs == []
        assert dedup_phase._embedding_calls_used == 1
        assert dedup_phase._embedding_tokens_used == 7

    async def test_default_collection_name_used_when_none(self, dedup_phase):
        """collection_name=None falls back to 'kagura_memories' in the search call."""
        a = _make_memory(summary="x")
        dedup_phase.collection_name = None
        dedup_phase.embedding_service.embed_with_usage = AsyncMock(return_value=([0.1] * 8, 1))
        dedup_phase.embedding_service.provider = "openai"
        dedup_phase.embedding_service.model = "text-embedding-3-small"

        captured = {}

        async def fake_search(**kwargs):
            captured.update(kwargs)
            return []

        with patch("services.sleep.dedup_merge.search_memories_qdrant", fake_search):
            await dedup_phase._find_similar_pairs([a], "u", None, None, threshold=0.9)

        assert captured["collection_name"] == "kagura_memories"
        # workspace_id/context_id None coalesce to "" at the call boundary.
        assert captured["workspace_id"] == ""
        assert captured["context_id"] == ""


# ---------------------------------------------------------------------------
# _rule_based_judge + _judge_cluster routing
# ---------------------------------------------------------------------------


class TestRuleBasedJudge:
    """Auto-merge winner selection and threshold boundary."""

    def test_lower_importance_loses_winner_is_higher(self):
        phase = DedupMergePhase.__new__(DedupMergePhase)
        mem_a = _make_memory(importance=0.4)
        mem_b = _make_memory(importance=0.9)
        scores = {tuple(sorted([mem_a.id, mem_b.id], key=str)): 0.99}
        decisions = phase._rule_based_judge([mem_a, mem_b], scores)
        assert decisions == [(mem_b.id, mem_a.id)]

    def test_tie_importance_keeps_first_iteration_order(self):
        """Equal importance → mem_a (the i-loop element) wins (>= branch)."""
        phase = DedupMergePhase.__new__(DedupMergePhase)
        mem_a = _make_memory(importance=0.5)
        mem_b = _make_memory(importance=0.5)
        scores = {tuple(sorted([mem_a.id, mem_b.id], key=str)): AUTO_MERGE_THRESHOLD}
        decisions = phase._rule_based_judge([mem_a, mem_b], scores)
        assert decisions == [(mem_a.id, mem_b.id)]

    def test_missing_pair_score_defaults_to_zero_no_merge(self):
        """A pair absent from pair_scores scores 0.0 → never auto-merged."""
        phase = DedupMergePhase.__new__(DedupMergePhase)
        mem_a = _make_memory()
        mem_b = _make_memory()
        decisions = phase._rule_based_judge([mem_a, mem_b], {})
        assert decisions == []


class TestJudgeClusterRouting:
    """_judge_cluster picks LLM vs rule path on llm_enabled + budget."""

    async def test_routes_to_rule_when_llm_disabled(self, dedup_phase):
        mem_a = _make_memory(importance=0.9)
        mem_b = _make_memory(importance=0.1)
        scores = {tuple(sorted([mem_a.id, mem_b.id], key=str)): 0.99}
        budget = SleepBudget()

        decisions = await dedup_phase._judge_cluster(
            [mem_a, mem_b], scores, False, "u", "ctx", "ws", budget, _make_config()
        )
        assert decisions == [(mem_a.id, mem_b.id)]
        # LLM was never consulted.
        dedup_phase.llm_service.complete_json.assert_not_called()

    async def test_routes_to_rule_when_budget_cannot_afford_llm(self, dedup_phase):
        """llm_enabled=True but exhausted budget → rule path, no LLM call."""
        mem_a = _make_memory(importance=0.9)
        mem_b = _make_memory(importance=0.1)
        scores = {tuple(sorted([mem_a.id, mem_b.id], key=str)): 0.99}
        budget = SleepBudget(max_llm_calls=0)  # can_afford(1) is False

        decisions = await dedup_phase._judge_cluster(
            [mem_a, mem_b], scores, True, "u", "ctx", "ws", budget, _make_config()
        )
        assert decisions == [(mem_a.id, mem_b.id)]
        dedup_phase.llm_service.complete_json.assert_not_called()


# ---------------------------------------------------------------------------
# _llm_judge success + failure
# ---------------------------------------------------------------------------


class TestLLMJudge:
    """LLM merge judgment: budget consume, token accumulation, failure swallow."""

    async def test_success_consumes_budget_and_returns_decisions(self, dedup_phase):
        mem_a = _make_memory(importance=0.7, summary="dup A")
        mem_b = _make_memory(importance=0.6, summary="dup B")
        scores = {tuple(sorted([mem_a.id, mem_b.id], key=str)): 0.96}
        budget = SleepBudget()

        # The labels assigned to the cluster are A, B in order → winner "A".
        parsed = {"judgments": [{"pair": ["A", "B"], "verdict": "merge", "winner": "A"}]}
        dedup_phase.llm_service.complete_json = AsyncMock(return_value=_make_llm_response(parsed))
        dedup_phase._tokens_used = 0
        dedup_phase._llm_breakdown = None

        decisions = await dedup_phase._llm_judge(
            [mem_a, mem_b], scores, "u", "ctx", "ws", budget, _make_config()
        )

        assert decisions == [(mem_a.id, mem_b.id)]
        assert budget.llm_calls_used == 1
        assert dedup_phase._tokens_used == 120
        assert dedup_phase._llm_breakdown is not None
        assert dedup_phase._llm_breakdown.calls == 1

    async def test_llm_exception_returns_empty_and_does_not_consume(self, dedup_phase):
        mem_a = _make_memory()
        mem_b = _make_memory()
        scores = {tuple(sorted([mem_a.id, mem_b.id], key=str)): 0.96}
        budget = SleepBudget()
        dedup_phase.llm_service.complete_json = AsyncMock(side_effect=RuntimeError("LLM 500"))
        dedup_phase._tokens_used = 0
        dedup_phase._llm_breakdown = None

        decisions = await dedup_phase._llm_judge(
            [mem_a, mem_b], scores, "u", "ctx", "ws", budget, _make_config()
        )

        assert decisions == []
        assert budget.llm_calls_used == 0  # no consume on the failure path
        assert dedup_phase._tokens_used == 0


# ---------------------------------------------------------------------------
# _parse_dedup_response extra branches
# ---------------------------------------------------------------------------


class TestParseDedupResponseBranches:
    """Branches not covered by the sibling suite."""

    def test_pair_wrong_length_is_skipped(self):
        phase = DedupMergePhase.__new__(DedupMergePhase)
        label_to_id = {"A": uuid4(), "B": uuid4()}
        resp = {"judgments": [{"pair": ["A"], "verdict": "merge", "winner": "A"}]}
        assert phase._parse_dedup_response(resp, label_to_id) == []

    def test_winner_label_unknown_is_skipped(self):
        phase = DedupMergePhase.__new__(DedupMergePhase)
        label_to_id = {"A": uuid4(), "B": uuid4()}
        # Both labels valid, but the declared winner isn't a known label.
        resp = {"judgments": [{"pair": ["A", "B"], "verdict": "merge", "winner": "Z"}]}
        assert phase._parse_dedup_response(resp, label_to_id) == []

    def test_winner_is_second_element_loser_is_first(self):
        phase = DedupMergePhase.__new__(DedupMergePhase)
        id_a, id_b = uuid4(), uuid4()
        label_to_id = {"A": id_a, "B": id_b}
        resp = {"judgments": [{"pair": ["A", "B"], "verdict": "merge", "winner": "B"}]}
        # winner=B → loser is the other element A.
        assert phase._parse_dedup_response(resp, label_to_id) == [(id_b, id_a)]

    def test_non_merge_verdict_is_skipped_first(self):
        """A keep_both verdict is skipped before any label validation."""
        phase = DedupMergePhase.__new__(DedupMergePhase)
        label_to_id = {"A": uuid4(), "B": uuid4()}
        resp = {"judgments": [{"pair": ["A", "B"], "verdict": "keep_both", "winner": None}]}
        assert phase._parse_dedup_response(resp, label_to_id) == []

    def test_hallucinated_pair_label_logs_and_continues(self):
        """A pair referencing a label outside label_to_id hits the warning/continue
        branch (ID-hallucination protection) and yields no decision."""
        phase = DedupMergePhase.__new__(DedupMergePhase)
        label_to_id = {"A": uuid4(), "B": uuid4()}
        # 'Q' is not a known label → pair validation fails.
        resp = {"judgments": [{"pair": ["A", "Q"], "verdict": "merge", "winner": "A"}]}
        assert phase._parse_dedup_response(resp, label_to_id) == []

    def test_mixed_valid_and_invalid_judgments(self):
        """One valid merge survives alongside a non-merge and a hallucinated pair."""
        phase = DedupMergePhase.__new__(DedupMergePhase)
        id_a, id_b = uuid4(), uuid4()
        label_to_id = {"A": id_a, "B": id_b}
        resp = {
            "judgments": [
                {"pair": ["A", "B"], "verdict": "keep_both", "winner": None},
                {"pair": ["A", "ZZ"], "verdict": "merge", "winner": "A"},
                {"pair": ["A", "B"], "verdict": "merge", "winner": "A"},
            ]
        }
        assert phase._parse_dedup_response(resp, label_to_id) == [(id_a, id_b)]


# ---------------------------------------------------------------------------
# _execute_merge against a real db_session
# ---------------------------------------------------------------------------


class TestExecuteMergeRealDB:
    """Soft-delete, tag union, qdrant-delete failure swallow, edge transfer."""

    async def _phase_for_db(self, db_session):
        with (
            patch("services.sleep.dedup_merge.NeuralEdgeRepository"),
            patch("services.sleep.dedup_merge.EmbeddingService"),
        ):
            phase = DedupMergePhase(db_session, AsyncMock())
        phase.edge_repo = AsyncMock()
        phase.edge_repo.transfer_edges = AsyncMock(return_value=0)
        return phase

    async def test_merge_unions_tags_and_soft_deletes_loser(self, db_session):
        winner = await _make_db_memory(db_session, summary="w", tags=["a", "b"])
        loser = await _make_db_memory(db_session, summary="l", tags=["b", "c"])
        phase = await self._phase_for_db(db_session)

        with patch(
            "services.sleep.dedup_merge.delete_memory_from_qdrant",
            new_callable=AsyncMock,
        ) as del_qdrant:
            await phase._execute_merge(winner, loser, "dedup-user", None, None)

        # Re-read column values directly. A scalar-column select avoids any
        # lazy ORM attribute reload outside the async greenlet context.
        w_tags = (
            await db_session.execute(select(Memory.tags).where(Memory.id == winner.id))
        ).scalar_one()
        loser_row = (
            await db_session.execute(
                select(Memory.deleted_at, Memory.deleted_by).where(Memory.id == loser.id)
            )
        ).one()

        assert set(w_tags) == {"a", "b", "c"}  # union of both tag sets
        assert loser_row.deleted_at is not None
        assert loser_row.deleted_by == "sleep_maintenance"
        del_qdrant.assert_awaited_once()
        phase.edge_repo.transfer_edges.assert_awaited_once()

    async def test_qdrant_delete_failure_is_swallowed_merge_still_completes(self, db_session):
        winner = await _make_db_memory(db_session, summary="w2", tags=["x"])
        loser = await _make_db_memory(db_session, summary="l2", tags=None)
        phase = await self._phase_for_db(db_session)

        with patch(
            "services.sleep.dedup_merge.delete_memory_from_qdrant",
            new_callable=AsyncMock,
            side_effect=RuntimeError("qdrant unreachable"),
        ):
            # Must NOT raise — failure is logged and the merge proceeds.
            await phase._execute_merge(winner, loser, "dedup-user", "ws", "ctx")

        loser_deleted_at = (
            await db_session.execute(select(Memory.deleted_at).where(Memory.id == loser.id))
        ).scalar_one()
        assert loser_deleted_at is not None  # soft-delete happened before qdrant call
        # Edge transfer still runs after the swallowed qdrant failure.
        phase.edge_repo.transfer_edges.assert_awaited_once()

    async def test_none_winner_or_loser_is_noop(self, db_session):
        phase = await self._phase_for_db(db_session)
        with patch(
            "services.sleep.dedup_merge.delete_memory_from_qdrant",
            new_callable=AsyncMock,
        ) as del_qdrant:
            await phase._execute_merge(None, _make_memory(), "u", None, None)
            await phase._execute_merge(_make_memory(), None, "u", None, None)

        del_qdrant.assert_not_called()
        phase.edge_repo.transfer_edges.assert_not_called()


# ---------------------------------------------------------------------------
# execute() orchestration paths
# ---------------------------------------------------------------------------


class TestExecuteOrchestration:
    """End-to-end execute() over mocked helpers: clustering, deferral, budget,
    reporter audit rows, rule-based merge accounting."""

    async def test_rule_based_merge_end_to_end_records_details(self, dedup_phase):
        """LLM-off run: a 0.99 pair auto-merges, details + changed ids populate."""
        mem_a = _make_memory(importance=0.8, tags=["t1"])
        mem_b = _make_memory(importance=0.3, tags=["t2"])
        config = _make_config(provider="")  # LLM off → rule path
        budget = SleepBudget()

        dedup_phase._fetch_active_memories = AsyncMock(return_value=[mem_a, mem_b])
        dedup_phase._find_similar_pairs = AsyncMock(return_value=[(mem_a.id, mem_b.id, 0.99)])
        dedup_phase._execute_merge = AsyncMock()

        result = await dedup_phase.execute(config, "u", "ws", "ctx", budget)

        assert result.details["merged"] == 1
        assert result.details["clusters"] == 1
        assert result.details["candidates"] == 1
        assert result.details["deferred_clusters"] == 0
        # Higher-importance mem_a is the winner → its id is the changed one.
        assert result.changed_memory_ids == {mem_a.id}
        dedup_phase._execute_merge.assert_awaited_once()
        # No LLM tokens on the rule path.
        assert result.llm_calls_used == 0

    async def test_oversized_cluster_split_and_partially_processed(self, dedup_phase):
        """#1184: a 6-member chained cluster exceeds MAX_CLUSTER_SIZE — instead
        of wholesale deferral it is re-split into <=5-member subclusters, the
        rule path merges within them, and the observability keys expose the
        oversize volume + pairs left unjudged."""
        mems = [_make_memory(importance=0.5) for _ in range(MAX_CLUSTER_SIZE + 1)]
        config = _make_config(provider="")
        budget = SleepBudget()

        dedup_phase._fetch_active_memories = AsyncMock(return_value=mems)
        # Chain them so Union-Find collapses all into one oversized cluster.
        pairs = [(mems[i].id, mems[i + 1].id, 0.99) for i in range(len(mems) - 1)]
        dedup_phase._find_similar_pairs = AsyncMock(return_value=pairs)
        dedup_phase._execute_merge = AsyncMock()

        result = await dedup_phase.execute(config, "u", "ws", "ctx", budget)

        assert result.details["oversize_clusters"] == 1
        assert result.details["deferred_clusters"] == 1  # legacy key: original count
        assert result.details["oversize_max_size"] == MAX_CLUSTER_SIZE + 1
        # 6 chained members at cap 5: equal scores tie-break on random UUIDs,
        # so the terminal partition is (5,1), (4,2) or (3,3) — 1 or 2
        # judgeable subclusters, and always exactly one blocked chain edge.
        assert result.details["split_subclusters"] in (1, 2)
        assert result.details["clusters"] == result.details["split_subclusters"]
        assert result.details["deferred_pairs"] == 1
        # Partial progress: merges happen INSIDE the subcluster (0.99 >= auto).
        assert result.details["merged"] > 0
        dedup_phase._execute_merge.assert_awaited()

    async def test_mega_cluster_yields_multiple_subclusters(self, dedup_phase):
        """#1184 Day-5 shape: ALL memories pairwise-similar → one mega-cluster;
        splitting must yield multiple judgeable subclusters, not zero."""
        n = 12
        mems = [_make_memory(importance=0.5) for _ in range(n)]
        config = _make_config(provider="")
        budget = SleepBudget()

        dedup_phase._fetch_active_memories = AsyncMock(return_value=mems)
        # Fully-connected candidate graph (all pairs above threshold).
        pairs = [(mems[i].id, mems[j].id, 0.99) for i in range(n) for j in range(i + 1, n)]
        dedup_phase._find_similar_pairs = AsyncMock(return_value=pairs)
        dedup_phase._execute_merge = AsyncMock()

        result = await dedup_phase.execute(config, "u", "ws", "ctx", budget)

        assert result.details["oversize_clusters"] == 1
        assert result.details["oversize_max_size"] == n
        # 12 members at cap 5 on a COMPLETE equal-score graph: the greedy
        # walk always fills components to capacity before starting new ones
        # (every node pair has an edge, so a below-cap component always finds
        # a mergeable partner while one exists) → deterministically (5,5,2),
        # i.e. exactly 3 subclusters regardless of UUID tie-break order.
        assert result.details["split_subclusters"] == 3
        assert result.details["merged"] > 0
        assert result.details["deferred_pairs"] > 0

    async def test_budget_exhaustion_breaks_cluster_loop(self, dedup_phase):
        """With LLM enabled and zero budget, the can_afford guard breaks before
        any cluster is judged → no merges."""
        mem_a = _make_memory()
        mem_b = _make_memory()
        config = _make_config(provider="openai")  # llm_enabled True
        budget = SleepBudget(max_llm_calls=0)

        dedup_phase._fetch_active_memories = AsyncMock(return_value=[mem_a, mem_b])
        dedup_phase._find_similar_pairs = AsyncMock(return_value=[(mem_a.id, mem_b.id, 0.96)])
        dedup_phase._judge_cluster = AsyncMock()
        dedup_phase._execute_merge = AsyncMock()

        result = await dedup_phase.execute(config, "u", "ws", "ctx", budget)

        assert result.details["merged"] == 0
        dedup_phase._judge_cluster.assert_not_called()

    async def test_reporter_add_action_called_on_merge(self, dedup_phase):
        """When reporter + report_id are supplied, a merge writes an audit action
        carrying similarity + tag/summary metadata."""
        mem_a = _make_memory(importance=0.8, tags=["keep"])
        mem_b = _make_memory(importance=0.2, tags=["drop"])
        mem_b.summary = "loser summary text"
        config = _make_config(provider="")
        budget = SleepBudget()

        dedup_phase._fetch_active_memories = AsyncMock(return_value=[mem_a, mem_b])
        dedup_phase._find_similar_pairs = AsyncMock(return_value=[(mem_a.id, mem_b.id, 0.985)])
        dedup_phase._execute_merge = AsyncMock()

        reporter = AsyncMock()
        report_id = uuid4()

        result = await dedup_phase.execute(
            config, "u", "ws", "ctx", budget, reporter=reporter, report_id=report_id
        )

        assert result.details["merged"] == 1
        reporter.add_action.assert_awaited_once()
        kwargs = reporter.add_action.await_args.kwargs
        assert kwargs["action_type"] == "merge"
        assert kwargs["memory_id"] == mem_a.id
        assert kwargs["target_id"] == mem_b.id
        assert kwargs["details"]["similarity"] == 0.985
        assert kwargs["details"]["loser_summary"] == "loser summary text"

    async def test_cluster_with_one_known_member_is_skipped(self, dedup_phase):
        """If a cluster's members aren't all in memory_map (len<2 after filter),
        it is skipped without merging."""
        mem_a = _make_memory()
        mem_b = _make_memory()
        config = _make_config(provider="")
        budget = SleepBudget()

        # Pair references a ghost id not in the fetched memory set, so the
        # cluster {mem_a, ghost} maps to just [mem_a] → skipped.
        ghost = uuid4()
        dedup_phase._fetch_active_memories = AsyncMock(return_value=[mem_a, mem_b])
        dedup_phase._find_similar_pairs = AsyncMock(return_value=[(mem_a.id, ghost, 0.99)])
        dedup_phase._execute_merge = AsyncMock()

        result = await dedup_phase.execute(config, "u", "ws", "ctx", budget)

        assert result.details["merged"] == 0
        dedup_phase._execute_merge.assert_not_called()

    async def test_llm_enabled_run_surfaces_breakdown_and_tokens(self, dedup_phase):
        """Full LLM path through execute(): breakdown attached, llm_calls_used
        reflects budget delta, embedding usage surfaced."""
        mem_a = _make_memory(importance=0.7)
        mem_b = _make_memory(importance=0.6)
        config = _make_config(provider="openai")
        budget = SleepBudget()

        dedup_phase._fetch_active_memories = AsyncMock(return_value=[mem_a, mem_b])
        dedup_phase._find_similar_pairs = AsyncMock(return_value=[(mem_a.id, mem_b.id, 0.96)])
        dedup_phase._execute_merge = AsyncMock()
        # Simulate embedding usage having been recorded by _find_similar_pairs.
        dedup_phase.embedding_service.provider = "openai"
        dedup_phase.embedding_service.model = "text-embedding-3-small"

        async def fake_find(*args, **kwargs):
            dedup_phase._embedding_calls_used = 2
            dedup_phase._embedding_tokens_used = 30
            return [(mem_a.id, mem_b.id, 0.96)]

        dedup_phase._find_similar_pairs = AsyncMock(side_effect=fake_find)

        parsed = {"judgments": [{"pair": ["A", "B"], "verdict": "merge", "winner": "A"}]}
        dedup_phase.llm_service.complete_json = AsyncMock(return_value=_make_llm_response(parsed))

        result = await dedup_phase.execute(config, "u", "ws", "ctx", budget)

        assert result.details["merged"] == 1
        assert result.llm_calls_used == 1
        assert result.tokens_used == 120
        assert len(result.llm_breakdown) == 1
        assert result.embedding_calls_used == 2
        assert result.embedding_tokens == 30
        assert result.embedding_provider == "openai"
        assert result.embedding_model == "text-embedding-3-small"

    async def test_no_embedding_usage_leaves_provider_unset(self, dedup_phase):
        """When _find_similar_pairs records zero embedding calls, the result does
        not fabricate an embedding provider/model identity."""
        mem_a = _make_memory(importance=0.8)
        mem_b = _make_memory(importance=0.3)
        config = _make_config(provider="")
        budget = SleepBudget()

        dedup_phase._fetch_active_memories = AsyncMock(return_value=[mem_a, mem_b])
        dedup_phase._find_similar_pairs = AsyncMock(return_value=[(mem_a.id, mem_b.id, 0.99)])
        dedup_phase._execute_merge = AsyncMock()

        result = await dedup_phase.execute(config, "u", "ws", "ctx", budget)

        assert result.embedding_calls_used == 0
        assert result.embedding_provider is None
        assert result.embedding_model is None


class TestExecuteEarlyReturns:
    """The three guard early-returns in execute()."""

    async def test_dedup_disabled_skips(self, dedup_phase):
        config = _make_config(dedup_enabled=False)
        result = await dedup_phase.execute(config, "u", "ws", "ctx", SleepBudget())
        assert result.skipped is True
        assert result.skip_reason == "dedup_disabled"

    async def test_fewer_than_two_memories_returns_not_enough(self, dedup_phase):
        config = _make_config()
        dedup_phase._fetch_active_memories = AsyncMock(return_value=[_make_memory()])
        result = await dedup_phase.execute(config, "u", "ws", "ctx", SleepBudget())
        assert result.details == {"message": "not_enough_memories", "count": 1}

    async def test_no_candidate_pairs_returns_message(self, dedup_phase):
        config = _make_config()
        dedup_phase._fetch_active_memories = AsyncMock(
            return_value=[_make_memory(), _make_memory()]
        )
        dedup_phase._find_similar_pairs = AsyncMock(return_value=[])
        result = await dedup_phase.execute(config, "u", "ws", "ctx", SleepBudget())
        assert result.details == {"message": "no_duplicate_candidates"}


class TestFetchActiveMemoriesRealDB:
    """_fetch_active_memories runs a real query: active-only, isolation filters."""

    async def _phase_for_db(self, db_session):
        with (
            patch("services.sleep.dedup_merge.NeuralEdgeRepository"),
            patch("services.sleep.dedup_merge.EmbeddingService"),
        ):
            phase = DedupMergePhase(db_session, AsyncMock())
        return phase

    async def test_excludes_soft_deleted_and_other_users(self, db_session):
        user = f"fetch-user-{uuid4()}"
        active = Memory(
            id=uuid4(),
            user_id=user,
            summary="active",
            content="c",
            type="note",
            client="pytest",
            scope="working",
        )
        deleted = Memory(
            id=uuid4(),
            user_id=user,
            summary="deleted",
            content="c",
            type="note",
            client="pytest",
            scope="working",
            deleted_at=utcnow(),
            deleted_by="someone",
        )
        other = Memory(
            id=uuid4(),
            user_id=f"other-{uuid4()}",
            summary="other user",
            content="c",
            type="note",
            client="pytest",
            scope="working",
        )
        db_session.add_all([active, deleted, other])
        await db_session.flush()

        phase = await self._phase_for_db(db_session)
        rows = await phase._fetch_active_memories(user, None, None, limit=500)

        ids = {m.id for m in rows}
        assert active.id in ids
        assert deleted.id not in ids  # soft-deleted excluded
        assert other.id not in ids  # other user excluded

    async def test_workspace_and_context_filters_applied(self, db_session):
        from models.auth import Context, Workspace

        user = f"iso-user-{uuid4()}"
        ws_id = uuid4()
        ctx_id = uuid4()
        # Real workspace + context so the memories.context_id FK is satisfied.
        db_session.add(Workspace(id=ws_id, name="ws", owner_user_id=user))
        await db_session.flush()
        db_session.add(Context(id=ctx_id, workspace_id=ws_id, name="ctx"))
        await db_session.flush()

        in_scope = Memory(
            id=uuid4(),
            user_id=user,
            summary="in scope",
            content="c",
            type="note",
            client="pytest",
            scope="working",
            workspace_id=ws_id,
            context_id=ctx_id,
        )
        # Same context but a different workspace_id → excluded by the ws filter
        # (line 296). context_id is the same valid FK so only ws differs.
        wrong_ws_id = uuid4()
        db_session.add(Workspace(id=wrong_ws_id, name="ws2", owner_user_id=user))
        await db_session.flush()
        wrong_ws = Memory(
            id=uuid4(),
            user_id=user,
            summary="wrong ws",
            content="c",
            type="note",
            client="pytest",
            scope="working",
            workspace_id=wrong_ws_id,
            context_id=ctx_id,
        )
        db_session.add_all([in_scope, wrong_ws])
        await db_session.flush()

        phase = await self._phase_for_db(db_session)
        rows = await phase._fetch_active_memories(user, str(ws_id), str(ctx_id), limit=500)

        ids = {m.id for m in rows}
        assert in_scope.id in ids
        assert wrong_ws.id not in ids  # filtered out by workspace_id (line 296)


# ---------------------------------------------------------------------------
# _split_oversize_cluster (#1184)
# ---------------------------------------------------------------------------


class TestSplitOversizeCluster:
    """Deterministic unit tests for the capacity-capped greedy split."""

    def test_prefers_highest_similarity_pairs(self):
        """With distinct scores the strongest pairs coalesce first: a 6-node
        chain whose weakest link is in the middle splits exactly there."""
        from services.sleep.dedup_merge import _split_oversize_cluster

        ids = [uuid4() for _ in range(6)]
        # Chain scores: strong at the ends, weakest in the middle (0.921).
        scores = [0.99, 0.98, 0.921, 0.97, 0.96]
        pairs = [(ids[i], ids[i + 1], scores[i]) for i in range(5)]

        subclusters, skipped = _split_oversize_cluster(set(ids), pairs, max_size=3)

        # Strong halves {0,1,2} and {3,4,5} form; the weak middle link is
        # the only one blocked by the cap.
        as_sets = sorted(subclusters, key=len)
        assert {frozenset(s) for s in as_sets} == {
            frozenset(ids[:3]),
            frozenset(ids[3:]),
        }
        assert skipped == 1

    def test_every_subcluster_within_cap(self):
        from services.sleep.dedup_merge import _split_oversize_cluster

        ids = [uuid4() for _ in range(23)]
        # Fully-connected with distinct descending scores for determinism.
        pairs = []
        score = 0.999
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                pairs.append((ids[i], ids[j], score))
                score -= 0.0001
        subclusters, skipped = _split_oversize_cluster(set(ids), pairs, max_size=5)

        assert all(2 <= len(s) <= 5 for s in subclusters)
        # Every member lands in some subcluster or is a leftover singleton;
        # no member appears twice.
        seen = [m for s in subclusters for m in s]
        assert len(seen) == len(set(seen))
        assert skipped > 0

    def test_pairs_outside_cluster_ignored(self):
        from services.sleep.dedup_merge import _split_oversize_cluster

        inside = [uuid4() for _ in range(3)]
        outsider = uuid4()
        pairs = [
            (inside[0], inside[1], 0.99),
            (inside[1], inside[2], 0.98),
            (inside[0], outsider, 1.0),  # must be ignored
        ]
        subclusters, skipped = _split_oversize_cluster(set(inside), pairs, max_size=5)

        assert subclusters == [set(inside)]
        assert skipped == 0
        assert outsider not in subclusters[0]

    def test_no_internal_pairs_yields_no_subclusters(self):
        """Degenerate guard: a cluster with no internal pair edges (cannot
        happen from union-find output, but the function must not crash)."""
        from services.sleep.dedup_merge import _split_oversize_cluster

        ids = {uuid4(), uuid4()}
        subclusters, skipped = _split_oversize_cluster(ids, [], max_size=5)

        assert subclusters == []
        assert skipped == 0
