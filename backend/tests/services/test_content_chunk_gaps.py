"""#2107: stamping content chunk gaps and reconciling them once a context is active."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from services import content_chunk_gaps as gaps
from utils.content_chunks import CONTENT_CHUNK_MIN_CONTENT_CHARS, content_hash

pytestmark = pytest.mark.asyncio

LONG = "y" * (CONTENT_CHUNK_MIN_CONTENT_CHARS * 2)
GAP_AT = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
ROOM = 10**6


def _sql(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


def _result(*, rows=(), scalars=(), rowcount=1):
    result = MagicMock()
    result.all.return_value = list(rows)
    result.scalars.return_value.all.return_value = list(scalars)
    result.tuples.return_value.all.return_value = list(rows)
    result.scalar_one.return_value = 0
    result.rowcount = rowcount
    return result


def _db(*results, scalar=None):
    db = MagicMock()
    db.execute = AsyncMock(side_effect=list(results) if results else None)
    if not results:
        db.execute.return_value = _result()
    db.scalar = AsyncMock(return_value=scalar)
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


@pytest.fixture(autouse=True)
def _fresh_rotation(monkeypatch):
    monkeypatch.setattr(gaps, "_rotation", gaps._Rotation())


@pytest.fixture
def qdrant():
    with (
        patch.object(gaps, "chunk_points_supported", return_value=True),
        patch.object(gaps, "chunk_points_content_hashes", AsyncMock(return_value={})) as hashes,
    ):
        yield hashes


def _ids(n):
    return sorted(uuid4() for _ in range(n))


# --- record_content_chunk_gap ------------------------------------------------


@pytest.fixture
def isolated():
    session = MagicMock()
    session.execute = AsyncMock(return_value=_result(rowcount=1))
    session.commit = AsyncMock()

    @asynccontextmanager
    async def factory(_db):
        yield session

    with patch.object(gaps, "isolated_session", factory):
        yield session


async def test_a_due_stamp_is_written_on_its_own_session(isolated):
    ctx = uuid4()
    db = _db(scalar=ctx)
    assert await gaps.record_content_chunk_gap(db, ctx, reason="t") is True
    # The due check on the caller's session, in a SAVEPOINT; no write there.
    db.begin_nested.assert_called_once()
    db.execute.assert_not_awaited()
    check = _sql(db.scalar.await_args.args[0])
    assert "content_chunks_gap_at IS NULL OR" in check
    assert "make_interval" in check
    # The write: lock timeout first, then the throttled UPDATE, committed.
    timeout, write = (c.args[0] for c in isolated.execute.await_args_list)
    assert "lock_timeout" in str(timeout)
    sql = _sql(write)
    assert "content_chunks_gap_at IS NULL OR" in sql
    assert "content_chunks_gap_cursor=" in sql
    assert "updated_at=context_search_configs.updated_at" in sql
    isolated.commit.assert_awaited_once()


async def test_a_fresh_stamp_stops_at_the_check(isolated):
    db = _db(scalar=None)
    assert await gaps.record_content_chunk_gap(db, uuid4(), reason="t") is False
    isolated.execute.assert_not_awaited()


async def test_a_failed_stamp_is_logged_not_raised(isolated):
    import structlog

    ctx = uuid4()
    isolated.execute = AsyncMock(side_effect=RuntimeError("lock timeout"))
    with structlog.testing.capture_logs() as logs:
        assert await gaps.record_content_chunk_gap(_db(scalar=ctx), ctx, reason="t") is False
    assert any(e["event"] == "content_chunks_gap_stamp_failed" for e in logs)


async def test_no_context_no_stamp(isolated):
    db = _db()
    assert await gaps.record_content_chunk_gap(db, None, reason="t") is False
    db.scalar.assert_not_awaited()


# --- queue_content_chunk_backfill ---------------------------------------------


async def test_the_backfill_with_ids_keeps_the_d4_fences(qdrant):
    db = _db()
    await gaps.queue_content_chunk_backfill(db, uuid4(), memory_ids=_ids(2))
    sql = _sql(db.execute.await_args.args[0])
    for clause in (
        "memories.deleted_at IS NULL",
        "char_length(memories.content) >=",
        "memories.resource_id IS NULL",
        "memories.embedding_status =",
        "memories.id IN",
    ):
        assert clause in sql
    assert "SET embedding_status=" in sql
    assert "updated_at" not in sql.split("SET", 1)[1].split("WHERE", 1)[0]


async def test_the_backfill_with_no_ids_writes_nothing(qdrant):
    db = _db()
    assert await gaps.queue_content_chunk_backfill(db, uuid4(), memory_ids=[]) == 0
    db.execute.assert_not_awaited()


async def test_the_backfill_is_a_noop_without_chunk_points():
    db = _db()
    with patch.object(gaps, "chunk_points_supported", return_value=False):
        assert await gaps.queue_content_chunk_backfill(db, uuid4()) == 0
    db.execute.assert_not_awaited()


# --- _without_current_chunks --------------------------------------------------


async def test_only_rows_without_current_chunks_are_reported(qdrant):
    missing, stale, current, no_hash = _ids(4)
    qdrant.return_value = {
        str(stale): "other-text",
        str(current): content_hash(LONG),
        str(no_hash): None,  # written before the hash existed
    }
    rows = [(missing, LONG), (stale, LONG), (current, LONG), (no_hash, LONG)]
    db = _db(_result(rows=rows))
    out = await gaps._without_current_chunks(db, [missing, stale, current, no_hash], "served")
    assert out == sorted([missing, stale])
    qdrant.assert_awaited_once_with([missing, stale, current, no_hash], "served")
    # The content is read for this batch only, the length test with it.
    sql = _sql(db.execute.await_args.args[0])
    assert "memories.id IN" in sql and "char_length(memories.content) >=" in sql
    db.commit.assert_awaited_once()


async def test_no_long_rows_no_vector_store_call(qdrant):
    assert await gaps._without_current_chunks(_db(_result()), _ids(2), "served") == []
    qdrant.assert_not_awaited()


# --- _reconcile_context -------------------------------------------------------


@pytest.fixture
def parts():
    """The reconciler's helpers, so a test sets only what it needs."""
    with (
        patch.object(gaps, "count_long_rows_waiting", AsyncMock(return_value=0)) as backlog,
        patch.object(gaps, "_without_current_chunks", AsyncMock(return_value=[])) as check,
        patch.object(gaps, "queue_content_chunk_backfill", AsyncMock(return_value=0)) as queue,
        patch.object(gaps, "_move_stamp", AsyncMock(return_value=True)) as move,
        patch(
            "services.context_routing.resolve_collection_name",
            AsyncMock(return_value="served"),
        ),
    ):
        yield SimpleNamespace(backlog=backlog, check=check, queue=queue, move=move)


