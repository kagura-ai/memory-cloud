"""#2015: writing a memory's content chunk points."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services import content_chunk_writer as writer
from utils.content_chunks import (
    CHUNK_OF_FIELD,
    CONTENT_CHUNK_MIN_CONTENT_CHARS,
    chunk_point_id,
    split_content,
)

pytestmark = pytest.mark.asyncio

WS = "11111111-1111-4111-8111-111111111111"
CTX = "22222222-2222-4222-8222-222222222222"


def _memory(content, **extra):
    fields = {
        "id": uuid4(),
        "user_id": "u",
        "workspace_id": WS,
        "context_id": CTX,
        "content": content,
        "resource_id": None,
    }
    fields.update(extra)
    return SimpleNamespace(**fields)


def _db(flag):
    result = MagicMock()
    result.scalar_one_or_none.return_value = flag
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    return db


def _embed_svc():
    svc = MagicMock()
    svc.embed_batch = AsyncMock(side_effect=lambda texts, *a, **k: [[0.1] for _ in texts])
    return svc


LONG = "x" * (CONTENT_CHUNK_MIN_CONTENT_CHARS * 3)


async def test_flag_on_short_content_still_deletes_old_chunks():
    """Shrink below the split threshold: the old body text must not survive."""
    memory = _memory("short")
    with (
        patch.object(writer, "chunk_points_supported", return_value=True),
        patch.object(writer, "delete_chunk_points", AsyncMock()) as delete,
        patch.object(writer, "upsert_chunk_points", AsyncMock()) as upsert,
    ):
        svc = _embed_svc()
        n = await writer.write_content_chunks(
            _db(True), memory, base_payload={}, embed_svc=svc, collection_name="c"
        )
    assert n == 0
    delete.assert_awaited_once_with([memory.id], "c")
    svc.embed_batch.assert_not_awaited()
    upsert.assert_not_awaited()


async def test_a_preloaded_flag_skips_the_lookup():
    db = _db(True)
    with (
        patch.object(writer, "chunk_points_supported", return_value=True),
        patch.object(writer, "delete_chunk_points", AsyncMock()),
        patch.object(writer, "upsert_chunk_points", AsyncMock()) as upsert,
    ):
        await writer.write_content_chunks(
            db,
            _memory(LONG),
            base_payload={},
            embed_svc=_embed_svc(),
            collection_name="c",
            chunks_enabled=False,
        )
    db.execute.assert_not_awaited()
    upsert.assert_not_awaited()


async def test_no_paid_embedding_on_a_store_without_chunk_points():
    """LanceDB: no chunk points can be stored, so nothing is embedded."""
    svc = _embed_svc()
    with (
        patch.object(writer, "chunk_points_supported", return_value=False),
        patch.object(writer, "delete_chunk_points", AsyncMock()),
        patch.object(writer, "upsert_chunk_points", AsyncMock()) as upsert,
    ):
        n = await writer.write_content_chunks(
            _db(True), _memory(LONG), base_payload={}, embed_svc=svc, collection_name="c"
        )
    assert n == 0
    svc.embed_batch.assert_not_awaited()
    upsert.assert_not_awaited()


async def test_failure_log_carries_the_error_class_only():
    import structlog

    svc = MagicMock()
    svc.embed_batch = AsyncMock(side_effect=RuntimeError("secret body text"))
    with (
        structlog.testing.capture_logs() as logs,
        patch.object(writer, "chunk_points_supported", return_value=True),
        patch.object(writer, "delete_chunk_points", AsyncMock()),
        patch.object(writer, "upsert_chunk_points", AsyncMock()),
    ):
        await writer.write_content_chunks(
            _db(True), _memory(LONG), base_payload={}, embed_svc=svc, collection_name="c"
        )
    event = next(e for e in logs if e["event"] == "content_chunks_failed")
    assert event["error_class"] == "RuntimeError"
    assert "error" not in event
    assert "secret body text" not in repr(event)


async def test_flag_off_writes_nothing():
    with (
        patch.object(writer, "delete_chunk_points", AsyncMock()) as delete,
        patch.object(writer, "upsert_chunk_points", AsyncMock()) as upsert,
    ):
        n = await writer.write_content_chunks(
            _db(False), _memory(LONG), base_payload={}, embed_svc=_embed_svc(), collection_name="c"
        )
    assert n == 0
    delete.assert_not_awaited()
    upsert.assert_not_awaited()


async def test_flag_on_replaces_chunks():
    memory = _memory(LONG)
    svc = _embed_svc()
    calls = []
    with (
        patch.object(writer, "chunk_points_supported", return_value=True),
        patch.object(
            writer, "delete_chunk_points", AsyncMock(side_effect=lambda *a: calls.append("delete"))
        ) as delete,
        patch.object(
            writer,
            "upsert_chunk_points",
            AsyncMock(side_effect=lambda *a, **k: calls.append("upsert")),
        ) as upsert,
    ):
        n = await writer.write_content_chunks(
            _db(True),
            memory,
            base_payload={"summary": "s", "summary_tokens": "t"},
            embed_svc=svc,
            collection_name="coll",
        )
    expected = split_content(LONG)
    assert n == len(expected) > 1
    assert calls == ["delete", "upsert"]  # stale chunks go first
    delete.assert_awaited_once_with([memory.id], "coll")
    svc.embed_batch.assert_awaited_once()
    assert svc.embed_batch.await_args.args[0] == expected
    points = upsert.await_args.args[0]
    assert [p[0] for p in points] == [chunk_point_id(memory.id, i) for i in range(n)]
    payload = points[1][2]
    assert payload[CHUNK_OF_FIELD] == str(memory.id)
    assert payload["chunk_text"] == expected[1]
    assert "summary_tokens" not in payload
    kwargs = upsert.await_args.kwargs
    assert kwargs["workspace_id"] == WS and kwargs["context_id"] == CTX
    assert kwargs["collection_name"] == "coll"


async def test_failure_is_fail_open():
    svc = MagicMock()
    svc.embed_batch = AsyncMock(side_effect=RuntimeError("provider down"))
    with (
        patch.object(writer, "chunk_points_supported", return_value=True),
        patch.object(writer, "delete_chunk_points", AsyncMock()),
        patch.object(writer, "upsert_chunk_points", AsyncMock()) as upsert,
    ):
        n = await writer.write_content_chunks(
            _db(True), _memory(LONG), base_payload={}, embed_svc=svc, collection_name="c"
        )
    assert n == 0
    upsert.assert_not_awaited()


async def test_process_pending_embedding_writes_chunks_after_the_summary_point():
    from services import memory_service
    from tests.services.test_embedding_retry_claim import _claimed, _memory_row, _rowcount

    memory_id = uuid4()
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[_claimed(memory_id), _memory_row(memory_id), _rowcount(1)])
    db.commit = AsyncMock()
    db.rollback = AsyncMock()

    async def _aiter():
        yield db

    embed_svc = MagicMock()
    embed_svc.embed = AsyncMock(return_value=[0.1])
    embed_svc.model = "m"
    order = []
    base = {"summary": "s"}

    with (
        patch("db.base.get_db", return_value=_aiter()),
        patch("services.embedding_service.EmbeddingService", MagicMock()),
        patch(
            "services.context_routing.resolve_context_routing",
            AsyncMock(return_value=("coll", embed_svc)),
        ),
        patch.object(
            memory_service,
            "add_memory_to_qdrant",
            AsyncMock(side_effect=lambda **k: order.append("summary")),
        ),
        patch.object(memory_service, "build_memory_point", return_value=(base, [], [])),
        patch.object(
            memory_service,
            "write_content_chunks",
            AsyncMock(side_effect=lambda *a, **k: order.append("chunks") or 0),
        ) as write,
        patch.object(memory_service, "_create_knn_seed_edges", AsyncMock()),
        patch.object(memory_service, "_create_tag_cooccurrence_seed_edges", AsyncMock()),
    ):
        await memory_service.process_pending_embedding(memory_id)

    assert order == ["summary", "chunks"]
    kwargs = write.await_args.kwargs
    assert kwargs["base_payload"] is base
    assert kwargs["embed_svc"] is embed_svc
    assert kwargs["collection_name"] == "coll"


async def test_chunk_cascade_needed_is_cached_per_context():
    writer._FLAG_CACHE.clear()
    db = _db(False)
    ctx = uuid4()
    assert await writer.chunk_cascade_needed(db, ctx) is False
    assert await writer.chunk_cascade_needed(db, ctx) is False
    assert db.execute.await_count == 1
    writer.forget_cached_chunk_flag(ctx)
    db.execute.return_value.scalar_one_or_none.return_value = True
    assert await writer.chunk_cascade_needed(db, ctx) is True


async def test_chunk_cascade_needed_fails_safe():
    writer._FLAG_CACHE.clear()
    db = MagicMock()
    db.execute = AsyncMock(side_effect=RuntimeError("db"))
    assert await writer.chunk_cascade_needed(db, uuid4()) is True


async def test_flag_turned_off_while_embedding_writes_nothing():
    """#2015 race: lookup says on, the flag is turned off and purged while the
    chunks are embedded — the fresh re-read before the upsert stops the write."""
    flags = AsyncMock(side_effect=[True, False])
    with (
        patch.object(writer, "content_chunks_enabled", flags),
        patch.object(writer, "chunk_points_supported", return_value=True),
        patch.object(writer, "delete_chunk_points", AsyncMock()),
        patch.object(writer, "upsert_chunk_points", AsyncMock()) as upsert,
    ):
        n = await writer.write_content_chunks(
            MagicMock(), _memory(LONG), base_payload={}, embed_svc=_embed_svc(), collection_name="c"
        )
    assert n == 0
    assert flags.await_count == 2
    upsert.assert_not_awaited()


async def test_a_preloaded_flag_is_still_re_read_before_the_upsert():
    flags = AsyncMock(return_value=False)
    with (
        patch.object(writer, "content_chunks_enabled", flags),
        patch.object(writer, "chunk_points_supported", return_value=True),
        patch.object(writer, "delete_chunk_points", AsyncMock()),
        patch.object(writer, "upsert_chunk_points", AsyncMock()) as upsert,
    ):
        await writer.write_content_chunks(
            MagicMock(),
            _memory(LONG),
            base_payload={},
            embed_svc=_embed_svc(),
            collection_name="c",
            chunks_enabled=True,
        )
    flags.assert_awaited_once()
    upsert.assert_not_awaited()


async def test_a_failed_delete_propagates():
    """#2015: the old body must not survive an edit — the row takes the normal
    failed/retry path instead."""
    with (
        patch.object(writer, "chunk_points_supported", return_value=True),
        patch.object(writer, "delete_chunk_points", AsyncMock(side_effect=RuntimeError("q"))),
        patch.object(writer, "upsert_chunk_points", AsyncMock()),
        pytest.raises(RuntimeError),
    ):
        await writer.write_content_chunks(
            _db(True), _memory(LONG), base_payload={}, embed_svc=_embed_svc(), collection_name="c"
        )


async def test_upsert_failure_stays_fail_open():
    with (
        patch.object(writer, "chunk_points_supported", return_value=True),
        patch.object(writer, "delete_chunk_points", AsyncMock()),
        patch.object(writer, "upsert_chunk_points", AsyncMock(side_effect=RuntimeError("q"))),
    ):
        n = await writer.write_content_chunks(
            _db(True), _memory(LONG), base_payload={}, embed_svc=_embed_svc(), collection_name="c"
        )
    assert n == 0


# --------------------------------------------------------------------- #2041
# Two overlapping re-embeds of one memory write the same chunk ids. Right
# before its upsert the writer re-reads the row; a run an edit or a newer
# embedding run superseded writes nothing — the newer run writes the chunks.


def _check(current: bool):
    check = MagicMock()
    check.scalar_one_or_none.return_value = uuid4() if current else None
    return check


def _db_superseded_check(still_current: bool, *, current_at_entry: bool = True):
    """Flag lookup, check before the delete, flag re-read, check before the upsert."""
    flag = MagicMock()
    flag.scalar_one_or_none.return_value = True
    db = MagicMock()
    db.execute = AsyncMock(
        side_effect=[flag, _check(current_at_entry), flag, _check(still_current)]
    )
    return db


async def _write_with(db, memory, svc=None):
    with (
        patch.object(writer, "chunk_points_supported", return_value=True),
        patch.object(writer, "delete_chunk_points", AsyncMock()) as delete,
        patch.object(writer, "upsert_chunk_points", AsyncMock()) as upsert,
    ):
        n = await writer.write_content_chunks(
            db, memory, base_payload={}, embed_svc=svc or _embed_svc(), collection_name="c"
        )
    return n, upsert, delete


async def test_a_superseded_run_writes_no_chunks():
    import structlog

    memory = _memory(LONG)
    with structlog.testing.capture_logs() as logs:
        n, upsert, delete = await _write_with(_db_superseded_check(False), memory)
    assert n == 0
    delete.assert_awaited_once()  # current at the start: the old chunks went first
    upsert.assert_not_awaited()
    event = next(e for e in logs if e["event"] == "content_chunks_skipped_superseded")
    assert event["memory_id"] == str(memory.id)
    assert event["stage"] == "before_upsert"
    assert event["log_level"] == "info"


async def test_a_run_superseded_before_it_starts_deletes_nothing():
    """A stale run that reaches the writer after a newer run finished must not
    delete the newer run's chunks (and pays for no embedding)."""
    import structlog

    svc = _embed_svc()
    db = _db_superseded_check(True, current_at_entry=False)
    with structlog.testing.capture_logs() as logs:
        n, upsert, delete = await _write_with(db, _memory(LONG), svc)
    assert n == 0
    delete.assert_not_awaited()
    svc.embed_batch.assert_not_awaited()
    upsert.assert_not_awaited()
    event = next(e for e in logs if e["event"] == "content_chunks_skipped_superseded")
    assert event["stage"] == "before_delete"


