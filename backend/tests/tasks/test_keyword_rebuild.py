"""#2984: the keyword vector rebuild (``tasks.keyword_rebuild``).

The vector store is a dict here (``_Store``); the pages are given rows. The
DB-backed run is in tests/integration/test_keyword_rebuild_db.py.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from qdrant_client.models import SparseVector
from sqlalchemy.dialects import postgresql

from services import keyword_stats
from services.memory_service import build_memory_point, memory_keyword_tokens
from services.resource_indexer import resource_point_id
from tasks import keyword_rebuild as task
from utils.sparse_vector import MemoryFieldStats, build_resource_sparse_vector
from utils.tokenizer import tokenize_for_search

STATS = MemoryFieldStats(summary=6.0, context_summary=20.0, content=90.0, summary_reading=8.0)
RESOURCE_AVGDL = 30.0


def _row(context_id: UUID, **overrides: Any) -> MagicMock:
    memory = MagicMock()
    memory.id = uuid4()
    memory.point_id = memory.id
    memory.context_id = context_id
    memory.user_id = "u"
    memory.summary = "認証エラーの対処 認証"
    memory.context_summary = "トークン期限切れ"
    memory.content = "期限切れのトークンで認証エラーが出たら更新する。" * 3
    memory.type = "note"
    memory.importance = 0.5
    memory.scope = "working"
    memory.tags = []
    memory.client = "pytest"
    memory.context = None
    memory.created_at = datetime(2026, 1, 1)
    memory.updated_at = datetime(2026, 1, 1)
    memory.location_lat = memory.location_lon = None
    memory.observed_at = None
    memory.event_start = None
    memory.summary_embedding_id = memory.id
    memory.resource_id = memory.resource_doc_id = memory.resource_version = None
    for column in keyword_stats.LENGTH_COLUMNS:
        setattr(memory, column, None)
    for key, value in overrides.items():
        setattr(memory, key, value)
    return memory


def _resource_row(context_id: UUID) -> MagicMock:
    point = resource_point_id("docs", "d1", 1)
    return _row(
        context_id,
        summary="[docs] d1 v1",
        resource_id="docs",
        resource_doc_id="d1",
        resource_version=1,
        summary_embedding_id=point,
        point_id=point,
    )


class _Store:
    """The points of one collection: payload and bm25 vector by point id."""

    def __init__(self) -> None:
        self.points: dict[str, dict[str, Any]] = {}
        self.payload_writes = 0
        self.vector_writes = 0

    def add(self, point_id: UUID, payload: dict[str, Any], indices=(), values=()) -> None:
        self.points[str(point_id)] = {
            "payload": dict(payload),
            "bm25": (list(indices), list(values)),
        }

    def vector(self, point_id: UUID) -> dict[int, float]:
        indices, values = self.points[str(point_id)]["bm25"]
        return dict(zip(indices, values, strict=True))

    async def retrieve(self, point_ids, collection, *, with_bm25=False):
        out = {}
        for point_id in point_ids:
            point = self.points.get(str(point_id))
            if point is None:
                continue
            vector = None
            if with_bm25:
                indices, values = point["bm25"]
                vector = {"bm25": SparseVector(indices=indices, values=values)}
            out[str(point_id)] = SimpleNamespace(
                id=str(point_id), payload=dict(point["payload"]), vector=vector
            )
        return out

    async def write(self, collection, *, payloads=(), vectors=()):
        for point_id, payload in payloads:
            self.points[str(point_id)]["payload"].update(payload)
            self.payload_writes += 1
        for point_id, indices, values in vectors:
            self.points[str(point_id)]["bm25"] = (list(indices), list(values))
            self.vector_writes += 1


def _db() -> MagicMock:
    db = MagicMock()
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    return db


def _pages(rows_by_page: list[tuple[list[UUID], list[Any]]]):
    """``_locked_page`` answering each pass with the same pages."""
    calls = {"n": 0}

    async def locked_page(db, context_id, after_id, page_size):
        index = calls["n"] % (len(rows_by_page) + 1)
        calls["n"] += 1
        if index == len(rows_by_page):
            return [], []
        return rows_by_page[index]

    return locked_page


async def _run(store: _Store, context_id: UUID, pages, **kwargs) -> task.KeywordRebuild:
    with (
        patch.object(task, "_active_store", return_value=None),
        patch.object(task, "_contexts", AsyncMock(return_value=([context_id], False))),
        patch.object(task, "resolve_collection_name", AsyncMock(return_value="coll")),
        patch.object(task, "_locked_page", side_effect=_pages(pages)),
        patch.object(task, "retrieve_keyword_points", side_effect=store.retrieve),
        patch.object(task, "write_keyword_points", side_effect=store.write),
        patch.object(task, "memory_field_stats", AsyncMock(return_value=STATS)) as stats,
        patch.object(
            task, "resource_fulltext_avgdl", AsyncMock(return_value=RESOURCE_AVGDL)
        ) as avgdl,
    ):
        result = await task.run_keyword_rebuild(
            _db(), context_id=context_id, dry_run=False, **kwargs
        )
    assert stats.await_args.kwargs == {"fresh": True}
    assert avgdl.await_args.kwargs == {"fresh": True}
    return result


def _legacy_point(store: _Store, memory, context_id: UUID) -> dict[str, Any]:
    """A point as written before #2984: raw tf weights (no statistics)."""
    payload, indices, values = build_memory_point(memory)
    payload["context_id"] = str(context_id)
    store.add(memory.point_id, payload, list(indices), [v * 3 for v in values])
    return payload