async def test_a_short_page_requeues_what_is_missing_and_clears(qdrant, parts):
    ids = _ids(4)
    parts.check.return_value = [ids[0], ids[2]]
    parts.queue.return_value = 2
    out = gaps.GapReconcileResult()
    ctx = uuid4()
    db = _db(_result(scalars=ids), _result())
    await gaps._reconcile_context(db, ctx, GAP_AT, None, out, global_room=ROOM)
    # The re-queue waits a bounded time behind another writer's row lock.
    assert "lock_timeout" in str(db.execute.await_args_list[1].args[0])

    parts.check.assert_awaited_once_with(ANY, ids, "served")
    parts.queue.assert_awaited_once_with(ANY, ctx, memory_ids=[ids[0], ids[2]])
    assert (out.scanned, out.requeued, out.cleared) == (4, 2, 1)
    assert parts.move.await_args.args[1:] == (ctx, GAP_AT)
    assert parts.move.await_args.kwargs == {"cursor": ids[-1], "clear": True}
    # The page reads cheap columns only: no content, no length test.
    page_sql = _sql(db.execute.await_args_list[0].args[0])
    assert "char_length" not in page_sql and "memories.content" not in page_sql
    assert "ORDER BY memories.id" in page_sql


async def test_a_full_page_advances_the_cursor_in_check_batches(qdrant, parts, monkeypatch):
    monkeypatch.setattr(gaps, "RECONCILE_SCAN_BATCH", 4)
    monkeypatch.setattr(gaps, "RECONCILE_CHECK_BATCH", 2)
    ids = _ids(4)
    out = gaps.GapReconcileResult()
    await gaps._reconcile_context(
        _db(_result(scalars=ids)), uuid4(), GAP_AT, None, out, global_room=ROOM
    )
    assert [c.args[1] for c in parts.check.await_args_list] == [ids[:2], ids[2:]]
    parts.queue.assert_not_awaited()
    assert parts.move.await_args.kwargs == {"cursor": ids[-1], "clear": False}
    assert out.cleared == 0