async def test_a_failed_check_before_the_delete_does_not_stop_the_run():
    flag = MagicMock()
    flag.scalar_one_or_none.return_value = True
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[flag, RuntimeError("db"), flag, _check(True)])
    n, upsert, delete = await _write_with(db, _memory(LONG))
    assert n == len(split_content(LONG))
    delete.assert_awaited_once()
    upsert.assert_awaited_once()


async def test_a_current_run_writes_its_chunks():
    n, upsert, delete = await _write_with(_db_superseded_check(True), _memory(LONG))
    assert n == len(split_content(LONG))
    upsert.assert_awaited_once()


async def test_the_check_runs_after_the_embedding_and_before_the_upsert():
    """The window an edit can land in is the embedding; check after it."""
    order = []
    results = list(_db_superseded_check(True).execute.side_effect)

    async def _execute(stmt, *a, **k):
        order.append("execute")
        return results.pop(0)

    db = MagicMock()
    db.execute = AsyncMock(side_effect=_execute)
    svc = _embed_svc()
    svc.embed_batch.side_effect = lambda texts, *a, **k: (
        order.append("embed") or [[0.1] for _ in texts]
    )
    with (
        patch.object(writer, "chunk_points_supported", return_value=True),
        patch.object(writer, "delete_chunk_points", AsyncMock()),
        patch.object(
            writer,
            "upsert_chunk_points",
            AsyncMock(side_effect=lambda *a, **k: order.append("upsert")),
        ),
    ):
        await writer.write_content_chunks(
            db, _memory(LONG), base_payload={}, embed_svc=svc, collection_name="c"
        )
    # flag lookup, check, (delete), embed, flag re-read, check, upsert
    assert order == ["execute", "execute", "embed", "execute", "execute", "upsert"]


