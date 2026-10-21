"""Supersede suggestions honour the recall's row filters (#2027).

``supersede_candidate`` names another memory and carries its summary. Under
``filters={"source_uri_prefix": "docs/"}`` (or ``type``, ``tags``,
``importance``, ``source_type``, ``scope``) the rows are filtered, but the
suggestion used to name any live target. The resolver now adds the recall's
row predicates to its one target query: a target outside the filter is not
returned, so the suggestion is dropped and the row kept, with no extra query.
Both the fold's resolution (#2131, reused by the page) and the page's own
read get the same effective filters.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.dialects import postgresql

from models.memory import SOURCE_TYPE_MANUAL
from models.schemas import RecallRequest, SupersedeCandidate
from services.memory_service import MemoryService

WS = uuid.uuid4()
CTX = uuid.uuid4()
T0 = datetime(2026, 10, 1, tzinfo=UTC)


def _source(target_id: uuid.UUID, *, age_days: int = 0, judged: bool = False) -> SimpleNamespace:
    candidate: dict = {
        "memory_id": str(target_id),
        "similarity": 0.93,
        "detected_at": "2026-10-01T00:00:00Z",
    }
    if judged:
        candidate["verification"] = {"verdict": "supersedes", "confidence": 0.9}
    return SimpleNamespace(
        id=uuid.uuid4(),
        summary="the newer fact",
        context_summary=None,
        content="body",
        type="note",
        importance=0.5,
        scope="working",
        created_at=T0 - timedelta(days=age_days),
        updated_at=None,
        version=1,
        last_used_at=None,
        access_count=0,
        accessed_by_clients=None,
        confidence=1.0,
        client="test",
        tags=[],
        context=None,
        source_uri="docs/a",
        source_type=SOURCE_TYPE_MANUAL,
        workspace_id=WS,
        context_id=CTX,
        supersede_candidate=candidate,
    )


def _target(target_id: uuid.UUID) -> SimpleNamespace:
    return SimpleNamespace(
        id=target_id,
        summary="the older fact",
        workspace_id=WS,
        context_id=CTX,
        type="note",
        source_type=SOURCE_TYPE_MANUAL,
        content_head="body",
    )


def _service(rows: list) -> MemoryService:
    service = MemoryService(MagicMock())
    result = MagicMock()
    result.all = MagicMock(return_value=rows)
    service.db.execute = AsyncMock(return_value=result)
    return service


def _where(service: MemoryService) -> str:
    statement = service.db.execute.await_args.args[0]
    return str(statement.compile(dialect=postgresql.dialect()))


@pytest.mark.asyncio
class TestResolver:
    async def test_resolver_adds_the_row_predicates_to_the_target_query(self):
        target_id = uuid.uuid4()
        source = _source(target_id)
        service = _service([_target(target_id)])

        await service._resolve_supersede_candidates(
            {source.id: source},
            user_id="u",
            operation="recall",
            filters={"source_uri_prefix": "docs/", "type": "note", "near": {"lat": 1}},
        )

        sql = _where(service)
        assert "memories.id IN" in sql
        assert "memories.deleted_at IS NULL" in sql
        assert "memories.source_uri LIKE" in sql
        assert "memories.type IN" in sql

    async def test_resolver_without_filters_queries_as_before(self):
        target_id = uuid.uuid4()
        source = _source(target_id)
        service = _service([_target(target_id)])

        resolved = await service._resolve_supersede_candidates(
            {source.id: source}, user_id="u", operation="recall"
        )

        sql = _where(service)
        assert "memories.source_uri" not in sql
        assert "memories.type IN" not in sql
        assert resolved[source.id].memory_id == target_id

    async def test_a_target_the_filter_keeps_out_drops_the_suggestion(self):
        # The query returns nothing for an off-filter target: same branch as
        # a deleted target — the suggestion goes, the source row stays.
        source = _source(uuid.uuid4())
        service = _service([])

        resolved = await service._resolve_supersede_candidates(
            {source.id: source},
            user_id="u",
            operation="recall",
            filters={"source_uri_prefix": "docs/"},
        )

        assert resolved == {}
        service.db.execute.assert_awaited_once()


@pytest.mark.asyncio
class TestThreading:
    async def test_page_hands_the_widened_filters_to_the_resolver(self):
        source = _source(uuid.uuid4())
        service = MemoryService(MagicMock())
        service._resolve_supersede_candidates = AsyncMock(return_value={})
        request = RecallRequest(
            query="q", k=5, filters={"tags": ["deploy"], "tags_normalize": True}
        )
        widened = {"tags": ["deploy", "Deploy"], "tags_normalize": True}

        await service._recall_build_responses(
            request,
            [{"id": "p1", "score": 0.9}],
            {"p1": source},
            shadow_map={},
            contradiction_map={},
            user_id="u",
            filters=widened,
        )

        assert service._resolve_supersede_candidates.await_args.kwargs["filters"] == widened

    async def test_page_defaults_to_the_request_filters(self):
        source = _source(uuid.uuid4())
        service = MemoryService(MagicMock())
        service._resolve_supersede_candidates = AsyncMock(return_value={})
        request = RecallRequest(query="q", k=5, filters={"type": "note"})

        await service._recall_build_responses(
            request,
            [{"id": "p1", "score": 0.9}],
            {"p1": source},
            shadow_map={},
            contradiction_map={},
            user_id="u",
        )

        assert service._resolve_supersede_candidates.await_args.kwargs["filters"] == {
            "type": "note"
        }

    async def test_fold_hands_the_filters_to_the_resolver(self):
        older = _source(uuid.uuid4(), age_days=1)
        newer = _source(older.id, judged=True)
        service = MemoryService(MagicMock())
        service._resolve_supersede_candidates = AsyncMock(return_value={})
        filters = {"source_uri_prefix": "docs/"}

        await service._apply_supersede_fold(
            [{"id": "n", "score": 0.9}, {"id": "o", "score": 0.8}],
            {"n": newer, "o": older},
            include_superseded=False,
            user_id="u",
            trusted_only=False,
            filters=filters,
        )

        assert service._resolve_supersede_candidates.await_args.kwargs["filters"] == filters

    async def test_fold_and_page_resolve_with_the_same_filters(self):
        # A judged head whose target is off-filter (outside the pool) and a
        # second judged row whose hint survives: the fold resolves both under
        # the filter, the page reuses that answer — no off-filter hint ships.
        off_filter_target = uuid.uuid4()
        on_filter_target = uuid.uuid4()
        head = _source(off_filter_target, judged=True)
        other = _source(on_filter_target, judged=True, age_days=1)
        hint = SupersedeCandidate(memory_id=on_filter_target, summary="kept", similarity=0.93)
        service = MemoryService(MagicMock())
        # The resolver under the filter returns only the surviving hint.
        service._resolve_supersede_candidates = AsyncMock(return_value={other.id: hint})
        filters = {"source_uri_prefix": "docs/"}
        pool = [{"id": "h", "score": 0.9}, {"id": "x", "score": 0.8}]
        memories = {"h": head, "x": other}
        resolved: dict = {}

        fold_map = await service._apply_supersede_fold(
            pool,
            memories,
            include_superseded=False,
            user_id="u",
            trusted_only=False,
            resolved=resolved,
            filters=filters,
        )
        page = await service._recall_build_responses(
            RecallRequest(query="q", k=5, filters=filters),
            pool,
            memories,
            shadow_map={},
            contradiction_map={},
            user_id="u",
            fold_map=fold_map,
            resolved_candidates=resolved,
            filters=filters,
        )

        service._resolve_supersede_candidates.assert_awaited_once()
        assert service._resolve_supersede_candidates.await_args.kwargs["filters"] == filters
        by_id = {r.memory_id: r for r in page}
        assert by_id[head.id].supersede_candidate is None
        assert by_id[other.id].supersede_candidate == hint
