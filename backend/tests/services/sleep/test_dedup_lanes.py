"""#3019: dedup's two lanes inside the phase — judge order and row outcomes.

The window is stubbed here (rows of each lane given directly); the lane
statements themselves run on Postgres in ``test_dedup_merge_coverage.py`` and
``tests/integration/test_3019_sleep_coverage_db.py``.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services.sleep.coverage import (
    BUDGET,
    FAILURE,
    PASSED,
    SETTLED,
    DedupCoverage,
    DedupLanes,
    advance_dedup,
    changed_key,
    sweep_key,
)
from services.sleep.dedup_merge import DedupMergePhase
from services.sleep.reporter import SleepBudget
from utils.datetime import utcnow
from utils.exceptions import (
    ConfigurationError,
    EmbeddingSpendCapExceeded,
    FeatureNotAvailableError,
)


def _row(days_ago: float):
    m = MagicMock()
    m.id = uuid4()
    m.summary = f"row {m.id.hex[:6]}"
    m.type = "note"
    m.importance = 0.5
    m.tags = []
    m.access_count = 0
    m.created_at = utcnow() - timedelta(days=days_ago)
    m.updated_at = None
    m.source_type = None
    return m


def _config(provider="openai"):
    config = MagicMock()
    config.sleep_dedup_enabled = True
    config.sleep_dedup_similarity_threshold = 0.92
    config.sleep_dedup_supersede_enabled = False
    config.sleep_llm_provider = provider
    config.sleep_llm_model = "m"
    config.sleep_max_memories_per_run = 500
    config.sleep_archive_exempt_tags = []
    return config


def _phase(lanes: DedupLanes | None = None) -> DedupMergePhase:
    with (
        patch("services.sleep.dedup_merge.NeuralEdgeRepository"),
        patch("services.sleep.dedup_merge.EmbeddingService"),
    ):
        phase = DedupMergePhase(AsyncMock(), AsyncMock(), coverage=lanes)
    phase.edge_repo = AsyncMock()
    phase.embedding_service = AsyncMock()
    phase._count_pinned = AsyncMock(return_value=0)
    phase._eligible_outside_ids = AsyncMock(return_value=set())
    phase._load_cluster_members = AsyncMock(return_value=[])
    return phase


def _window(phase, changed, sweep, *, floor=None, exhausted=False):
    """Stub the fetch: ``changed`` in lane order (oldest first), then ``sweep``."""

    async def fetch(user_id, workspace_id, context_id, limit=500):
        phase.coverage_outcome = DedupCoverage(
            changed=[changed_key(m) for m in changed],
            sweep=[sweep_key(m) for m in sweep],
            floor=floor,
            exhausted=exhausted,
            outcomes=dict.fromkeys((m.id for m in [*changed, *sweep]), FAILURE),
        )
        return [*changed, *sweep]

    phase._fetch_active_memories = fetch


def _answered(outcome, *, from_cache: bool) -> None:
    """Say where an embed's vector came from, as ``EmbeddingService`` does."""
    if outcome is not None:
        outcome["cache_hit"] = from_cache


def _pairs(*clusters):
    """Candidate pairs chaining each group of rows into one cluster."""
    return [
        (a.id, b.id, 0.95) for group in clusters for a, b in zip(group, group[1:], strict=False)
    ]


def _judge_recorder(phase, *, failing=()):
    """Stub the judge: record each cluster, spend one call, answer or fail."""
    judged: list[frozenset] = []

    async def judge(
        cluster_memories,
        pair_scores,
        llm_enabled,
        user_id,
        context_id,
        workspace_id,
        budget,
        config,
    ):
        ids = frozenset(m.id for m in cluster_memories)
        judged.append(ids)
        if llm_enabled:
            budget.consume(llm_calls=1)
        if ids & set(failing):
            phase._llm_failures += 1
        return []

    phase._judge_cluster = judge
    return judged


async def _run(phase, *, calls=50, provider="openai"):
    return await phase.execute(_config(provider), "u", None, None, SleepBudget(max_llm_calls=calls))