async def test_the_superseded_check_compares_content_status_and_claim():
    """One indexed lookup by id: live, same content, not re-queued, same claim."""
    from datetime import datetime

    from sqlalchemy.dialects import postgresql

    claimed_at = datetime(2026, 1, 2, 3, 4, 5, 678901)
    memory = _memory(LONG, embedding_attempted_at=claimed_at)
    db = _db_superseded_check(True)
    await _write_with(db, memory)

    stmt = db.execute.await_args_list[3].args[0]
    assert str(stmt) == str(db.execute.await_args_list[1].args[0])  # same check twice
    compiled = stmt.compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "memories.id = " in sql
    assert "memories.deleted_at IS NULL" in sql
    assert "memories.content = " in sql
    assert "memories.embedding_status != " in sql
    assert "memories.embedding_attempted_at IS NOT DISTINCT FROM " in sql
    params = compiled.params
    assert memory.id in params.values()
    assert LONG in params.values()  # the content this run split and embedded
    assert "pending" in params.values()
    assert claimed_at in params.values()


async def test_the_snapshot_is_taken_before_anything_is_awaited():
    """A row object refreshed mid-write must not make a stale run look current."""
    memory = _memory(LONG, embedding_attempted_at=None)
    db = _db_superseded_check(True)
    svc = _embed_svc()

    def _edit_lands(texts, *a, **k):
        memory.content = "edited " + LONG
        return [[0.1] for _ in texts]

    svc.embed_batch.side_effect = _edit_lands
    with (
        patch.object(writer, "chunk_points_supported", return_value=True),
        patch.object(writer, "delete_chunk_points", AsyncMock()),
        patch.object(writer, "upsert_chunk_points", AsyncMock()),
    ):
        await writer.write_content_chunks(
            db, memory, base_payload={}, embed_svc=svc, collection_name="c"
        )
    stmt = db.execute.await_args_list[3].args[0]
    params = stmt.compile().params
    assert LONG in params.values()
    assert "edited " + LONG not in params.values()


