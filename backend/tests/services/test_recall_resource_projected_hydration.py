"""Recall hydration for resource-projected memories (Issue #972).

Resource-projected memories (written by ``ResourceIndexer._apply_upsert``) store
their Qdrant point under a *deterministic* id ``uuid5(resource_id:doc_id:vN)``
that is DISTINCT from ``Memory.id``; the link from row → point is recorded in
``Memory.summary_embedding_id`` (the column whose documented purpose is
"Qdrant point ID", ``models/memory.py``). ``remember()``-written memories instead
use ``point.id == Memory.id`` (and set ``summary_embedding_id == id``).

``recall`` hydrates Qdrant hits back to PG rows. If it maps a hit solely by
``Memory.id == point.id`` it silently drops EVERY resource-projected hit
(``memories.get(point_id)`` → None → ``continue``), so recall returns 0 results
in all modes even though ``memory_count > 0`` and ``errors == 0``. These tests
pin that recall resolves a hit via the authoritative ``summary_embedding_id``
link, with ``Memory.id`` retained as the fallback for normal memories.

Mirrors the mocked-service pattern of ``test_recall_trust_tier_filter.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from models.schemas import RecallRequest


def _fake_resource_memory(mem_id, point_id):
    """A resource-projected Memory whose Qdrant point id != its row id."""
    m = MagicMock()
    m.id = mem_id
    m.summary_embedding_id = point_id  # the Qdrant point id (uuid5), != m.id
    m.summary = "[innoxia_dictionary] drug-214814 v2"
    m.context_summary = "ソラナックス 一般名 アルプラゾラム ..."
    m.type = "resource_data"
    m.importance = 0.6
    m.scope = "working"
    m.created_at = datetime(2026, 6, 10, tzinfo=UTC)
    m.client = "resource_indexer"
    m.tags = []
    m.context = {"context_id": str(uuid4())}
    m.source_uri = None
    m.source_type = "connector"
    return m


def _make_service(memories_returned):
    from services.memory_service import MemoryService

    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = memories_returned
    db.execute = AsyncMock(return_value=result)
    svc = MemoryService(db)
    svc.search_service = MagicMock()
    svc.memory_repo = MagicMock()
    svc.memory_repo.update_access_stats = AsyncMock()
    # Skip the auto-promotion side-effect (writes); not under test here.
    svc._check_and_promote = AsyncMock()
    return svc, db


def _compiled_selects(db) -> str:
    sqls = []
    for call in db.execute.call_args_list:
        if call.args:
            try:
                sqls.append(str(call.args[0].compile(compile_kwargs={"literal_binds": False})))
            except Exception:
                sqls.append(str(call.args[0]))
    return " ".join(sqls)


@pytest.mark.asyncio
async def test_recall_returns_resource_projected_hit_via_summary_embedding_id():
    """A hit whose point id == summary_embedding_id (not Memory.id) must survive
    hydration. Pre-fix this returned 0 results (the regression)."""
    mem_id = uuid4()
    point_id = uuid4()  # stands in for uuid5(resource:doc:vN); != mem_id
    assert mem_id != point_id

    svc, _db = _make_service([_fake_resource_memory(mem_id, point_id)])
    # hybrid_search returns the Qdrant point id, exactly as the indexer wrote it.
    svc.search_service.hybrid_search = AsyncMock(
        return_value=[{"id": str(point_id), "score": 0.91}]
    )

    resp = await svc.recall(
        request=RecallRequest(query="ソラナックス", k=5),
        user_id="u",
        current_context_id=uuid4(),
        current_workspace_id=uuid4(),
    )

    assert len(resp.results) == 1, "resource-projected hit must not be dropped"
    assert resp.results[0].memory_id == mem_id


@pytest.mark.asyncio
async def test_recall_still_resolves_normal_memory_by_id():
    """Regression guard: normal memories (point.id == Memory.id) keep working."""
    mem_id = uuid4()
    m = _fake_resource_memory(mem_id, mem_id)  # summary_embedding_id == id (normal)

    svc, _db = _make_service([m])
    svc.search_service.hybrid_search = AsyncMock(return_value=[{"id": str(mem_id), "score": 0.88}])

    resp = await svc.recall(
        request=RecallRequest(query="q", k=5),
        user_id="u",
        current_context_id=uuid4(),
        current_workspace_id=uuid4(),
    )

    assert len(resp.results) == 1
    assert resp.results[0].memory_id == mem_id


@pytest.mark.asyncio
async def test_candidate_fetch_selects_by_summary_embedding_id():
    """SQL pin: the candidate fetch must query summary_embedding_id so a future
    refactor cannot silently drop the resource-projection mapping."""
    svc, db = _make_service([])
    svc.search_service.hybrid_search = AsyncMock(return_value=[{"id": str(uuid4()), "score": 0.9}])

    await svc.recall(
        request=RecallRequest(query="q", k=5),
        user_id="u",
        current_context_id=uuid4(),
        current_workspace_id=uuid4(),
    )

    # Normalize whitespace so the multi-line compiled WHERE is matchable.
    sql = " ".join(_compiled_selects(db).split())
    # ``summary_embedding_id`` always appears in the SELECT column list; the pin
    # is that it ALSO appears in a WHERE ``IN`` predicate (the resource-point
    # mapping), which a regression dropping the ``or_`` branch would remove.
    assert "summary_embedding_id IN" in sql, (
        "recall candidate fetch must match resource points on summary_embedding_id"
    )
