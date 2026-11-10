"""An optional ``hint`` from recall towards ``list`` (#2754).

When ``RECALL_LIST_HINT_ENABLED`` is on, a single-context, non-degraded recall
looks at the candidate pool it already holds (the candidates that passed the
same filters and shadowing as the page, before the top-k cut). For each tag on a
page row, ``shown`` counts page rows with the tag and ``matched`` pool candidates
with it; a tag with ``matched > shown`` qualifies, the largest ``matched`` wins
(ties: larger ``shown``, then alphabetical) and the reply carries one line naming
``list(filters={"tags": [...]})``. No query is added for it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from config.settings import Settings, get_settings
from models.schemas import MemoryResponse, RecallRequest
from services.memory_service import MemoryService
from services.recall_list_hint import list_hint


def _expected(tag: str, matched: int, shown: int) -> str:
    quoted = json.dumps(tag, ensure_ascii=False)
    return (
        f"At least {matched} memories matching this search carry tag {quoted}, "
        f'{shown} shown; list(filters={{"tags": [{quoted}]}}) returns every memory with it.'
    )


# ------------------------------------------------------------ the pure choice


class TestListHint:
    def test_no_pool_beyond_the_page_says_nothing(self):
        page = [["trip"], ["trip", "food"]]
        assert list_hint(page, page) is None

    def test_one_tag_ahead_is_named(self):
        page = [["trip"], ["food"]]
        pool = [*page, ["trip"], ["trip", "bike"]]
        assert list_hint(page, pool) == _expected("trip", 3, 1)

    def test_the_largest_matched_wins(self):
        page = [["trip", "food"]]
        pool = [*page, ["trip"], ["food"], ["food"]]
        assert list_hint(page, pool) == _expected("food", 3, 1)

    def test_a_tie_on_matched_goes_to_the_larger_shown(self):
        page = [["trip", "food"], ["food"]]
        pool = [*page, ["trip"], ["trip"], ["food"]]
        # trip: matched 3, shown 1; food: matched 3, shown 2.
        assert list_hint(page, pool) == _expected("food", 3, 2)

    def test_a_full_tie_goes_to_the_alphabetical_first(self):
        page = [["zeta", "alpha"]]
        pool = [*page, ["zeta"], ["alpha"]]
        assert list_hint(page, pool) == _expected("alpha", 2, 1)

    def test_a_tag_only_on_the_pool_is_never_hinted(self):
        page = [["trip"]]
        pool = [*page, ["bike"], ["bike"], ["bike"]]
        assert list_hint(page, pool) is None

    def test_a_tag_repeated_on_one_row_counts_once(self):
        page = [["trip", "trip"]]
        pool = [*page, ["trip", "trip"]]
        assert list_hint(page, pool) == _expected("trip", 2, 1)

    def test_the_tag_is_json_escaped(self):
        tag = 'say "hi"\\旅行'
        page = [[tag]]
        pool = [*page, [tag]]
        hint = list_hint(page, pool)
        assert hint == _expected(tag, 2, 1)
        assert '"say \\"hi\\"\\\\旅行"' in hint

    def test_an_empty_page_says_nothing(self):
        assert list_hint([], [["trip"], ["trip"]]) is None


def test_the_setting_is_off_by_default(monkeypatch):
    monkeypatch.delenv("RECALL_LIST_HINT_ENABLED", raising=False)
    assert Settings.model_fields["recall_list_hint_enabled"].default is False


# ------------------------------------------------------ the service wiring


def _memory(tags: list[str]) -> SimpleNamespace:
    return SimpleNamespace(id=uuid4(), tags=tags)


def _response(memory: SimpleNamespace) -> MemoryResponse:
    return MemoryResponse(
        memory_id=memory.id,
        summary="a memory",
        context_summary=None,
        type="event",
        importance=0.5,
        scope="persistent",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=None,
        client="test",
        tags=memory.tags,
        context=None,
        score=0.5,
    )


def _pool(page_size: int = 2):
    """Five candidates; the first ``page_size`` are the page. One extra hit has no
    hydrated row (it cannot be in the reply, so it must not count)."""
    rows = [
        _memory(["trip"]),
        _memory(["food"]),
        _memory(["trip"]),
        _memory(["trip"]),
        _memory(["food"]),
    ]
    memories = {f"p{i}": m for i, m in enumerate(rows)}
    hits = [{"id": f"p{i}", "score": 0.9 - i / 10} for i in range(len(rows))]
    hits.append({"id": "gone", "score": 0.1})
    page = [_response(m) for m in rows[:page_size]]
    return hits, memories, page


async def _finalize(svc: MemoryService, hits, memories, page, **overrides):
    kwargs = {
        "user_id": "u",
        "memories": memories,
        "current_context_id": uuid4(),
        "effective_workspace_id": uuid4(),
        "neural_enabled": False,
        "binding_row_filtered": 0,
        "selection_evidence": None,
        "cross_context": False,
        "degradation": None,
    }
    kwargs.update(overrides)
    with patch("services.memory_access_event_writer.emit_memory_access_event", new=AsyncMock()):
        return await svc._recall_finalize_response(
            RecallRequest(query="q", k=2), hits, page, **kwargs
        )


@pytest.fixture
def hint_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "recall_list_hint_enabled", True)


def _service() -> MemoryService:
    db = MagicMock()
    db.execute = AsyncMock(side_effect=AssertionError("the hint must not query"))
    svc = MemoryService(db)
    svc._tag_suggestions_for_empty_result = AsyncMock(return_value=None)
    return svc


@pytest.mark.asyncio
class TestFinalizeUsesThePoolItHolds:
    async def test_the_pool_is_the_pre_cut_candidate_set(self, hint_on):
        hits, memories, page = _pool()
        response = await _finalize(_service(), hits, memories, page)
        # trip: 3 hydrated candidates, 1 on the page; food: 2 and 1.
        assert response.hint == _expected("trip", 3, 1)

    async def test_no_query_runs_for_the_hint(self, hint_on):
        svc = _service()
        hits, memories, page = _pool()
        await _finalize(svc, hits, memories, page)
        svc.db.execute.assert_not_called()
        svc._tag_suggestions_for_empty_result.assert_awaited_once()

    async def test_off_by_default_no_hint(self):
        assert get_settings().recall_list_hint_enabled is False
        hits, memories, page = _pool()
        response = await _finalize(_service(), hits, memories, page)
        assert response.hint is None

    async def test_not_on_a_cross_context_recall(self, hint_on):
        hits, memories, page = _pool()
        response = await _finalize(_service(), hits, memories, page, cross_context=True)
        assert response.hint is None

    async def test_not_on_a_degraded_recall(self, hint_on):
        hits, memories, page = _pool()
        response = await _finalize(
            _service(),
            hits,
            memories,
            page,
            degradation={"degraded": True, "reason": "embedding_unavailable"},
        )
        assert response.hint is None

    async def test_a_page_holding_the_whole_pool_gets_none(self, hint_on):
        hits, memories, page = _pool(page_size=5)
        response = await _finalize(_service(), hits, memories, page)
        assert response.hint is None