async def test_a_failed_superseded_check_is_fail_open():
    flag = MagicMock()
    flag.scalar_one_or_none.return_value = True
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[flag, _check(True), flag, RuntimeError("db")])
    n, upsert, delete = await _write_with(db, _memory(LONG))
    assert n == 0
    upsert.assert_not_awaited()


async def test_resource_indexer_rows_are_never_chunked():
    db = _db(True)
    svc = _embed_svc()
    with (
        patch.object(writer, "_is_resource_row", return_value=True),
        patch.object(writer, "delete_chunk_points", AsyncMock()) as delete,
    ):
        n = await writer.write_content_chunks(
            db, _memory(LONG), base_payload={}, embed_svc=svc, collection_name="c"
        )
    assert n == 0
    db.execute.assert_not_awaited()
    delete.assert_not_awaited()
    svc.embed_batch.assert_not_awaited()


# --------------------------------------------------------------------- #2049
# The embedding pipeline must not stamp a memory `success` whose chunks were
# dropped: with fail_open=False a chunk failure is logged and raised, so
# process_pending_embedding marks the row failed and the sweep retries it.


@pytest.mark.parametrize("stage", ["embed", "upsert", "flag"])
async def test_fail_closed_logs_and_raises(stage):
    import structlog

    from utils.exceptions import SelfHostedUnreachableError

    error = SelfHostedUnreachableError("Cannot connect to self-hosted inference server")
    svc = _embed_svc()
    db = _db(True)
    upsert = AsyncMock()
    if stage == "embed":
        svc.embed_batch = AsyncMock(side_effect=error)
    elif stage == "upsert":
        upsert = AsyncMock(side_effect=error)
    else:
        db.execute = AsyncMock(side_effect=error)
    with (
        structlog.testing.capture_logs() as logs,
        patch.object(writer, "chunk_points_supported", return_value=True),
        patch.object(writer, "delete_chunk_points", AsyncMock()),
        patch.object(writer, "upsert_chunk_points", upsert),
        pytest.raises(SelfHostedUnreachableError),
    ):
        await writer.write_content_chunks(
            db,
            _memory(LONG),
            base_payload={},
            embed_svc=svc,
            collection_name="c",
            fail_open=False,
        )
    event = next(e for e in logs if e["event"] == "content_chunks_failed")
    assert event["error_class"] == "SelfHostedUnreachableError"