class TestJudgeStages:
    @pytest.mark.asyncio
    async def test_the_sweep_keeps_its_share_of_the_calls(self):
        """Two calls: the changed lane's head cluster, then the sweep's; the
        rest of the changed lane waits for budget."""
        c = [_row(1 - i * 0.1) for i in range(4)]
        s = [_row(50 + i) for i in range(2)]
        phase = _phase()
        _window(phase, c, s)
        phase._find_similar_pairs = AsyncMock(return_value=_pairs(c[:2], c[2:], s))
        judged = _judge_recorder(phase)

        result = await _run(phase, calls=2)

        assert judged == [frozenset({c[0].id, c[1].id}), frozenset({s[0].id, s[1].id})]
        outcomes = phase.coverage_outcome.outcomes
        assert [outcomes[m.id] for m in c] == [SETTLED, SETTLED, BUDGET, BUDGET]
        assert [outcomes[m.id] for m in s] == [SETTLED, SETTLED]
        assert result.details["no_action_reasons"]["not_judged_budget"] == 1
        assert result.details["budget_cut"] is True

    @pytest.mark.asyncio
    async def test_stages_judge_changed_then_sweep_then_the_rest_of_changed(self):
        c = [_row(1 - i * 0.1) for i in range(6)]
        s = [_row(50 + i) for i in range(2)]
        phase = _phase()
        _window(phase, c, s)
        phase._find_similar_pairs = AsyncMock(return_value=_pairs(c[:2], c[2:4], c[4:], s))
        judged = _judge_recorder(phase)

        await _run(phase, calls=4)

        assert judged == [
            frozenset({c[0].id, c[1].id}),
            frozenset({c[2].id, c[3].id}),
            frozenset({c[4].id, c[5].id}),
            frozenset({s[0].id, s[1].id}),
        ]

    @pytest.mark.asyncio
    async def test_a_cluster_of_both_lanes_is_judged_once(self):
        c = [_row(1)]
        s = [_row(50)]
        phase = _phase()
        _window(phase, c, s)
        phase._find_similar_pairs = AsyncMock(return_value=_pairs([c[0], s[0]]))
        judged = _judge_recorder(phase)

        await _run(phase, calls=4)

        assert judged == [frozenset({c[0].id, s[0].id})]

    @pytest.mark.asyncio
    async def test_the_rule_path_judges_every_cluster_changed_first(self):
        c = [_row(1 - i * 0.1) for i in range(4)]
        s = [_row(50 + i) for i in range(2)]
        phase = _phase()
        _window(phase, c, s)
        phase._find_similar_pairs = AsyncMock(return_value=_pairs(s, c[2:], c[:2]))
        judged = _judge_recorder(phase)

        await _run(phase, provider="")

        assert judged == [
            frozenset({c[0].id, c[1].id}),
            frozenset({c[2].id, c[3].id}),
            frozenset({s[0].id, s[1].id}),
        ]

    @pytest.mark.asyncio
    async def test_one_call_the_lanes_take_turns(self):
        """The starvation case: a changed lane with more clusters than calls.
        With one call the changed lane's head is judged on one run, the
        sweep's head on the next."""
        c = [_row(1 - i * 0.1) for i in range(4)]
        s = [_row(50 + i) for i in range(2)]
        lanes = DedupLanes()
        runs = []
        for _ in range(2):
            phase = _phase(lanes)
            _window(phase, c, s)
            phase._find_similar_pairs = AsyncMock(return_value=_pairs(c[:2], c[2:], s))
            judged = _judge_recorder(phase)
            result = await _run(phase, calls=1)
            runs.append(judged)
            lanes, _ = advance_dedup(
                lanes, phase.coverage_outcome, returned=result.success, now=utcnow()
            )

        assert runs == [[frozenset({c[0].id, c[1].id})], [frozenset({s[0].id, s[1].id})]]

    @pytest.mark.asyncio
    async def test_without_a_window_clusters_are_judged_in_list_order(self):
        rows = [_row(i) for i in range(4)]
        phase = _phase()
        phase._fetch_active_memories = AsyncMock(return_value=rows)
        phase._find_similar_pairs = AsyncMock(return_value=_pairs(rows[2:], rows[:2]))
        judged = _judge_recorder(phase)

        await _run(phase, calls=1)

        assert judged == [frozenset({rows[0].id, rows[1].id})]
        assert phase.coverage_outcome is None


