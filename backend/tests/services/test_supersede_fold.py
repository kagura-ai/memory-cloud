"""#2123: judge-verified older versions of a fact fold under the newest one.

Pins the read-path half of the fix:

1. A verified chain (v3→v2→v1) collapses to one row whatever the pool order
   (the cross-encoder may rank v1 first, the hybrid order v3 first): the head
   is the newest version, at the group's best position, with the group's best
   scores; the others are listed newest first in the fold map.
2. Joins are transitive (a skipped link still forms one group) and an
   unverified link breaks the chain there.
3. A cosine-only (unverified) candidate never folds.
4. ``include_superseded=true`` folds nothing.
5. A candidate whose target is not in the pool folds nothing.
6. Fail-open: a resolver error leaves the pool untouched.
7. recall() folds before the re-rank, the selection plan and the page builder,
   and hands the page builder the candidates it resolved.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from models.schemas import MemoryResponse, RecallRequest, SupersedeCandidate
from services.memory_service import MemoryService
from utils.recall_projection import recall_result_item

T0 = datetime(2026, 10, 1, tzinfo=UTC)


def _mem(age_days: int, **overrides) -> SimpleNamespace:
    """A hydrated memory; a larger ``age_days`` is an OLDER version."""
    values = {
        "id": uuid4(),
        "summary": "the fact",
        "context_summary": None,
        "type": "fact",
        "importance": 0.5,
        "scope": "working",
        "created_at": T0 - timedelta(days=age_days),
        "updated_at": None,
        "client": "test",
        "tags": [],
        "context": None,
        "source_uri": None,
        "source_type": None,
        "version": 1,
        "supersede_candidate": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _cand(target, *, verified: bool | None = True) -> SupersedeCandidate:
    return SupersedeCandidate(
        memory_id=target.id, summary="older", similarity=0.8, verified=verified
    )


def _service(candidates) -> MemoryService:
    svc = MemoryService(MagicMock())
    svc._resolve_supersede_candidates = AsyncMock(return_value=candidates)
    return svc


def _pool(*rows):
    """(memory, score) pairs → search_results rows + the memories map."""
    search_results = [{"id": str(m.id), "score": score, "hybrid_score": score} for m, score in rows]
    memories = {str(m.id): m for m, _ in rows}
    return search_results, memories


async def _fold(svc, search_results, memories, *, include_superseded=False):
    return await svc._apply_supersede_fold(
        search_results,
        memories,
        include_superseded=include_superseded,
        user_id="u",
        trusted_only=False,
    )


def _chain():
    v1, v2, v3 = _mem(2), _mem(1), _mem(0)
    candidates = {v3.id: _cand(v2), v2.id: _cand(v1)}
    return v1, v2, v3, candidates


@pytest.mark.asyncio
class TestFold:
    async def test_chain_ranked_oldest_first_folds_under_the_newest(self):
        """The reranker-on shape: the query wording ranks v1 highest."""
        v1, v2, v3, candidates = _chain()
        other = _mem(5)
        search_results, memories = _pool((v1, 0.9), (other, 0.85), (v2, 0.8), (v3, 0.7))
        svc = _service(candidates)

        fold_map, resolved = await _fold(svc, search_results, memories)

        assert [r["id"] for r in search_results] == [str(v3.id), str(other.id)]
        head = search_results[0]
        assert head["score"] == 0.9 and head["hybrid_score"] == 0.9
        assert fold_map == {v3.id: [v2.id, v1.id]}
        assert resolved is candidates
        # Resolved once, over the whole pool (each memory once).
        pool_arg = svc._resolve_supersede_candidates.await_args.args[0]
        assert set(pool_arg) == {v1.id, v2.id, v3.id, other.id}

    async def test_chain_ranked_newest_first_gives_the_same_page(self):
        """The reranker-off shape: the hybrid order already has v3 first."""
        v1, v2, v3, candidates = _chain()
        other = _mem(5)
        search_results, memories = _pool((v3, 0.9), (other, 0.85), (v1, 0.8), (v2, 0.7))

        fold_map, _ = await _fold(_service(candidates), search_results, memories)

        assert [r["id"] for r in search_results] == [str(v3.id), str(other.id)]
        assert search_results[0]["score"] == 0.9
        assert fold_map == {v3.id: [v2.id, v1.id]}

    async def test_head_takes_the_best_rows_place_and_rank_stamps(self):
        """The head inherits the replaced row's ranking keys — including the
        reranker's unscored stamp — and the best raw cosine of the group."""
        from services.reranker_service import RERANK_UNSCORED_KEY

        v1, v2, v3, candidates = _chain()
        search_results = [
            {"id": str(v1.id), "score": 0.4, "hybrid_score": 0.95, "rerank_score": 0.95},
            {
                "id": str(v3.id),
                "score": 0.6,
                "hybrid_score": 0.3,
                RERANK_UNSCORED_KEY: True,
                "semantic_score_raw": 0.88,
            },
            {"id": str(v2.id), "score": 0.5, "hybrid_score": 0.2, "semantic_score_raw": 0.7},
        ]
        memories = {str(m.id): m for m in (v1, v2, v3)}

        await _fold(_service(candidates), search_results, memories)

        assert search_results == [
            {
                "id": str(v3.id),
                "score": 0.4,
                "hybrid_score": 0.95,
                "rerank_score": 0.95,
                "semantic_score_raw": 0.88,
            }
        ]

    async def test_a_skipped_link_still_forms_one_group(self):
        """v3→v1 and v2→v1 (v2 was not v3's top-1 neighbour): one group."""
        v1, v2, v3 = _mem(2), _mem(1), _mem(0)
        candidates = {v3.id: _cand(v1), v2.id: _cand(v1)}
        search_results, memories = _pool((v2, 0.9), (v1, 0.8), (v3, 0.7))

        fold_map, _ = await _fold(_service(candidates), search_results, memories)

        assert [r["id"] for r in search_results] == [str(v3.id)]
        assert fold_map == {v3.id: [v2.id, v1.id]}

    async def test_an_unverified_link_breaks_the_chain(self):
        v1, v2, v3 = _mem(2), _mem(1), _mem(0)
        candidates = {v3.id: _cand(v2), v2.id: _cand(v1, verified=None)}
        search_results, memories = _pool((v1, 0.9), (v2, 0.8), (v3, 0.7))

        fold_map, _ = await _fold(_service(candidates), search_results, memories)

        assert [r["id"] for r in search_results] == [str(v1.id), str(v3.id)]
        assert fold_map == {v3.id: [v2.id]}

    async def test_unverified_candidates_never_fold(self):
        v1, v2, v3 = _mem(2), _mem(1), _mem(0)
        candidates = {v3.id: _cand(v2, verified=None), v2.id: _cand(v1, verified=None)}
        search_results, memories = _pool((v1, 0.9), (v2, 0.8), (v3, 0.7))
        before = [dict(r) for r in search_results]

        fold_map, resolved = await _fold(_service(candidates), search_results, memories)

        assert fold_map == {}
        assert search_results == before
        assert resolved is candidates  # the hints still reach the page

    async def test_include_superseded_folds_nothing(self):
        v1, v2, v3, candidates = _chain()
        search_results, memories = _pool((v1, 0.9), (v2, 0.8), (v3, 0.7))

        fold_map, resolved = await _fold(
            _service(candidates), search_results, memories, include_superseded=True
        )

        assert fold_map == {}
        assert len(search_results) == 3
        assert resolved is candidates

    async def test_a_target_outside_the_pool_folds_nothing(self):
        v1, v3 = _mem(2), _mem(0)
        candidates = {v3.id: _cand(v1)}
        search_results, memories = _pool((v3, 0.9))

        fold_map, _ = await _fold(_service(candidates), search_results, memories)

        assert fold_map == {}
        assert [r["id"] for r in search_results] == [str(v3.id)]

    async def test_resolver_error_fails_open(self):
        v1, v2, v3, _ = _chain()
        search_results, memories = _pool((v1, 0.9), (v2, 0.8), (v3, 0.7))
        before = [dict(r) for r in search_results]
        svc = MemoryService(MagicMock())
        svc._resolve_supersede_candidates = AsyncMock(side_effect=RuntimeError("boom"))

        with patch("services.memory_service.logger") as log:
            fold_map, resolved = await _fold(svc, search_results, memories)

        assert fold_map == {}
        assert resolved is None  # the page builder resolves the page itself
        assert search_results == before
        events = [c.args[0] for c in log.warning.call_args_list if c.args]
        assert "supersede_fold_failed" in events

    async def test_a_grouping_error_leaves_the_pool_untouched(self):
        v1, v2, v3, candidates = _chain()
        v2.created_at = "not a datetime"  # sorting raises
        search_results, memories = _pool((v1, 0.9), (v2, 0.8), (v3, 0.7))
        before = [dict(r) for r in search_results]

        fold_map, resolved = await _fold(_service(candidates), search_results, memories)

        assert fold_map == {}
        assert resolved is candidates
        assert search_results == before

    async def test_empty_pool_does_not_query(self):
        svc = _service({})

        assert await _fold(svc, [], {}) == ({}, {})
        svc._resolve_supersede_candidates.assert_not_awaited()