async def test_process_pending_embedding_marks_the_row_failed_when_chunks_fail():
    """#2049: a transient chunk-embed failure ends `failed`, never `success`.

    The failure takes the normal path: the row is stamped failed (a
    configuration-class failure keeps its retry budget), so
    ``sweep_pending_embeddings`` re-embeds it and rewrites the chunks.
    """
    from sqlalchemy.dialects import postgresql

    from services import memory_service
    from tests.services.test_embedding_retry_claim import _claimed, _memory_row
    from utils.exceptions import SelfHostedUnreachableError

    memory_id = uuid4()
    failed = MagicMock()
    failed.scalar_one_or_none.return_value = 0
    statements = []
    results = iter([_claimed(memory_id), _memory_row(memory_id), failed])

    async def _execute(stmt, *a, **k):
        statements.append(stmt)
        return next(results)

    db = MagicMock()
    db.execute = AsyncMock(side_effect=_execute)
    db.commit = AsyncMock()
    db.rollback = AsyncMock()

    async def _aiter():
        yield db

    embed_svc = MagicMock()
    embed_svc.embed = AsyncMock(return_value=[0.1])
    embed_svc.model = "m"
    knn = AsyncMock()

    with (
        patch("db.base.get_db", return_value=_aiter()),
        patch("services.embedding_service.EmbeddingService", MagicMock()),
        patch(
            "services.context_routing.resolve_context_routing",
            AsyncMock(return_value=("coll", embed_svc)),
        ),
        patch.object(memory_service, "add_memory_to_qdrant", AsyncMock()),
        patch.object(memory_service, "build_memory_point", return_value=({}, [], [])),
        patch.object(
            memory_service,
            "write_content_chunks",
            AsyncMock(side_effect=SelfHostedUnreachableError("stall")),
        ) as write,
        patch.object(memory_service, "_create_knn_seed_edges", knn),
        patch.object(memory_service, "_create_tag_cooccurrence_seed_edges", AsyncMock()),
    ):
        await memory_service.process_pending_embedding(memory_id)

    assert write.await_args.kwargs["fail_open"] is False
    db.rollback.assert_awaited()
    knn.assert_not_awaited()
    last = str(
        statements[-1].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    ).upper()
    assert "'FAILED'" in last
    assert "'SUCCESS'" not in last
    # A configuration-class failure does not spend the retry budget (#1496).
    assert "EMBEDDING_RETRY_COUNT=0" in last.replace(" ", "")


async def test_fail_closed_never_raises_the_chunk_text():
    """#2049: the raised message is stored and logged by the caller — a foreign
    error that echoes its input is reduced to its class name."""
    svc = MagicMock()
    svc.embed_batch = AsyncMock(side_effect=ValueError("secret body text"))
    with (
        patch.object(writer, "chunk_points_supported", return_value=True),
        patch.object(writer, "delete_chunk_points", AsyncMock()),
        patch.object(writer, "upsert_chunk_points", AsyncMock()),
        pytest.raises(writer.ContentChunkWriteError) as err,
    ):
        await writer.write_content_chunks(
            _db(True),
            _memory(LONG),
            base_payload={},
            embed_svc=svc,
            collection_name="c",
            fail_open=False,
        )
    assert "secret body text" not in str(err.value)
    assert "ValueError" in str(err.value)
    assert err.value.__cause__ is None and err.value.__suppress_context__