class TestOutcomes:
    @pytest.mark.asyncio
    async def test_a_failed_judge_call_passes_when_another_answered(self):
        c = [_row(1 - i * 0.1) for i in range(4)]
        phase = _phase()
        _window(phase, c, [])
        phase._find_similar_pairs = AsyncMock(return_value=_pairs(c[:2], c[2:]))
        _judge_recorder(phase, failing=[c[0].id])

        await _run(phase)

        outcomes = phase.coverage_outcome.outcomes
        assert [outcomes[m.id] for m in c] == [PASSED, PASSED, SETTLED, SETTLED]

    @pytest.mark.asyncio
    async def test_judge_outage_is_a_failure(self):
        c = [_row(1 - i * 0.1) for i in range(4)]
        phase = _phase()
        _window(phase, c, [])
        phase._find_similar_pairs = AsyncMock(return_value=_pairs(c[:2], c[2:]))
        _judge_recorder(phase, failing=[m.id for m in c])

        await _run(phase)

        assert set(phase.coverage_outcome.outcomes.values()) == {FAILURE}

    @pytest.mark.asyncio
    async def test_a_row_without_a_pair_settles_and_a_failed_search_passes(self):
        lonely, broken = _row(2), _row(1)
        phase = _phase()
        _window(phase, [lonely, broken], [])

        async def embed(text, *, outcome=None, **kwargs):
            if text == broken.summary:
                raise RuntimeError("too long for the provider")
            _answered(outcome, from_cache=False)
            return ([1.0, 0.0], 1)

        phase.embedding_service.embed_with_usage = AsyncMock(side_effect=embed)
        with patch("services.sleep.dedup_merge.search_memories_qdrant", AsyncMock(return_value=[])):
            result = await _run(phase)

        assert result.details["message"] == "no_duplicate_candidates"
        assert phase.coverage_outcome.outcomes == {lonely.id: SETTLED, broken.id: PASSED}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("source", ["cache", "unknown"])
    async def test_an_embed_the_provider_did_not_answer_is_no_evidence(self, source):
        """A vector from the cache says nothing about the provider: on a night
        it is down, the rows written in the last day are still searched from
        the cache, and a failed embed must hold the lane all the same. An
        embed that does not say where its vector came from counts the same."""
        cached, broken = _row(2), _row(1)
        phase = _phase()
        _window(phase, [cached, broken], [])

        async def embed(text, *, outcome=None, **kwargs):
            if text == broken.summary:
                raise RuntimeError("provider down")
            if source == "cache":
                _answered(outcome, from_cache=True)
            return ([1.0, 0.0], 0)

        phase.embedding_service.embed_with_usage = AsyncMock(side_effect=embed)
        with patch("services.sleep.dedup_merge.search_memories_qdrant", AsyncMock(return_value=[])):
            await _run(phase)

        assert phase.coverage_outcome.outcomes == {cached.id: SETTLED, broken.id: FAILURE}

    @pytest.mark.asyncio
    async def test_a_failed_vector_search_passes_when_another_search_answered(self):
        """The evidence is per step: a cached row's search proves the vector
        store answered, so a search that raised for another row is its own."""
        cached, broken = _row(2), _row(1)
        phase = _phase()
        _window(phase, [cached, broken], [])

        async def embed(text, *, outcome=None, **kwargs):
            _answered(outcome, from_cache=True)
            return ([1.0, 0.0] if text == cached.summary else [0.0, 1.0], 0)

        async def search(*, query_vector, **kwargs):
            if query_vector == [0.0, 1.0]:
                raise RuntimeError("search timed out")
            return []

        phase.embedding_service.embed_with_usage = AsyncMock(side_effect=embed)
        with patch(
            "services.sleep.dedup_merge.search_memories_qdrant", AsyncMock(side_effect=search)
        ):
            await _run(phase)

        assert phase.coverage_outcome.outcomes == {cached.id: SETTLED, broken.id: PASSED}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "refusal",
        [
            EmbeddingSpendCapExceeded(period="daily", cap_usd=1.0, current_usd=1.0),
            ConfigurationError("no key for the provider"),
            FeatureNotAvailableError(),
        ],
        ids=["spend_cap", "configuration", "plan"],
    )
    async def test_a_refusal_that_is_never_one_rows_own_holds_the_lane(self, refusal):
        """The spend cap reached partway through the window (or a provider key
        or plan that went away) refuses every later row: those rows hold the
        lane although the provider answered for earlier ones."""
        answered, refused = _row(2), _row(1)
        phase = _phase()
        _window(phase, [answered, refused], [])

        async def embed(text, *, outcome=None, **kwargs):
            if text == refused.summary:
                raise refusal
            _answered(outcome, from_cache=False)
            return ([1.0, 0.0], 1)

        phase.embedding_service.embed_with_usage = AsyncMock(side_effect=embed)
        with patch("services.sleep.dedup_merge.search_memories_qdrant", AsyncMock(return_value=[])):
            result = await _run(phase)

        assert phase.coverage_outcome.outcomes == {answered.id: SETTLED, refused.id: FAILURE}
        assert result.details["no_action_reasons"]["search_failed"] == 1

    @pytest.mark.asyncio
    async def test_a_provider_outage_moves_no_lane(self):
        """End to end through the plan: the changed lane's rows embed from the
        cache, every sweep row needs the provider and fails. The changed lane
        moves; the sweep stalls instead of moving past rows it never searched."""
        changed = [_row(0.5)]
        sweep = [_row(30), _row(31)]
        phase = _phase()
        _window(phase, changed, sweep)

        async def embed(text, *, outcome=None, **kwargs):
            if text == changed[0].summary:
                _answered(outcome, from_cache=True)
                return ([1.0, 0.0], 0)
            _answered(outcome, from_cache=False)
            raise RuntimeError("provider down")

        phase.embedding_service.embed_with_usage = AsyncMock(side_effect=embed)
        with patch("services.sleep.dedup_merge.search_memories_qdrant", AsyncMock(return_value=[])):
            await _run(phase)

        lanes, report = advance_dedup(
            DedupLanes(), phase.coverage_outcome, returned=True, now=utcnow()
        )
        assert lanes.changed.position == changed_key(changed[0])
        assert lanes.sweep.position is None
        assert lanes.sweep.stalls == 1
        assert report["sweep"]["advanced"] == 0

    @pytest.mark.asyncio
    async def test_every_search_failing_is_a_failure(self):
        rows = [_row(2), _row(1)]
        phase = _phase()
        _window(phase, rows, [])
        phase.embedding_service.embed_with_usage = AsyncMock(side_effect=RuntimeError("down"))

        await _run(phase)

        assert set(phase.coverage_outcome.outcomes.values()) == {FAILURE}

    @pytest.mark.asyncio
    async def test_a_cluster_that_could_not_load_two_members_settles(self):
        a = _row(1)
        phase = _phase()
        _window(phase, [a], [])
        phase._find_similar_pairs = AsyncMock(return_value=[(a.id, uuid4(), 0.95)])

        await _run(phase)

        assert phase.coverage_outcome.outcomes == {a.id: SETTLED}


class TestOversize:
    def _mega(self):
        rows = [_row(1 - i * 0.01) for i in range(6)]
        pairs = [(a.id, b.id, 0.99) for i, a in enumerate(rows) for b in rows[i + 1 :]]
        return rows, pairs

    @pytest.mark.asyncio
    async def test_a_merge_in_the_original_cluster_leaves_its_rows_for_next_run(self):
        rows, pairs = self._mega()
        phase = _phase()
        _window(phase, rows, [])
        phase._find_similar_pairs = AsyncMock(return_value=pairs)
        phase._execute_merge = AsyncMock(return_value=True)

        result = await _run(phase, provider="")

        assert result.details["merged"] >= 1
        assert set(phase.coverage_outcome.outcomes.values()) == {BUDGET}

    @pytest.mark.asyncio
    async def test_no_merge_in_the_original_cluster_settles_its_rows(self):
        rows, pairs = self._mega()
        phase = _phase()
        _window(phase, rows, [])
        phase._find_similar_pairs = AsyncMock(return_value=pairs)
        phase._execute_merge = AsyncMock(return_value=False)

        result = await _run(phase, provider="")

        assert result.details["merged"] == 0
        assert set(phase.coverage_outcome.outcomes.values()) == {SETTLED}