@pytest.mark.asyncio
class TestPage:
    async def test_page_carries_folded_and_reuses_the_resolved_candidates(self):
        v1, v2, v3, candidates = _chain()
        search_results, memories = _pool((v1, 0.9), (v2, 0.8), (v3, 0.7))
        svc = _service(candidates)
        fold_map, resolved = await _fold(svc, search_results, memories)

        responses = await svc._recall_build_responses(
            RecallRequest(query="q", k=5),
            search_results,
            memories,
            shadow_map={},
            contradiction_map={},
            user_id="u",
            fold_map=fold_map,
            supersede_candidates=resolved,
        )

        assert len(responses) == 1
        head = responses[0]
        assert head.memory_id == v3.id
        assert head.score == 0.9
        assert head.folded.count == 2
        assert head.folded.memory_ids == [v2.id, v1.id]
        # The head keeps its own accept affordance.
        assert head.supersede_candidate.memory_id == v2.id
        # One resolution for the whole recall, not a second one for the page.
        svc._resolve_supersede_candidates.assert_awaited_once()

    async def test_projection_renders_folded_and_omits_it_when_absent(self):
        v1, v2, v3, candidates = _chain()
        search_results, memories = _pool((v1, 0.9), (v2, 0.8), (v3, 0.7), (_mem(9), 0.1))
        svc = _service({v3.id: candidates[v3.id], v2.id: candidates[v2.id]})
        fold_map, resolved = await _fold(svc, search_results, memories)
        responses = await svc._recall_build_responses(
            RecallRequest(query="q", k=5),
            search_results,
            memories,
            shadow_map={},
            contradiction_map={},
            user_id="u",
            fold_map=fold_map,
            supersede_candidates=resolved,
        )

        for verbosity in ("compact", "full"):
            head, plain = (recall_result_item(r, verbosity) for r in responses)
            assert head["folded"] == {"count": 2, "memory_ids": [str(v2.id), str(v1.id)]}
            assert "folded" not in plain


