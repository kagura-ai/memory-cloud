"""#2123: judge-verified older versions of a fact fold under the newer one.

Pins the read-path half of the fix:

1. A verified chain (v3→v2→v1) collapses to one row whatever the pool order
   (the cross-encoder may rank v1 first, the hybrid order v3 first): the head
   is the newest version, at the group's best position, with that row's
   scores; the others are listed newest first in the fold map.
2. Links are followed transitively but only in the direction a verdict gave:
   two newer memories that each supersede the same older one both stay.
3. An unverified link breaks the chain; a cosine-only candidate never folds.
4. ``include_superseded=true`` folds nothing.
5. A candidate whose target is not in the pool folds nothing.
6. A version shadowing removed still links its chain, over several edges.
7. Only memories with a ``supersedes`` verdict are resolved; none → no query.
8. Fail-open: a resolver error leaves the pool untouched.
9. recall() folds before the re-rank, the selection plan and the page builder.
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
        "content": "the fact in full",  # #2124
        "type": "fact",
        "importance": 0.5,
        "scope": "working",
        "created_at": T0 - timedelta(days=age_days),
        "updated_at": None,
        "client": "test",
        "tags": [],
        "context": None,
        "details": None,  # #2330
        "source_uri": None,
        "source_type": None,
        "version": 1,
        "supersede_candidate": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _judged(newer, older) -> None:
    """Store a candidate with a ``supersedes`` verdict on ``newer``."""
    newer.supersede_candidate = {
        "memory_id": str(older.id),
        "similarity": 0.8,
        "verification": {"verdict": "supersedes", "confidence": 0.9},
    }


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


async def _fold(
    svc, search_results, memories, *, include_superseded=False, shadow_map=None, resolved=None
):
    return await svc._apply_supersede_fold(
        search_results,
        memories,
        include_superseded=include_superseded,
        user_id="u",
        trusted_only=False,
        shadow_map=shadow_map,
        resolved=resolved,
    )


async def _page(svc, search_results, memories, fold_map, resolved=None):
    return await svc._recall_build_responses(
        RecallRequest(query="q", k=5),
        search_results,
        memories,
        shadow_map={},
        contradiction_map={},
        user_id="u",
        fold_map=fold_map,
        resolved_candidates=resolved,
    )


def _chain():
    v1, v2, v3 = _mem(2), _mem(1), _mem(0)
    _judged(v3, v2)
    _judged(v2, v1)
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

        fold_map = await _fold(svc, search_results, memories)

        assert [r["id"] for r in search_results] == [str(v3.id), str(other.id)]
        head = search_results[0]
        assert head["score"] == 0.9 and head["hybrid_score"] == 0.9
        assert fold_map == {v3.id: [v2.id, v1.id]}
        # Only the memories with a supersedes verdict are resolved.
        resolved_over = svc._resolve_supersede_candidates.await_args.args[0]
        assert set(resolved_over) == {v2.id, v3.id}

    async def test_chain_ranked_newest_first_gives_the_same_page(self):
        """The reranker-off shape: the hybrid order already has v3 first."""
        v1, v2, v3, candidates = _chain()
        other = _mem(5)
        search_results, memories = _pool((v3, 0.9), (other, 0.85), (v1, 0.8), (v2, 0.7))

        fold_map = await _fold(_service(candidates), search_results, memories)

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

    async def test_two_newer_memories_of_one_older_both_stay(self):
        """v3→v1 and v2→v1, no verdict between v3 and v2 (staging vs prod
        upgraded): both stay, the shared older one folds under the newer."""
        v1, v2, v3 = _mem(2), _mem(1), _mem(0)
        _judged(v3, v1)
        _judged(v2, v1)
        candidates = {v3.id: _cand(v1), v2.id: _cand(v1)}
        search_results, memories = _pool((v2, 0.9), (v1, 0.8), (v3, 0.7))

        fold_map = await _fold(_service(candidates), search_results, memories)

        assert [r["id"] for r in search_results] == [str(v2.id), str(v3.id)]
        assert fold_map == {v3.id: [v1.id]}

    async def test_an_unverified_link_breaks_the_chain(self):
        v1, v2, v3 = _mem(2), _mem(1), _mem(0)
        _judged(v3, v2)
        _judged(v2, v1)
        candidates = {v3.id: _cand(v2), v2.id: _cand(v1, verified=None)}
        search_results, memories = _pool((v1, 0.9), (v2, 0.8), (v3, 0.7))

        fold_map = await _fold(_service(candidates), search_results, memories)

        assert [r["id"] for r in search_results] == [str(v1.id), str(v3.id)]
        assert fold_map == {v3.id: [v2.id]}

    async def test_unverified_candidates_never_fold(self):
        v1, v2, v3 = _mem(2), _mem(1), _mem(0)
        _judged(v3, v2)
        _judged(v2, v1)
        candidates = {v3.id: _cand(v2, verified=None), v2.id: _cand(v1, verified=None)}
        search_results, memories = _pool((v1, 0.9), (v2, 0.8), (v3, 0.7))
        before = [dict(r) for r in search_results]

        fold_map = await _fold(_service(candidates), search_results, memories)

        assert fold_map == {}
        assert search_results == before

    async def test_no_supersedes_verdict_means_no_query(self):
        """The judge-off deployment: cosine-only candidates, nothing to fold,
        and the fold costs no query."""
        v1, v2 = _mem(1), _mem(0)
        v2.supersede_candidate = {"memory_id": str(v1.id), "similarity": 0.9}
        search_results, memories = _pool((v1, 0.9), (v2, 0.8))
        svc = _service({})

        assert await _fold(svc, search_results, memories) == {}
        svc._resolve_supersede_candidates.assert_not_awaited()
        assert len(search_results) == 2

    async def test_include_superseded_folds_nothing(self):
        v1, v2, v3, candidates = _chain()
        search_results, memories = _pool((v1, 0.9), (v2, 0.8), (v3, 0.7))
        svc = _service(candidates)

        fold_map = await _fold(svc, search_results, memories, include_superseded=True)

        assert fold_map == {}
        assert len(search_results) == 3
        svc._resolve_supersede_candidates.assert_not_awaited()

    async def test_a_target_outside_the_pool_folds_nothing(self):
        v1, v2, v3 = _mem(2), _mem(1), _mem(0)
        _judged(v3, v1)
        candidates = {v3.id: _cand(v1)}
        search_results, memories = _pool((v3, 0.9), (v2, 0.5))

        fold_map = await _fold(_service(candidates), search_results, memories)

        assert fold_map == {}
        assert [r["id"] for r in search_results] == [str(v3.id), str(v2.id)]

    async def test_a_shadowed_version_still_links_its_chain(self):
        """Accepting v3's suggestion shadows v2 (edge v3→v2) and clears v3's
        candidate; v1, linked to v2 only, stays folded under v3."""
        v1, v2, v3 = _mem(2), _mem(1), _mem(0)
        _judged(v2, v1)
        candidates = {v2.id: _cand(v1)}
        search_results, memories = _pool((v1, 0.9), (v3, 0.8))
        memories[str(v2.id)] = v2  # hydrated, then removed by shadowing
        svc = _service(candidates)

        fold_map = await _fold(svc, search_results, memories, shadow_map={v2.id: v3.id})

        assert [r["id"] for r in search_results] == [str(v3.id)]
        assert search_results[0]["score"] == 0.9
        # v2 is a link, not a row this recall folded.
        assert fold_map == {v3.id: [v1.id]}
        assert set(svc._resolve_supersede_candidates.await_args.args[0]) == {v2.id}

    async def test_a_chain_of_accepted_edges_still_links(self):
        """v4→v3 and v3→v2 accepted (both shadowed), v2 verified→v1: v1 stays
        folded under v4."""
        v1, v2, v3, v4 = _mem(3), _mem(2), _mem(1), _mem(0)
        _judged(v2, v1)
        candidates = {v2.id: _cand(v1)}
        search_results, memories = _pool((v1, 0.9), (v4, 0.8))
        memories[str(v2.id)] = v2
        memories[str(v3.id)] = v3

        fold_map = await _fold(
            _service(candidates),
            search_results,
            memories,
            shadow_map={v2.id: v3.id, v3.id: v4.id},
        )

        assert [r["id"] for r in search_results] == [str(v4.id)]
        assert fold_map == {v4.id: [v1.id]}

    async def test_a_cycle_without_a_head_stays_unfolded(self):
        v1, v2 = _mem(1), _mem(0)
        _judged(v1, v2)
        _judged(v2, v1)
        candidates = {v1.id: _cand(v2), v2.id: _cand(v1)}
        search_results, memories = _pool((v1, 0.9), (v2, 0.8))

        assert await _fold(_service(candidates), search_results, memories) == {}
        assert len(search_results) == 2

    async def test_resolver_error_fails_open(self):
        v1, v2, v3, _ = _chain()
        search_results, memories = _pool((v1, 0.9), (v2, 0.8), (v3, 0.7))
        before = [dict(r) for r in search_results]
        svc = MemoryService(MagicMock())
        svc._resolve_supersede_candidates = AsyncMock(side_effect=RuntimeError("boom"))

        with patch("services.memory_service.logger") as log:
            fold_map = await _fold(svc, search_results, memories)

        assert fold_map == {}
        assert search_results == before
        events = [c.args[0] for c in log.warning.call_args_list if c.args]
        assert "supersede_fold_failed" in events

    async def test_a_grouping_error_leaves_the_pool_untouched(self):
        v1, v2, v3, candidates = _chain()
        v2.created_at = "not a datetime"  # ordering the members raises
        search_results, memories = _pool((v1, 0.9), (v2, 0.8), (v3, 0.7))
        before = [dict(r) for r in search_results]

        assert await _fold(_service(candidates), search_results, memories) == {}
        assert search_results == before

    async def test_empty_pool_does_not_query(self):
        svc = _service({})

        assert await _fold(svc, [], {}) == {}
        svc._resolve_supersede_candidates.assert_not_awaited()


@pytest.mark.asyncio
class TestPage:
    async def test_page_carries_folded_and_the_heads_own_hint(self):
        v1, v2, v3, candidates = _chain()
        search_results, memories = _pool((v1, 0.9), (v2, 0.8), (v3, 0.7))
        svc = _service(candidates)
        resolved: dict = {}  # as recall() hands it to both (#2131)
        fold_map = await _fold(svc, search_results, memories, resolved=resolved)

        responses = await svc._recall_build_responses(
            RecallRequest(query="q", k=5),
            search_results,
            memories,
            shadow_map={},
            contradiction_map={},
            user_id="u",
            fold_map=fold_map,
            resolved_candidates=resolved,
        )

        assert len(responses) == 1
        head = responses[0]
        assert head.memory_id == v3.id
        assert head.score == 0.9
        assert head.folded.count == 2
        assert head.folded.memory_ids == [v2.id, v1.id]
        # The head keeps its own accept affordance — from the fold's lookup,
        # which the page reuses rather than resolving v3 again (#2131).
        assert head.supersede_candidate.memory_id == v2.id
        svc._resolve_supersede_candidates.assert_awaited_once()
        assert set(svc._resolve_supersede_candidates.await_args.args[0]) == {v2.id, v3.id}

    async def test_projection_renders_folded_and_omits_it_when_absent(self):
        v1, v2, v3, candidates = _chain()
        search_results, memories = _pool((v1, 0.9), (v2, 0.8), (v3, 0.7), (_mem(9), 0.1))
        svc = _service(candidates)
        fold_map = await _fold(svc, search_results, memories)
        responses = await svc._recall_build_responses(
            RecallRequest(query="q", k=5),
            search_results,
            memories,
            shadow_map={},
            contradiction_map={},
            user_id="u",
            fold_map=fold_map,
        )

        for verbosity in ("compact", "full"):
            head, plain = (recall_result_item(r, verbosity) for r in responses)
            assert head["folded"] == {"count": 2, "memory_ids": [str(v2.id), str(v1.id)]}
            assert "folded" not in plain


@pytest.mark.asyncio
class TestResolveOnce:
    """#2131: a judge-on recall resolves each candidate once — the page reuses
    the fold's answer and queries only the rows the fold did not resolve."""

    async def test_page_reuses_the_folds_resolution(self):
        v1, v2, v3, candidates = _chain()
        search_results, memories = _pool((v1, 0.9), (v2, 0.8), (v3, 0.7))
        svc = _service(candidates)
        resolved: dict = {}

        fold_map = await _fold(svc, search_results, memories, resolved=resolved)
        responses = await _page(svc, search_results, memories, fold_map, resolved)

        svc._resolve_supersede_candidates.assert_awaited_once()
        assert resolved == {v3.id: candidates[v3.id], v2.id: candidates[v2.id]}
        assert responses[0].supersede_candidate == candidates[v3.id]

    async def test_page_resolves_only_the_rows_the_fold_did_not(self):
        v1, v2, v3, candidates = _chain()
        other, target = _mem(5), _mem(6)
        other.supersede_candidate = {"memory_id": str(target.id), "similarity": 0.9}
        other_hint = _cand(target, verified=None)
        search_results, memories = _pool((v1, 0.9), (other, 0.85), (v2, 0.8), (v3, 0.7))
        svc = _service(candidates)
        svc._resolve_supersede_candidates.side_effect = [candidates, {other.id: other_hint}]
        resolved: dict = {}

        fold_map = await _fold(svc, search_results, memories, resolved=resolved)
        responses = await _page(svc, search_results, memories, fold_map, resolved)

        assert svc._resolve_supersede_candidates.await_count == 2
        page_lookup = svc._resolve_supersede_candidates.await_args_list[-1].args[0]
        assert set(page_lookup) == {str(other.id)}
        hints = {r.memory_id: r.supersede_candidate for r in responses}
        assert hints == {v3.id: candidates[v3.id], other.id: other_hint}

    async def test_a_judged_row_whose_hint_was_dropped_is_not_resolved_again(self):
        """The resolver dropped v2's hint (parallel, stale, target gone) and
        kept v3's: the sink records both, so the page neither asks again nor
        ships a hint for v2."""
        v1, v2, v3 = _mem(2), _mem(1), _mem(0)
        _judged(v2, v1)
        _judged(v3, v1)
        search_results, memories = _pool((v1, 0.9), (v2, 0.8), (v3, 0.7))
        hint = _cand(v1, verified=None)  # unverified: nothing folds
        svc = _service({v3.id: hint})
        resolved: dict = {}

        fold_map = await _fold(svc, search_results, memories, resolved=resolved)
        responses = await _page(svc, search_results, memories, fold_map, resolved)

        assert fold_map == {}
        assert resolved == {v2.id: None, v3.id: hint}
        # Only v1 (no stored candidate, so no query) goes to the page lookup.
        page_lookup = svc._resolve_supersede_candidates.await_args_list[-1].args[0]
        assert set(page_lookup) == {str(v1.id)}
        hints = {r.memory_id: r.supersede_candidate for r in responses}
        assert hints == {v1.id: None, v2.id: None, v3.id: hint}

    async def test_an_empty_answer_leaves_the_page_to_resolve(self):
        """The real resolver fails open to {} (DB or binding error), which
        cannot be told from "no hint survives": the sink stays empty and the
        page resolves its rows itself, as before #2131."""
        v1, v2, v3, candidates = _chain()
        search_results, memories = _pool((v1, 0.9), (v2, 0.8), (v3, 0.7))
        svc = _service(candidates)
        svc._resolve_supersede_candidates.side_effect = [{}, candidates]
        resolved: dict = {}

        fold_map = await _fold(svc, search_results, memories, resolved=resolved)
        responses = await _page(svc, search_results, memories, fold_map, resolved)

        assert fold_map == {} and resolved == {}
        page_lookup = svc._resolve_supersede_candidates.await_args_list[-1].args[0]
        assert set(page_lookup) == {str(v1.id), str(v2.id), str(v3.id)}
        hints = {r.memory_id: r.supersede_candidate for r in responses}
        assert hints[v3.id] == candidates[v3.id]

    async def test_a_fold_that_fails_open_leaves_the_page_to_resolve(self):
        v1, v2, v3, candidates = _chain()
        search_results, memories = _pool((v1, 0.9), (v2, 0.8), (v3, 0.7))
        svc = _service(candidates)
        svc._resolve_supersede_candidates.side_effect = [RuntimeError("boom"), candidates]
        resolved: dict = {}

        fold_map = await _fold(svc, search_results, memories, resolved=resolved)
        responses = await _page(svc, search_results, memories, fold_map, resolved)

        assert fold_map == {} and resolved == {}
        assert svc._resolve_supersede_candidates.await_count == 2
        page_lookup = svc._resolve_supersede_candidates.await_args_list[-1].args[0]
        assert set(page_lookup) == {str(v1.id), str(v2.id), str(v3.id)}
        assert len(responses) == 3

    async def test_judge_off_is_still_one_page_query(self):
        v1, v2 = _mem(1), _mem(0)
        v2.supersede_candidate = {"memory_id": str(v1.id), "similarity": 0.9}
        search_results, memories = _pool((v1, 0.9), (v2, 0.8))
        svc = _service({v2.id: _cand(v1, verified=None)})
        resolved: dict = {}

        fold_map = await _fold(svc, search_results, memories, resolved=resolved)
        await _page(svc, search_results, memories, fold_map, resolved)

        assert resolved == {}
        svc._resolve_supersede_candidates.assert_awaited_once()
        page_lookup = svc._resolve_supersede_candidates.await_args.args[0]
        assert set(page_lookup) == {str(v1.id), str(v2.id)}

    async def test_recall_hands_the_folds_resolution_to_the_page(self):
        v1, v2, v3, candidates = _chain()
        search_results, memories = _pool((v1, 0.9), (v2, 0.8), (v3, 0.7))
        service = MemoryService(db=AsyncMock())
        captured: dict = {}

        async def fold(*_args, resolved, **_kwargs):
            resolved[v3.id] = candidates[v3.id]
            captured["fold"] = resolved
            return {}

        service._apply_supersede_fold = fold
        build = await _run_recall(service, search_results, memories, candidates)

        assert build.await_args.kwargs["resolved_candidates"] is captured["fold"]
        assert captured["fold"] == {v3.id: candidates[v3.id]}