async def test_the_requeue_is_capped_to_the_backlog_room(qdrant, parts, monkeypatch):
    monkeypatch.setattr(gaps, "RECONCILE_CHECK_BATCH", 2)
    parts.backlog.return_value = gaps.RECONCILE_BACKLOG_LIMIT - 2
    ids = _ids(6)
    parts.check.side_effect = [[ids[0]], [ids[2], ids[3]], [ids[4]]]
    await gaps._reconcile_context(
        _db(_result(scalars=ids), _result()),
        uuid4(),
        GAP_AT,
        None,
        gaps.GapReconcileResult(),
        global_room=ROOM,
    )
    # Room for two: stop there and resume right after the last re-queued row.
    assert parts.check.await_count == 2
    assert parts.queue.await_args.kwargs == {"memory_ids": [ids[0], ids[2]]}
    assert parts.move.await_args.kwargs == {"cursor": ids[2], "clear": False}


async def test_the_scan_resumes_after_the_cursor(qdrant, parts):
    cursor = uuid4()
    db = _db(_result())
    await gaps._reconcile_context(
        db, uuid4(), GAP_AT, cursor, gaps.GapReconcileResult(), global_room=ROOM
    )
    assert "memories.id >" in _sql(db.execute.await_args_list[0].args[0])
    parts.check.assert_not_awaited()
    assert parts.move.await_args.kwargs == {"cursor": cursor, "clear": True}


async def test_a_backlog_defers_the_context(qdrant, parts):
    parts.backlog.return_value = gaps.RECONCILE_BACKLOG_LIMIT
    out = gaps.GapReconcileResult()
    await gaps._reconcile_context(_db(), uuid4(), GAP_AT, None, out, global_room=ROOM)
    assert out.deferred == 1
    parts.check.assert_not_awaited()
    parts.move.assert_not_awaited()


async def test_a_stamp_moved_by_a_new_gap_is_not_cleared(qdrant, parts):
    parts.move.return_value = False
    out = gaps.GapReconcileResult()
    await gaps._reconcile_context(_db(_result()), uuid4(), GAP_AT, None, out, global_room=ROOM)
    assert out.cleared == 0


async def test_move_stamp_is_fenced_on_the_read_stamp():
    db = _db()
    await gaps._move_stamp(db, uuid4(), GAP_AT, cursor=None, clear=True)
    sql = _sql(db.execute.await_args.args[0])
    assert "context_search_configs.content_chunks_gap_at =" in sql.split("WHERE", 1)[1]
    assert "content_chunks_gap_at=" in sql.split("WHERE", 1)[0]
    assert "updated_at=context_search_configs.updated_at" in sql
    db.commit.assert_awaited_once()


# --- reconcile_content_chunk_gaps ---------------------------------------------


