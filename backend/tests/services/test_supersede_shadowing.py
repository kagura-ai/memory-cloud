"""#1208: supersedes/contradicts relations + recall-time shadowing.

Pins the non-destructive update path:

1. **Shadow filter**: a candidate that is the dst of a LIVE supersedes edge
   is removed from the pool before the top-k slice; ``include_superseded``
   keeps it and the shadow map annotates it.
2. **Self-healing**: the liveness JOIN means a deleted superseder stops
   shadowing (simulated here by the query returning no rows for that edge).
3. **contradicts never hides** — both sides are annotated, nothing is
   filtered.
4. **Over-supersede placebo (deterministic form)**: memories NOT linked by a
   supersedes edge are never filtered — shadowing cannot leak onto distinct
   facts that merely co-occur in the results.
5. **Fail-open**: an edge-query error preserves the original results.
6. Shadow-mode dedup merge records a supersedes edge and mutates nothing;
   already-settled pairs are filtered before judging.
"""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from services.memory_service import MemoryService
from services.sleep.dedup_merge import DedupMergePhase


def _mem(mid) -> MagicMock:
    m = MagicMock()
    m.id = mid
    return m


def _service_with_edges(supersede_rows, contradict_rows) -> MemoryService:
    svc = MemoryService(MagicMock())
    db = AsyncMock()
    s_result = MagicMock()
    s_result.all.return_value = supersede_rows
    c_result = MagicMock()
    c_result.all.return_value = contradict_rows
    db.execute.side_effect = [s_result, c_result]
    svc.db = db
    return svc


@pytest.mark.asyncio
async def test_shadowed_memory_filtered_out_by_default() -> None:
    old_id, new_id, other_id = uuid4(), uuid4(), uuid4()
    memories = {str(old_id): _mem(old_id), str(other_id): _mem(other_id)}
    search_results = [
        {"id": str(old_id), "score": 0.9},
        {"id": str(other_id), "score": 0.8},
    ]
    svc = _service_with_edges([(old_id, new_id)], [])

    shadow_map, contradiction_map = await svc._apply_supersede_shadowing(
        search_results, memories, user_id="u", include_superseded=False
    )

    assert shadow_map == {old_id: new_id}
    assert [r["id"] for r in search_results] == [str(other_id)]
    assert contradiction_map == {}


@pytest.mark.asyncio
async def test_include_superseded_keeps_and_annotates() -> None:
    old_id, new_id = uuid4(), uuid4()
    memories = {str(old_id): _mem(old_id)}
    search_results = [{"id": str(old_id), "score": 0.9}]
    svc = _service_with_edges([(old_id, new_id)], [])

    shadow_map, _ = await svc._apply_supersede_shadowing(
        search_results, memories, user_id="u", include_superseded=True
    )

    assert shadow_map == {old_id: new_id}
    assert len(search_results) == 1  # kept — annotation is the caller's job


@pytest.mark.asyncio
async def test_dead_superseder_stops_shadowing() -> None:
    """Self-healing: the liveness JOIN yields no row when the superseding
    memory was deleted — the previously shadowed memory surfaces again."""
    old_id = uuid4()
    memories = {str(old_id): _mem(old_id)}
    search_results = [{"id": str(old_id), "score": 0.9}]
    # JOIN against a deleted src returns nothing.
    svc = _service_with_edges([], [])

    shadow_map, _ = await svc._apply_supersede_shadowing(
        search_results, memories, user_id="u", include_superseded=False
    )

    assert shadow_map == {}
    assert len(search_results) == 1


@pytest.mark.asyncio
async def test_contradicts_never_hides_annotates_both_sides() -> None:
    a_id, b_id = uuid4(), uuid4()
    memories = {str(a_id): _mem(a_id), str(b_id): _mem(b_id)}
    search_results = [
        {"id": str(a_id), "score": 0.9},
        {"id": str(b_id), "score": 0.8},
    ]
    svc = _service_with_edges([], [(a_id, b_id)])

    shadow_map, contradiction_map = await svc._apply_supersede_shadowing(
        search_results, memories, user_id="u", include_superseded=False
    )

    assert shadow_map == {}
    assert len(search_results) == 2  # nothing hidden
    assert contradiction_map[a_id] == [b_id]
    assert contradiction_map[b_id] == [a_id]


