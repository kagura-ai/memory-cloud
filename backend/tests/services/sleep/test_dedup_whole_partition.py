"""#3019 point 3: dedup compares a memory with its whole context.

A vector-search hit outside the window is no longer dropped: an id-only check
under the window's own eligibility predicate (plus a successful embedding)
decides, before clustering. Full rows and stored vectors are loaded only for
the members of a cluster about to be judged; the direct-score veto and the
locked recheck apply to them as to window rows.
"""

from __future__ import annotations

import re
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services.sleep.dedup_merge import DedupMergePhase
from services.sleep.reporter import SleepBudget
from utils.datetime import utcnow

# A memory line of the judge prompt: its label and the first line of its summary.
_MEMORY_LINE = r"\[(\w)\][^\n]*\n\s+summary:\n\[BEGIN UNTRUSTED MEMORY CONTENT\]\n([^\n]+)"


def _memory(**attrs):
    m = MagicMock()
    m.id = uuid4()
    m.summary = attrs.pop("summary", "s")
    m.type = "note"
    m.importance = attrs.pop("importance", 0.5)
    m.tags = []
    m.access_count = 0
    m.created_at = attrs.pop("created_at", utcnow() - timedelta(days=1))
    m.updated_at = None
    m.source_type = None
    m.summary_embedding_id = None
    for key, value in attrs.items():
        setattr(m, key, value)
    return m


def _config(provider=""):
    config = MagicMock()
    config.sleep_dedup_enabled = True
    config.sleep_dedup_similarity_threshold = 0.92
    config.sleep_dedup_supersede_enabled = False
    config.sleep_llm_provider = provider
    config.sleep_llm_model = "m"
    config.sleep_max_memories_per_run = 500
    config.sleep_archive_exempt_tags = []
    return config


def _response(parsed):
    resp = MagicMock()
    resp.parsed = parsed
    resp.total_tokens = 10
    resp.input_tokens = 6
    resp.output_tokens = 4
    resp.cached_input_tokens = 0
    resp.cache_write_tokens = 0
    resp.provider = "openai"
    resp.model = "m"
    resp.tokenizer_version = None
    return resp


@pytest.fixture
def phase():
    with (
        patch("services.sleep.dedup_merge.NeuralEdgeRepository"),
        patch("services.sleep.dedup_merge.EmbeddingService"),
    ):
        p = DedupMergePhase(AsyncMock(), AsyncMock())
    p.edge_repo = AsyncMock()
    p.embedding_service = AsyncMock()
    p.embedding_service.embed_with_usage = AsyncMock(return_value=([1.0, 0.0], 1))
    p._count_pinned = AsyncMock(return_value=0)
    p._eligible_outside_ids = AsyncMock(return_value=set())
    return p


def _loader(phase, rows, vectors=None):
    """Stub the member load: ``rows`` load, ``vectors`` are their stored vectors."""

    async def load(ids, user_id, workspace_id, context_id):
        loaded = [row for row in rows if row.id in set(ids)]
        for row in loaded:
            if vectors and row.id in vectors:
                phase._summary_vectors[row.id] = vectors[row.id]
        return loaded

    phase._load_cluster_members = AsyncMock(side_effect=load)


class TestCandidateSearch:
    @pytest.mark.asyncio
    async def test_an_eligible_outside_hit_joins_and_another_is_dropped(self, phase):
        a = _memory()
        old, exempt = uuid4(), uuid4()
        phase._eligible_outside_ids = AsyncMock(return_value={old})
        hits = [
            {"id": str(a.id), "score": 1.0},
            {"id": str(old), "score": 0.95},
            {"id": str(exempt), "score": 0.96},
        ]
        with patch(
            "services.sleep.dedup_merge.search_memories_qdrant", AsyncMock(return_value=hits)
        ):
            pairs = await phase._find_similar_pairs([a], "u", None, None, 0.92)

        assert pairs == [(a.id, old, 0.95)]
        assert phase._eligible_outside_ids.await_count == 1
        assert set(phase._eligible_outside_ids.await_args.args[0]) == {old, exempt}
        assert phase._search_reasons()["neighbour_not_eligible"] == 1

    @pytest.mark.asyncio
    async def test_an_ineligible_hit_does_not_glue_two_clusters(self, phase):
        """The check runs before clustering: a pinned row both a and b hit
        does not chain them into one cluster."""
        a, b = _memory(), _memory()
        pinned = uuid4()
        hits = {
            a.id: [{"id": str(pinned), "score": 0.95}],
            b.id: [{"id": str(pinned), "score": 0.95}],
        }
        phase.embedding_service.embed_with_usage = AsyncMock(
            side_effect=[([1.0, 0.0], 1), ([0.0, 1.0], 1)]
        )
        order = iter([a.id, b.id])

        async def search(**kwargs):
            return hits[next(order)]

        with patch("services.sleep.dedup_merge.search_memories_qdrant", search):
            pairs = await phase._find_similar_pairs([a, b], "u", None, None, 0.92)

        assert pairs == []
        assert phase._search_reasons()["neighbour_not_eligible"] == 2