@pytest.mark.asyncio
async def test_rebuilt_vector_equals_the_one_the_writer_builds_with_the_same_statistics():
    context_id = uuid4()
    memory = _row(context_id)
    store = _Store()
    _legacy_point(store, memory, context_id)

    result = await _run(store, context_id, [([memory.id], [memory])])

    _, indices, values = build_memory_point(memory, STATS)
    rebuilt = store.vector(memory.point_id)
    assert rebuilt.keys() == set(indices)
    for index, value in zip(indices, values, strict=True):
        assert rebuilt[index] == pytest.approx(value)
    assert result.vectors_written == 1
    assert result.lengths_written == 1
    assert result.tokens_rewritten == 0
    lengths = keyword_stats.memory_keyword_lengths(store.points[str(memory.id)]["payload"])
    assert {c: getattr(memory, c) for c in lengths} == lengths


@pytest.mark.asyncio
async def test_a_second_run_writes_nothing():
    context_id = uuid4()
    memory, resource = _row(context_id), _resource_row(context_id)
    store = _Store()
    _legacy_point(store, memory, context_id)
    store.add(
        resource.point_id, {"content": "Title: 認証 認証 手順", "context_id": str(context_id)}
    )
    pages = [([memory.id, resource.id], [memory, resource])]

    first = await _run(store, context_id, pages)
    assert first.vectors_written == 2
    before = {pid: dict(p["payload"]) for pid, p in store.points.items()}
    vectors = {pid: store.vector(UUID(pid)) for pid in store.points}
    writes = (store.payload_writes, store.vector_writes)

    second = await _run(store, context_id, pages)

    assert second.vectors_written == second.lengths_written == second.tokens_rewritten == 0
    assert second.unchanged == 2
    assert (store.payload_writes, store.vector_writes) == writes
    assert {pid: p["payload"] for pid, p in store.points.items()} == before
    assert {pid: store.vector(UUID(pid)) for pid in store.points} == vectors


@pytest.mark.asyncio
async def test_stale_token_payload_is_rewritten_from_the_row():
    """A point tokenized by an older tokenizer gets the row's tokens and the vector of them."""
    context_id = uuid4()
    memory = _row(context_id)
    store = _Store()
    payload = _legacy_point(store, memory, context_id)
    store.points[str(memory.id)]["payload"]["content_tokens"] = "old tokens"
    store.points[str(memory.id)]["payload"]["summary"] = payload["summary"]

    result = await _run(store, context_id, [([memory.id], [memory])])

    fields = memory_keyword_tokens(memory).payload_fields()
    stored = store.points[str(memory.id)]["payload"]
    assert {key: stored[key] for key in fields} == fields
    assert result.tokens_rewritten == 1
    _, indices, values = build_memory_point(memory, STATS)
    assert store.vector(memory.point_id) == pytest.approx(dict(zip(indices, values, strict=True)))
    # Only the token fields: the rest of the payload is as it was.
    assert {k: v for k, v in stored.items() if k not in fields} == {
        k: v for k, v in payload.items() if k not in fields
    }


@pytest.mark.asyncio
async def test_resource_rows_use_their_own_average():
    context_id = uuid4()
    resource = _resource_row(context_id)
    text = "Title: 認証 認証 手順 更新"
    store = _Store()
    store.add(resource.point_id, {"content": text, "context_id": str(context_id)}, [1], [5.0])

    result = await _run(store, context_id, [([resource.id], [resource])])

    assert resource.keyword_fulltext_len == len(tokenize_for_search(text).split())
    assert resource.keyword_summary_len is None
    indices, values = build_resource_sparse_vector(text, RESOURCE_AVGDL)
    assert store.vector(resource.point_id) == pytest.approx(dict(zip(indices, values, strict=True)))
    assert result.tokens_rewritten == 0
    assert "summary_tokens" not in store.points[str(resource.point_id)]["payload"]


@pytest.mark.asyncio
async def test_rows_another_writer_holds_are_skipped_and_counted():
    """The page's ids include rows the lock query did not return (locked, not success)."""
    context_id = uuid4()
    taken, busy = _row(context_id), _row(context_id)
    store = _Store()
    _legacy_point(store, taken, context_id)
    busy_vector = _legacy_point(store, busy, context_id)
    before = store.vector(busy.point_id)

    result = await _run(store, context_id, [([taken.id, busy.id], [taken])])

    assert result.skipped == 2  # once per pass
    assert store.vector(busy.point_id) == before
    assert busy_vector == {**store.points[str(busy.id)]["payload"]}
    assert result.vectors_written == 1