async def test_a_store_without_chunk_points_does_nothing():
    db = _db()
    with patch.object(gaps, "chunk_points_supported", return_value=False):
        out = await gaps.reconcile_content_chunk_gaps(db)
    assert out == gaps.GapReconcileResult()
    db.execute.assert_not_awaited()


def _stamped(n):
    return [(context_id, GAP_AT, None) for context_id in _ids(n)]


async def test_only_active_stamped_contexts_are_reconciled(qdrant, monkeypatch):
    monkeypatch.setattr(gaps, "RECONCILE_CONTEXTS_PER_TICK", 2)
    stamped = _stamped(4)
    active = {row[0] for row in stamped[1:]}
    with (
        patch.object(gaps, "_stamped_contexts", AsyncMock(return_value=stamped)),
        patch.object(gaps, "content_chunks_effective_ids", AsyncMock(return_value=active)),
        patch.object(gaps, "_global_backlog", AsyncMock(return_value=0)),
        patch.object(gaps, "_reconcile_context", AsyncMock(return_value=0)) as one,
    ):
        out = await gaps.reconcile_content_chunk_gaps(_db())
    assert [c.args[1] for c in one.await_args_list] == [stamped[1][0], stamped[2][0]]
    assert (out.stamped, out.active) == (4, 2)
    # More active ones than slots: the next tick starts after the last picked.
    assert gaps._rotation.after == stamped[2][0]


async def test_the_read_goes_round_all_stamped_contexts(qdrant, monkeypatch):
    """Contexts that stay inactive, or keep failing, never hold the others back."""
    monkeypatch.setattr(gaps, "STAMPED_READ_LIMIT", 2)
    first, second = _stamped(2), _stamped(1)
    reads = AsyncMock(side_effect=[first, second, [], first])
    with (
        patch.object(gaps, "_stamped_contexts", reads),
        patch.object(gaps, "content_chunks_effective_ids", AsyncMock(return_value=set())),
        patch.object(gaps, "_global_backlog", AsyncMock(return_value=0)),
    ):
        await gaps.reconcile_content_chunk_gaps(_db())  # a full read: continue after it
        assert gaps._rotation.after == first[-1][0]
        await gaps.reconcile_content_chunk_gaps(_db())  # a short read: wrap next time
        assert gaps._rotation.after is None
        gaps._rotation.after = second[0][0]
        await gaps.reconcile_content_chunk_gaps(_db())  # nothing after: start over
    assert [c.args[1] for c in reads.await_args_list] == [
        None,
        first[-1][0],
        second[0][0],
        None,
    ]


async def test_one_failing_context_does_not_stop_the_others(qdrant):
    stamped = _stamped(2)
    db = _db()
    with (
        patch.object(gaps, "_stamped_contexts", AsyncMock(return_value=stamped)),
        patch.object(
            gaps,
            "content_chunks_effective_ids",
            AsyncMock(return_value={row[0] for row in stamped}),
        ),
        patch.object(gaps, "_global_backlog", AsyncMock(return_value=0)),
        patch.object(
            gaps, "_reconcile_context", AsyncMock(side_effect=[RuntimeError("q"), 0])
        ) as one,
    ):
        out = await gaps.reconcile_content_chunk_gaps(db)
    assert one.await_count == 2
    assert out.failed == 1
    db.rollback.assert_awaited_once()


async def test_the_stamped_query_waits_for_the_settle_time(qdrant):
    db = _db(_result())
    await gaps._stamped_contexts(db, None)
    sql = _sql(db.execute.await_args.args[0])
    assert "clock_timestamp() - make_interval(" in sql
    assert "context_search_configs.content_chunks_enabled IS true" in sql
    assert "contexts.deleted_at IS NULL" in sql
    assert "ORDER BY context_search_configs.context_id" in sql