class TestExecute:
    @pytest.mark.asyncio
    async def test_an_outside_row_can_lose_the_merge(self, phase):
        new = _memory(importance=0.5)
        old = _memory(importance=0.5, created_at=utcnow() - timedelta(days=200))
        phase._fetch_active_memories = AsyncMock(return_value=[new])
        phase._find_similar_pairs = AsyncMock(return_value=[(new.id, old.id, 0.99)])
        _loader(phase, [old], {old.id: [1.0, 0.0]})
        phase._summary_vectors = {new.id: [1.0, 0.0]}
        phase._execute_merge = AsyncMock(return_value=True)

        result = await phase.execute(_config(), "u", None, None, SleepBudget())

        winner, loser = phase._execute_merge.await_args.args[:2]
        assert (winner.id, loser.id) == (new.id, old.id)
        assert result.details["merged"] == 1

    @pytest.mark.asyncio
    async def test_a_member_that_no_longer_loads_is_left_out(self, phase):
        a, b = _memory(), _memory()
        gone = uuid4()
        phase._fetch_active_memories = AsyncMock(return_value=[a, b])
        phase._find_similar_pairs = AsyncMock(return_value=[(a.id, b.id, 0.95), (a.id, gone, 0.95)])
        _loader(phase, [])
        judged = []

        async def judge(cluster_memories, *args, **kwargs):
            judged.append({m.id for m in cluster_memories})
            return []

        phase._judge_cluster = judge

        await phase.execute(_config(), "u", None, None, SleepBudget())

        assert judged == [{a.id, b.id}]

    @pytest.mark.asyncio
    async def test_two_outside_rows_below_the_threshold_are_vetoed(self, phase):
        result = await self._judge_picks_the_outside_pair(
            phase, vectors=lambda o1, o2: {o1.id: [1.0, 0.0], o2.id: [0.0, 1.0]}
        )
        assert result.details["llm_merge_guarded"] == 1
        assert result.details["merged"] == 0

    @pytest.mark.asyncio
    async def test_two_outside_rows_without_vectors_are_unverifiable(self, phase):
        result = await self._judge_picks_the_outside_pair(phase, vectors=lambda o1, o2: {})
        assert result.details["llm_merge_unverifiable"] == 1
        assert result.details["merged"] == 0

    async def _judge_picks_the_outside_pair(self, phase, vectors):
        a = _memory()
        o1, o2 = _memory(), _memory()
        phase._fetch_active_memories = AsyncMock(return_value=[a])
        phase._find_similar_pairs = AsyncMock(
            return_value=[(a.id, o1.id, 0.95), (a.id, o2.id, 0.95)]
        )
        phase._summary_vectors = {a.id: [1.0, 0.0]}
        _loader(phase, [o1, o2], vectors(o1, o2))
        phase._execute_merge = AsyncMock(return_value=True)
        labels = {}

        def respond(**kwargs):
            # The judge names the pair of the two outside rows: the one the
            # candidate search did not score (both are a's neighbours).
            for label, summary in re.findall(_MEMORY_LINE, kwargs["prompt"]):
                labels[summary] = label
            outside = [labels["outside-1"], labels["outside-2"]]
            return _response(
                {"judgments": [{"pair": outside, "verdict": "merge", "winner": outside[0]}]}
            )

        o1.summary, o2.summary, a.summary = "outside-1", "outside-2", "window-a"
        phase.llm_service.complete_json = AsyncMock(side_effect=respond)
        result = await phase.execute(_config(provider="openai"), "u", None, None, SleepBudget())
        phase._execute_merge.assert_not_awaited()
        return result

    @pytest.mark.asyncio
    async def test_members_are_loaded_only_for_judged_clusters(self, phase):
        """One call and three clusters, each with an outside member: only the
        judged cluster's member is loaded."""
        rows = [_memory() for _ in range(3)]
        outside = [_memory() for _ in range(3)]
        phase._fetch_active_memories = AsyncMock(return_value=rows)
        phase._find_similar_pairs = AsyncMock(
            return_value=[
                (row.id, other.id, 0.95) for row, other in zip(rows, outside, strict=True)
            ]
        )
        _loader(phase, outside)
        phase.llm_service.complete_json = AsyncMock(return_value=_response({"judgments": []}))

        result = await phase.execute(
            _config(provider="openai"), "u", None, None, SleepBudget(max_llm_calls=1)
        )

        assert phase._load_cluster_members.await_count == 1
        assert len(phase._load_cluster_members.await_args.args[0]) == 1
        assert result.details["no_action_reasons"]["not_judged_budget"] == 2

    @pytest.mark.asyncio
    async def test_one_window_row_is_searched(self, phase):
        a = _memory()
        phase._fetch_active_memories = AsyncMock(return_value=[a])
        phase._find_similar_pairs = AsyncMock(return_value=[])

        result = await phase.execute(_config(), "u", None, None, SleepBudget())

        phase._find_similar_pairs.assert_awaited_once()
        assert result.details["message"] == "no_duplicate_candidates"

    @pytest.mark.asyncio
    async def test_an_empty_window_is_not_enough(self, phase):
        phase._fetch_active_memories = AsyncMock(return_value=[])
        result = await phase.execute(_config(), "u", None, None, SleepBudget())
        assert result.details["message"] == "not_enough_memories"
        assert result.details["count"] == 0