@pytest.mark.asyncio
async def test_recall_folds_before_rerank_selection_and_page():
    """#1306 invariant: every subtractive gate — the fold included — has run
    before the re-rank and the selection plan register the pool."""
    v1, v2, v3, candidates = _chain()
    search_results, memories = _pool((v1, 0.9), (v2, 0.8), (v3, 0.7))
    service = MemoryService(db=AsyncMock())
    ctx, ws = uuid4(), uuid4()
    request = RecallRequest(query="q", k=5, filters={"trust_tier": "trusted"})
    plan = SimpleNamespace(
        request=request,
        effective_workspace_id=ws,
        is_shared_context_read=False,
        neural_enabled=False,
        search_config=None,
        search_context_id=str(ctx),
    )
    seen: dict[str, list[str]] = {}

    def recorder(name):
        async def _record(pool, *_args, **_kwargs):
            seen[name] = [r["id"] for r in pool]
            return {} if name == "selection" else None

        return _record

    service._recall_check_agent_bindings = AsyncMock()
    service._recall_prepare = AsyncMock(return_value=plan)
    service._recall_resolve_cluster_filter = AsyncMock(return_value=None)
    service.search_service = MagicMock()
    service.search_service.hybrid_search = AsyncMock(return_value=search_results)
    service._recall_hydrate_memories = AsyncMock(return_value=memories)
    service._recall_filter_rows_by_binding = AsyncMock(side_effect=lambda _r, m, **_k: (m, 0))
    service._apply_supersede_shadowing = AsyncMock(return_value=({}, {}))
    service._resolve_supersede_candidates = AsyncMock(return_value=candidates)
    service._maybe_reinforce_rerank = recorder("rerank")
    service._maybe_graph_boost = AsyncMock()
    service._recall_apply_selection_plan = recorder("selection")
    build = AsyncMock(return_value=[])
    service._recall_build_responses = build
    service._recall_record_access = AsyncMock()
    service._recall_finalize = AsyncMock(return_value="response")

    await service.recall(
        request,
        "u",
        current_context_id=ctx,
        current_workspace_id=ws,
        selection_config=SimpleNamespace(candidate_pool_k=20),
        capacity_gate=False,
    )

    assert seen["rerank"] == [str(v3.id)]
    assert seen["selection"] == [str(v3.id)]
    assert [r["id"] for r in build.await_args.args[1]] == [str(v3.id)]
    assert build.await_args.kwargs["fold_map"] == {v3.id: [v2.id, v1.id]}
    assert build.await_args.kwargs["supersede_candidates"] is candidates


def test_folded_is_an_optional_response_field():
    response = MemoryResponse(
        memory_id=uuid4(),
        summary="s",
        context_summary=None,
        type="fact",
        importance=0.5,
        scope="working",
        created_at=T0,
        client="test",
        tags=[],
        context=None,
    )
    assert response.folded is None
    assert response.model_dump()["folded"] is None