@pytest.mark.asyncio
async def test_a_point_out_of_step_with_its_row_is_skipped_in_the_vector_pass():
    """A writer changed the point between the passes: its lengths no longer match the row."""
    context_id = uuid4()
    memory = _row(context_id)
    store = _Store()
    _legacy_point(store, memory, context_id)
    original = task._vectors_page

    async def vectors_page(*args, **kwargs):
        store.points[str(memory.id)]["payload"]["content_tokens"] = "a newer write"
        return await original(*args, **kwargs)

    with patch.object(task, "_vectors_page", side_effect=vectors_page):
        result = await _run(store, context_id, [([memory.id], [memory])])

    assert result.skipped == 1
    assert result.vectors_written == 0


@pytest.mark.asyncio
async def test_a_row_without_a_point_in_the_context_is_counted_not_written():
    context_id = uuid4()
    gone, elsewhere = _row(context_id), _row(context_id)
    store = _Store()
    payload = _legacy_point(store, elsewhere, context_id)
    store.points[str(elsewhere.id)]["payload"] = {**payload, "context_id": str(uuid4())}

    result = await _run(store, context_id, [([gone.id, elsewhere.id], [gone, elsewhere])])

    assert result.missing_point == 4  # two rows, two passes
    assert result.written == 0
    assert gone.keyword_summary_len is None


@pytest.mark.asyncio
async def test_lance_store_writes_nothing():
    db = _db()
    with (
        patch.object(task, "_active_store", return_value=object()),
        patch.object(task, "_contexts", AsyncMock()) as contexts,
    ):
        result = await task.run_keyword_rebuild(db, dry_run=False)
    assert result.store == "lance"
    contexts.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_plan_counts_and_touches_no_vector_store():
    context_id = uuid4()
    db = _db()
    counted = MagicMock()
    counted.one.return_value = (7, 3)
    db.execute = AsyncMock(return_value=counted)
    with (
        patch.object(task, "_active_store", return_value=None),
        patch.object(task, "_contexts", AsyncMock(return_value=([context_id], False))),
        patch.object(task, "retrieve_keyword_points", AsyncMock()) as retrieve,
        patch.object(task, "write_keyword_points", AsyncMock()) as write,
    ):
        result = await task.run_keyword_rebuild(db, dry_run=True)
    assert (result.rows, result.rows_without_lengths, result.contexts) == (7, 3, 1)
    retrieve.assert_not_awaited()
    write.assert_not_awaited()
    sql = str(
        db.execute.await_args.args[0].compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "memories.embedding_status = 'success'" in sql
    assert "memories.deleted_at IS NULL" in sql


@pytest.mark.asyncio
async def test_limit_stops_with_a_resume_point():
    first, second = uuid4(), uuid4()
    db = _db()
    counted = MagicMock()
    counted.one.return_value = (0, 0)
    db.execute = AsyncMock(return_value=counted)
    with (
        patch.object(task, "_active_store", return_value=None),
        patch.object(task, "_contexts", AsyncMock(return_value=([first, second], True))) as ctxs,
    ):
        cursor = uuid4()
        result = await task.run_keyword_rebuild(db, after=cursor, limit=2)
    assert ctxs.await_args.args == (db, None, cursor, 2)
    assert result.stopped == "limit"
    assert result.last_context == second


@pytest.mark.asyncio
async def test_contexts_are_taken_by_id_after_the_cursor():
    after = uuid4()
    ids = [uuid4(), uuid4(), uuid4()]
    db = _db()
    listed = MagicMock()
    listed.scalars.return_value.all.return_value = ids
    db.execute = AsyncMock(return_value=listed)

    chosen, more = await task._contexts(db, None, after, 2)

    assert (chosen, more) == (ids[:2], True)
    sql = str(
        db.execute.await_args.args[0].compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert f"contexts.id > '{after}'" in sql
    assert "contexts.deleted_at IS NULL" in sql
    assert "ORDER BY contexts.id" in sql
    assert "LIMIT 3" in sql


@pytest.mark.asyncio
async def test_the_page_locks_only_live_embedded_rows_and_skips_locked_ones():
    context_id, after = uuid4(), uuid4()
    db = _db()
    ids = MagicMock()
    ids.scalars.return_value.all.return_value = [uuid4()]
    rows = MagicMock()
    rows.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(side_effect=[ids, rows])

    await task._locked_page(db, context_id, after, 50)

    page_sql, lock_sql = (
        str(c.args[0].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
        for c in db.execute.await_args_list
    )
    assert f"memories.id > '{after}'" in page_sql
    assert "LIMIT 50" in page_sql
    assert "FOR UPDATE" not in page_sql
    assert "FOR NO KEY UPDATE SKIP LOCKED" in lock_sql
    assert "memories.embedding_status = 'success'" in lock_sql
    assert "memories.embedding_suppressed_at IS NULL" in lock_sql