@pytest.mark.asyncio
async def test_over_supersede_placebo_unlinked_memories_untouched() -> None:
    """Distinct facts without a supersedes edge are NEVER filtered — the
    deterministic form of the over-supersede placebo: shadowing cannot leak
    beyond explicitly linked pairs."""
    ids = [uuid4() for _ in range(4)]
    memories = {str(i): _mem(i) for i in ids}
    search_results = [{"id": str(i), "score": 0.9} for i in ids]
    shadowed_id, superseder = ids[0], uuid4()
    svc = _service_with_edges([(shadowed_id, superseder)], [])

    shadow_map, _ = await svc._apply_supersede_shadowing(
        search_results, memories, user_id="u", include_superseded=False
    )

    assert set(shadow_map) == {shadowed_id}
    survivors = {r["id"] for r in search_results}
    assert survivors == {str(i) for i in ids[1:]}  # only the linked dst dropped


@pytest.mark.asyncio
async def test_fail_open_on_query_error() -> None:
    old_id = uuid4()
    memories = {str(old_id): _mem(old_id)}
    search_results = [{"id": str(old_id), "score": 0.9}]
    svc = MemoryService(MagicMock())
    db = AsyncMock()
    db.execute.side_effect = RuntimeError("edge table on fire")
    svc.db = db

    shadow_map, contradiction_map = await svc._apply_supersede_shadowing(
        search_results, memories, user_id="u", include_superseded=False
    )

    assert shadow_map == {} and contradiction_map == {}
    assert len(search_results) == 1  # original results preserved


@pytest.mark.asyncio
async def test_empty_candidates_short_circuits() -> None:
    svc = MemoryService(MagicMock())
    svc.db = AsyncMock()
    shadow_map, _ = await svc._apply_supersede_shadowing(
        [], {}, user_id="u", include_superseded=False
    )
    assert shadow_map == {}
    svc.db.execute.assert_not_awaited()


# ---------------------------------------------------------------- dedup mode


class TestShadowMergeMode:
    @pytest.mark.asyncio
    async def test_shadow_merge_creates_edge_and_mutates_nothing(self) -> None:
        phase = DedupMergePhase(MagicMock(), MagicMock())
        phase.edge_repo = MagicMock()
        phase.edge_repo.create_edge_if_absent = AsyncMock()
        winner, loser = _mem(uuid4()), _mem(uuid4())

        await phase._execute_shadow_merge(winner, loser, "u", None, None)

        kwargs = phase.edge_repo.create_edge_if_absent.await_args.kwargs
        assert kwargs["src_id"] == winner.id  # src = superseding (winner)
        assert kwargs["dst_id"] == loser.id  # dst = superseded (loser)
        assert kwargs["edge_type"] == "supersedes"
        assert kwargs["origin"] == "semantic"  # machine-inferred, not declared

    @pytest.mark.asyncio
    async def test_settled_pairs_filtered_before_judging(self) -> None:
        phase = DedupMergePhase(MagicMock(), MagicMock())
        a, b, c, d = uuid4(), uuid4(), uuid4(), uuid4()
        pairs = [(a, b, 0.95), (c, d, 0.94)]
        db = AsyncMock()
        rows = MagicMock()
        rows.all.return_value = [(b, a)]  # (a, b) already settled, either direction
        db.execute.return_value = rows
        phase.db = db

        remaining, skipped = await phase._filter_already_superseded_pairs(pairs, "u")

        assert remaining == [(c, d, 0.94)]
        assert skipped == 1

    @pytest.mark.asyncio
    async def test_no_settled_pairs_is_passthrough(self) -> None:
        phase = DedupMergePhase(MagicMock(), MagicMock())
        pairs = [(uuid4(), uuid4(), 0.95)]
        db = AsyncMock()
        rows = MagicMock()
        rows.all.return_value = []
        db.execute.return_value = rows
        phase.db = db

        remaining, skipped = await phase._filter_already_superseded_pairs(pairs, "u")
        assert remaining == pairs and skipped == 0