async def test_settle_exceeds_the_refresh():
    """The fenced clear relies on it: a gap during a scan always moves the stamp."""
    assert gaps.GAP_SETTLE_SECONDS > gaps.GAP_STAMP_REFRESH_SECONDS


async def test_the_global_backlog_caps_what_a_tick_requeues(qdrant, monkeypatch):
    """Fresh writes are not queued behind a backfill: the room is shared by the tick."""
    monkeypatch.setattr(gaps, "RECONCILE_GLOBAL_BACKLOG_LIMIT", 10)
    stamped = _stamped(3)
    with (
        patch.object(gaps, "_stamped_contexts", AsyncMock(return_value=stamped)),
        patch.object(
            gaps,
            "content_chunks_effective_ids",
            AsyncMock(return_value={row[0] for row in stamped}),
        ),
        patch.object(gaps, "_global_backlog", AsyncMock(return_value=4)),
        patch.object(gaps, "_reconcile_context", AsyncMock(side_effect=[5, 1, 0])) as one,
    ):
        await gaps.reconcile_content_chunk_gaps(_db())
    assert [c.kwargs["global_room"] for c in one.await_args_list] == [6, 1, 0]


async def test_a_spent_global_room_defers(qdrant, parts):
    out = gaps.GapReconcileResult()
    await gaps._reconcile_context(_db(), uuid4(), GAP_AT, None, out, global_room=0)
    assert out.deferred == 1
    parts.check.assert_not_awaited()


async def test_the_backfill_locks_rows_in_id_order(qdrant):
    db = _db()
    await gaps.queue_content_chunk_backfill(db, uuid4())
    sql = _sql(db.execute.await_args.args[0])
    assert "memories.id IN (SELECT memories.id" in sql
    assert "ORDER BY memories.id" in sql and "FOR NO KEY UPDATE" in sql


async def test_the_waiting_count_with_and_without_failed_rows():
    db = _db()
    await gaps.count_long_rows_waiting(db, uuid4(), include_failed=True)
    await gaps.count_long_rows_waiting(db, uuid4(), include_failed=False)
    with_failed, without = (_sql(c.args[0]) for c in db.execute.await_args_list)
    assert "memories.embedding_status !=" in with_failed
    assert "memories.embedding_status IN" in without
    for sql in (with_failed, without):
        assert "char_length(memories.content) >=" in sql
        assert "memories.resource_id IS NULL" in sql


# --- seed_content_chunk_gaps --------------------------------------------------


async def test_the_seed_does_nothing_without_the_gate(qdrant):
    db = _db()
    with patch.object(gaps, "content_chunks_gated", return_value=False):
        assert await gaps.seed_content_chunk_gaps(db) is None
    # No marker either: turning the gate on later stamps at the next start.
    db.execute.assert_not_awaited()


async def test_the_seed_stamps_once_with_the_marker_in_the_same_transaction(qdrant):
    marker = MagicMock()
    marker.scalar_one_or_none.return_value = 1
    db = _db(marker, _result(rowcount=3))
    with patch.object(gaps, "content_chunks_gated", return_value=True):
        assert await gaps.seed_content_chunk_gaps(db) == 3
    insert, stamp = (_sql(c.args[0]) for c in db.execute.await_args_list)
    assert "INSERT INTO config_overrides" in insert and "ON CONFLICT (key) DO NOTHING" in insert
    assert "content_chunks_enabled IS true" in stamp
    assert "content_chunks_gap_at IS NULL" in stamp
    assert "updated_at=context_search_configs.updated_at" in stamp
    db.commit.assert_awaited_once()


async def test_a_second_start_finds_the_marker_and_stamps_nothing(qdrant):
    marker = MagicMock()
    marker.scalar_one_or_none.return_value = None
    db = _db(marker)
    with patch.object(gaps, "content_chunks_gated", return_value=True):
        assert await gaps.seed_content_chunk_gaps(db) is None
    assert db.execute.await_count == 1
    db.commit.assert_awaited_once()