async def _run_recall(service, search_results, memories, candidates):
    """Drive recall() with every lane but the fold stubbed; returns the
    page-builder mock."""
    ctx, ws = uuid4(), uuid4()
    request = RecallRequest(query="q", k=5)
    plan = SimpleNamespace(
        request=request,
        effective_workspace_id=ws,
        is_shared_context_read=False,
        neural_enabled=False,
        search_config=None,
        search_context_id=str(ctx),
    )
    service._recall_check_agent_bindings = AsyncMock()
    service._recall_prepare = AsyncMock(return_value=plan)
    service._recall_resolve_cluster_filter = AsyncMock(return_value=None)
    service.search_service = MagicMock()
    service.search_service.hybrid_search = AsyncMock(return_value=search_results)
    service._recall_hydrate_memories = AsyncMock(return_value=memories)
    service._recall_filter_rows_by_binding = AsyncMock(side_effect=lambda _r, m, **_k: (m, 0))
    service._apply_supersede_shadowing = AsyncMock(return_value=({}, {}))
    service._resolve_supersede_candidates = AsyncMock(return_value=candidates)
    service._maybe_reinforce_rerank = AsyncMock()
    service._maybe_graph_boost = AsyncMock()
    build = AsyncMock(return_value=[])
    service._recall_build_responses = build
    service._recall_record_access = AsyncMock()
    service._recall_finalize = AsyncMock(return_value="response")
    await service.recall(
        request,
        "u",
        current_context_id=ctx,
        current_workspace_id=ws,
        capacity_gate=False,
    )
    return build


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


@pytest.mark.asyncio
class TestSubThresholdCandidatesAtReadTime:
    """#2123: a candidate below 0.85 exists only on the judge's word, so it
    surfaces only while that word confirms it (verified: true)."""

    @staticmethod
    def _pair(similarity: float, verdict: str | None, confidence: float = 0.95):
        from services.supersede_verify import pair_fingerprint, text_fingerprint

        ws, ctx = uuid4(), uuid4()
        old = SimpleNamespace(
            id=uuid4(),
            summary="old",
            content="old",
            content_head="old",
            workspace_id=ws,
            context_id=ctx,
            type="fact",
            source_type="manual",
        )
        new = _mem(0, workspace_id=ws, context_id=ctx, summary="new", content="new")
        candidate = {"memory_id": str(old.id), "similarity": similarity}
        if verdict is not None:
            candidate["verification"] = {
                "verdict": verdict,
                "confidence": confidence,
                "fingerprint": pair_fingerprint(old, new),
                "old_fingerprint": text_fingerprint(old.summary, old.content),
            }
        new.supersede_candidate = candidate
        svc = MemoryService(MagicMock())
        result = MagicMock()
        result.all = MagicMock(return_value=[old])
        svc.db.execute = AsyncMock(return_value=result)
        return svc, new, old

    async def _resolve(self, svc, new):
        return await svc._resolve_supersede_candidates(
            {new.id: new}, user_id="u", operation="recall"
        )

    async def test_a_verified_sub_threshold_candidate_surfaces(self):
        svc, new, old = self._pair(0.78, "supersedes")
        resolved = await self._resolve(svc, new)
        assert resolved[new.id].memory_id == old.id
        assert resolved[new.id].verified is True

    async def test_a_low_confidence_verdict_below_085_is_hidden(self, monkeypatch):
        monkeypatch.delenv("SUPERSEDE_VERIFY_MIN_CONFIDENCE", raising=False)
        svc, new, _ = self._pair(0.78, "supersedes", confidence=0.5)
        assert await self._resolve(svc, new) == {}

    async def test_a_stale_verdict_below_085_is_hidden(self):
        svc, new, _ = self._pair(0.78, "supersedes")
        new.summary = "edited since the judge saw it"
        assert await self._resolve(svc, new) == {}

    async def test_an_unverified_candidate_at_085_still_surfaces(self):
        svc, new, old = self._pair(0.9, None)
        resolved = await self._resolve(svc, new)
        assert resolved[new.id].memory_id == old.id
        assert resolved[new.id].verified is None
