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


@pytest.fixture(autouse=True)
def _trim():
    """The trim after the upsert (#2061); a test that checks it patches it again."""
    with patch.object(writer, "delete_chunk_points_from", AsyncMock()) as trim:
        yield trim


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


async def test_flag_on_replaces_chunks_in_place():
    """#2061: upsert over the old chunks first, then trim the ones past the new count."""
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
            "delete_chunk_points_from",
            AsyncMock(side_effect=lambda *a: calls.append("trim")),
        ) as trim,
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
    # Never without chunks: the old ones are overwritten, then the tail trimmed.
    assert calls == ["upsert", "trim"]
    delete.assert_not_awaited()
    trim.assert_awaited_once_with(memory.id, n, "coll")
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


async def test_chunk_cascade_needed_reads_the_flag_every_time():
    """#2061: no process-local cache — a flag turned on by another process is
    seen by the very next metadata update, so its chunks get the cascade."""
    db = _db(False)
    ctx = uuid4()
    assert await writer.chunk_cascade_needed(db, ctx) is False
    db.execute.return_value.scalar_one_or_none.return_value = True
    assert await writer.chunk_cascade_needed(db, ctx) is True
    assert db.execute.await_count == 2


async def test_chunk_cascade_needed_without_a_context_reads_nothing():
    db = _db(True)
    assert await writer.chunk_cascade_needed(db, None) is False
    db.execute.assert_not_awaited()


async def test_chunk_cascade_needed_fails_safe():
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


@pytest.mark.parametrize("content", [LONG, "now short"])
async def test_a_failed_trim_or_delete_is_raised_when_fail_closed(content):
    """#2015: an edit's old body must not stay behind unnoticed — with
    ``fail_open=False`` the row takes the caller's failed/retry path."""
    from utils.exceptions import QdrantError

    error = QdrantError("q")
    with (
        patch.object(writer, "chunk_points_supported", return_value=True),
        patch.object(writer, "delete_chunk_points", AsyncMock(side_effect=error)),
        patch.object(writer, "delete_chunk_points_from", AsyncMock(side_effect=error)),
        patch.object(writer, "upsert_chunk_points", AsyncMock()),
    ):
        assert (
            await writer.write_content_chunks(
                _db(True),
                _memory(content),
                base_payload={},
                embed_svc=_embed_svc(),
                collection_name="c",
            )
            == 0
        )
        with pytest.raises(QdrantError):
            await writer.write_content_chunks(
                _db(True),
                _memory(content),
                base_payload={},
                embed_svc=_embed_svc(),
                collection_name="c",
                fail_open=False,
            )


@pytest.mark.parametrize("fail_open", [True, False])
async def test_a_failed_embedding_keeps_the_old_chunks(fail_open):
    """#2061: nothing is deleted before the new chunks exist, so a provider
    failure leaves the memory with its old chunks, not summary-only."""
    svc = MagicMock()
    svc.embed_batch = AsyncMock(side_effect=RuntimeError("429"))
    with (
        patch.object(writer, "chunk_points_supported", return_value=True),
        patch.object(writer, "delete_chunk_points", AsyncMock()) as delete,
        patch.object(writer, "delete_chunk_points_from", AsyncMock()) as trim,
        patch.object(writer, "upsert_chunk_points", AsyncMock()) as upsert,
    ):
        try:
            await writer.write_content_chunks(
                _db(True),
                _memory(LONG),
                base_payload={},
                embed_svc=svc,
                collection_name="c",
                fail_open=fail_open,
            )
        except writer.ContentChunkWriteError:
            assert not fail_open
    delete.assert_not_awaited()
    trim.assert_not_awaited()
    upsert.assert_not_awaited()


async def test_a_failed_upsert_trims_nothing():
    """The old tail stays until a retry writes the new chunks in front of it."""
    with (
        patch.object(writer, "chunk_points_supported", return_value=True),
        patch.object(writer, "delete_chunk_points_from", AsyncMock()) as trim,
        patch.object(writer, "upsert_chunk_points", AsyncMock(side_effect=RuntimeError("q"))),
    ):
        n = await writer.write_content_chunks(
            _db(True), _memory(LONG), base_payload={}, embed_svc=_embed_svc(), collection_name="c"
        )
    assert n == 0
    trim.assert_not_awaited()


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


def _db_superseded_check(
    still_current: bool, *, current_at_entry: bool = True, current_after_write: bool = True
):
    """Flag lookup, check before the write, flag re-read, check before the
    upsert and — without a claim — the check after the write (#2061)."""
    flag = MagicMock()
    flag.scalar_one_or_none.return_value = True
    db = MagicMock()
    db.execute = AsyncMock(
        side_effect=[
            flag,
            _check(current_at_entry),
            flag,
            _check(still_current),
            _check(current_after_write),
        ]
    )
    return db


async def _write_with(db, memory, svc=None):
    """Returns ``(n, upsert, trim)``."""
    with (
        patch.object(writer, "chunk_points_supported", return_value=True),
        patch.object(writer, "delete_chunk_points", AsyncMock()) as delete,
        patch.object(writer, "delete_chunk_points_from", AsyncMock()) as trim,
        patch.object(writer, "upsert_chunk_points", AsyncMock()) as upsert,
    ):
        n = await writer.write_content_chunks(
            db, memory, base_payload={}, embed_svc=svc or _embed_svc(), collection_name="c"
        )
    delete.assert_not_awaited()  # long content: replaced in place, never deleted
    return n, upsert, trim


async def test_a_superseded_run_writes_no_chunks():
    import structlog

    memory = _memory(LONG)
    with structlog.testing.capture_logs() as logs:
        n, upsert, trim = await _write_with(_db_superseded_check(False), memory)
    assert n == 0
    trim.assert_not_awaited()  # the newer run's chunks are left as it wrote them
    upsert.assert_not_awaited()
    event = next(e for e in logs if e["event"] == "content_chunks_skipped_superseded")
    assert event["memory_id"] == str(memory.id)
    assert event["stage"] == "before_upsert"
    assert event["log_level"] == "info"


async def test_a_run_superseded_before_it_starts_deletes_nothing():
    """A stale run that reaches the writer after a newer run finished must not
    touch the newer run's chunks (and pays for no embedding)."""
    import structlog

    svc = _embed_svc()
    db = _db_superseded_check(True, current_at_entry=False)
    with structlog.testing.capture_logs() as logs:
        n, upsert, trim = await _write_with(db, _memory(LONG), svc)
    assert n == 0
    trim.assert_not_awaited()
    svc.embed_batch.assert_not_awaited()
    upsert.assert_not_awaited()
    event = next(e for e in logs if e["event"] == "content_chunks_skipped_superseded")
    assert event["stage"] == "before_write"


async def test_a_failed_check_before_the_write_does_not_stop_the_run():
    flag = MagicMock()
    flag.scalar_one_or_none.return_value = True
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[flag, RuntimeError("db"), flag, _check(True), _check(True)])
    n, upsert, trim = await _write_with(db, _memory(LONG))
    assert n == len(split_content(LONG))
    upsert.assert_awaited_once()
    trim.assert_awaited_once()


async def test_a_current_run_writes_its_chunks():
    n, upsert, _ = await _write_with(_db_superseded_check(True), _memory(LONG))
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
    # flag lookup, check, embed, flag re-read, check, upsert, (trim), check
    assert order == ["execute", "execute", "embed", "execute", "execute", "upsert", "execute"]


async def test_the_superseded_check_compares_content_status_and_claim():
    """One indexed lookup by id: live, same content, not re-queued, same claim."""
    from datetime import datetime

    from sqlalchemy.dialects import postgresql

    from services.embedding_claim import EmbeddingClaim

    claimed_at = datetime(2026, 1, 2, 3, 4, 5, 678901)
    memory = _memory(LONG, embedding_attempted_at=None)
    db = _db_superseded_check(True)
    with (
        patch.object(writer, "chunk_points_supported", return_value=True),
        patch.object(writer, "delete_chunk_points", AsyncMock()),
        patch.object(writer, "upsert_chunk_points", AsyncMock()),
    ):
        await writer.write_content_chunks(
            db,
            memory,
            base_payload={},
            embed_svc=_embed_svc(),
            collection_name="c",
            # The run's own claim, not the stamp loaded on the row.
            claim=EmbeddingClaim(db, memory.id, claimed_at),
        )

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


async def test_a_caller_without_a_claim_compares_no_stamp():
    """The Sleep paths hold no claim: a live run renewing its stamp must not
    read to them as a newer claim (#2041), so only the content etc. is checked."""
    from datetime import datetime

    memory = _memory(LONG, embedding_attempted_at=datetime(2026, 1, 2, 3, 4, 5))
    db = _db_superseded_check(True)
    n, upsert, _ = await _write_with(db, memory)
    assert n == len(split_content(LONG))
    upsert.assert_awaited_once()
    calls = db.execute.await_args_list
    assert len(calls) == 5
    # Before the write, before the upsert, after the write.
    for call in (calls[1], calls[3], calls[4]):
        sql = str(call.args[0])
        assert "memories.content = " in sql
        assert "embedding_attempted_at" not in sql


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
    n, upsert, _ = await _write_with(db, _memory(LONG))
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


# --- #2015 PR 2b: usage, content_hash, metadata-only payload sync ----------


def _usage_embed_svc(tokens=30, cached=1):
    async def embed_batch(texts, *args, usage=None, **kwargs):
        if usage is not None:
            usage.update(cached=cached, requests=1, tokens=tokens)
        return [[0.1] for _ in texts]

    svc = MagicMock()
    svc.embed_batch = AsyncMock(side_effect=embed_batch)
    return svc


async def test_usage_and_content_hash_reach_the_caller_and_the_points():
    import structlog

    from utils.content_chunks import content_hash

    memory = _memory(LONG)
    usage: dict[str, int] = {}
    with (
        structlog.testing.capture_logs() as logs,
        patch.object(writer, "chunk_points_supported", return_value=True),
        patch.object(writer, "delete_chunk_points", AsyncMock()),
        patch.object(writer, "upsert_chunk_points", AsyncMock()) as upsert,
    ):
        n = await writer.write_content_chunks(
            _db(True),
            memory,
            base_payload={"summary": "s"},
            embed_svc=_usage_embed_svc(),
            collection_name="c",
            usage=usage,
        )
    assert n == len(split_content(LONG))
    assert usage == {"cached": 1, "requests": 1, "tokens": 30}
    points = upsert.await_args.args[0]
    assert {p[2]["content_hash"] for p in points} == {content_hash(LONG)}
    event = next(e for e in logs if e["event"] == "content_chunks_written")
    assert (event["tokens"], event["cached"]) == (30, 1)


async def test_usage_is_kept_when_the_upsert_fails():
    """The provider billed the chunks even though they were not stored."""
    usage: dict[str, int] = {}
    with (
        patch.object(writer, "chunk_points_supported", return_value=True),
        patch.object(writer, "delete_chunk_points", AsyncMock()),
        patch.object(writer, "upsert_chunk_points", AsyncMock(side_effect=RuntimeError("x"))),
    ):
        n = await writer.write_content_chunks(
            _db(True),
            _memory(LONG),
            base_payload={},
            embed_svc=_usage_embed_svc(tokens=12),
            collection_name="c",
            usage=usage,
        )
    assert n == 0
    assert usage["tokens"] == 12


async def test_sync_copies_the_payload_without_embedding():
    memory = _memory(LONG)
    base = {"summary": "s", "importance": 0.9, "scope": "persistent", "summary_tokens": "t"}
    with (
        patch.object(writer, "chunk_points_supported", return_value=True),
        patch.object(writer, "sync_chunk_points_payload", AsyncMock(return_value=True)) as sync,
    ):
        ok = await writer.sync_content_chunk_payload(
            _db(True), memory, base_payload=base, collection_name="c"
        )
    assert ok is True
    # The BM25 token fields are not copied (chunk points have no sparse vector),
    # and the optional fields the memory does not have are removed.
    sync.assert_awaited_once_with(
        memory.id,
        {"summary": "s", "importance": 0.9, "scope": "persistent"},
        "c",
        delete_keys=["context", "location"],
    )


async def test_sync_keeps_the_optional_fields_the_memory_has():
    base = {"summary": "s", "context": {"k": 1}, "location": {"lat": 1.0, "lon": 2.0}}
    with (
        patch.object(writer, "chunk_points_supported", return_value=True),
        patch.object(writer, "sync_chunk_points_payload", AsyncMock(return_value=True)) as sync,
    ):
        await writer.sync_content_chunk_payload(
            _db(True), _memory(LONG), base_payload=base, collection_name="c"
        )
    assert sync.await_args.args[1] == base
    assert sync.await_args.kwargs["delete_keys"] is None


@pytest.mark.parametrize(
    ("flag", "supported", "resource"),
    [(False, True, False), (True, False, False), (True, True, True)],
)
async def test_sync_is_a_no_op_without_chunk_points(flag, supported, resource):
    extra = {"resource_id": "r"} if resource else {}
    memory = _memory(LONG, **extra)
    with (
        patch.object(writer, "_is_resource_row", return_value=resource),
        patch.object(writer, "chunk_points_supported", return_value=supported),
        patch.object(writer, "sync_chunk_points_payload", AsyncMock()) as sync,
    ):
        ok = await writer.sync_content_chunk_payload(
            _db(flag), memory, base_payload={"summary": "s"}, collection_name="c"
        )
    assert ok is True
    sync.assert_not_awaited()


@pytest.mark.parametrize("outcome", [False, RuntimeError("qdrant down")])
async def test_sync_reports_chunks_it_could_not_update(outcome):
    """Deleted instead of updated, or not even deleted: the caller rewrites them."""
    side_effect = outcome if isinstance(outcome, Exception) else None
    with (
        patch.object(writer, "chunk_points_supported", return_value=True),
        patch.object(
            writer,
            "sync_chunk_points_payload",
            AsyncMock(return_value=outcome, side_effect=side_effect),
        ),
    ):
        ok = await writer.sync_content_chunk_payload(
            _db(True), _memory(LONG), base_payload={"summary": "s"}, collection_name="c"
        )
    assert ok is False


# --------------------------------------------------------------------- #2061
# A Sleep chunk write that failed re-queues the memory for the embedding sweep.


def _requeue_db(rowcount):
    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock(rowcount=rowcount))
    return db


async def test_requeue_embedding_is_fenced_on_a_live_success_row():
    """Only ``embedding_status`` moves; the edit cue, the claim stamp and the
    last error stay, and a pending / processing / failed / deleted row is left."""
    import structlog
    from sqlalchemy.dialects import postgresql

    memory_id = uuid4()
    db = _requeue_db(1)
    with structlog.testing.capture_logs() as logs:
        assert await writer.requeue_embedding(db, memory_id, reason="chunk_write_failed") is True

    db.begin_nested.assert_called_once()  # a failed UPDATE leaves the caller's txn usable
    stmt = db.execute.await_args.args[0]
    sql = str(
        stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    ).replace("memories.", "")
    set_clause, where = sql.split(" WHERE ", 1)
    assert set_clause.endswith("SET embedding_status='pending'")
    for column in ("updated_at", "embedding_attempted_at", "embedding_error"):
        assert column not in set_clause
    assert f"id = '{memory_id}'" in where.replace("::UUID", "")
    assert "deleted_at IS NULL" in where
    assert "embedding_status = 'success'" in where
    assert stmt.get_execution_options()["synchronize_session"] is False
    event = next(e for e in logs if e["event"] == "embedding_requeued")
    assert event["memory_id"] == str(memory_id)
    assert event["reason"] == "chunk_write_failed"


def _assert_requeue_over_run(stmt) -> None:
    """#2095: the re-queue over a run in flight moves ``success`` or
    ``processing``, never ``pending`` / ``failed`` / a deleted row."""
    from sqlalchemy.dialects import postgresql

    sql = str(
        stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    ).replace("memories.", "")
    set_clause, where = sql.split(" WHERE ", 1)
    assert set_clause.endswith("SET embedding_status='pending'")
    assert "embedding_status IN ('success', 'processing')" in where
    assert "deleted_at IS NULL" in where


async def test_requeue_embedding_over_a_run_moves_processing_too():
    db = _requeue_db(1)
    assert await writer.requeue_embedding(db, uuid4(), reason="r", include_processing=True) is True
    _assert_requeue_over_run(db.execute.await_args.args[0])


def _stale_over_run_db(requeue_rowcounts):
    """A Sleep write (no claim) of LONG whose row moved on after the write."""
    db = _db_superseded_check(True, current_after_write=False)
    db.execute.side_effect = [
        *db.execute.side_effect,
        *(MagicMock(rowcount=n) for n in requeue_rowcounts),
    ]
    return db


@pytest.mark.parametrize(
    ("stored", "over_run"),
    [("own", True), ("other", False), (None, False), (RuntimeError("qdrant"), False)],
)
async def test_a_sleep_write_over_a_run_in_flight_is_re_queued_only_when_visible(stored, over_run):
    """#2095: the success-only re-queue moved nothing, so the row is ``processing``
    (or ``pending``). When chunk 0 still carries the hash of the text this write
    put there, the run in flight upserted first and this write replaced its
    chunks: re-queue over it. Another hash, none, or a failed read: the newer
    write stands."""
    import structlog

    memory = _memory(LONG)
    db = _stale_over_run_db([0, 1])
    digest = writer.content_hash(LONG)
    if stored == "own":
        read = AsyncMock(return_value=digest)
    elif stored == "other":
        read = AsyncMock(return_value="newer-hash")
    elif stored is None:
        read = AsyncMock(return_value=None)
    else:
        read = AsyncMock(side_effect=stored)
    with (
        structlog.testing.capture_logs() as logs,
        patch.object(writer, "chunk_points_content_hash", read),
    ):
        n, upsert, _trim = await _write_with(db, memory)

    assert n == 0
    upsert.assert_awaited_once()
    read.assert_awaited_once_with(memory.id, "c")
    executes = db.execute.await_args_list
    assert len(executes) == (7 if over_run else 6)
    if over_run:
        _assert_requeue_over_run(executes[-1].args[0])
        reasons = [e["reason"] for e in logs if e["event"] == "embedding_requeued"]
        assert reasons == ["stale_chunks_over_run"]
    event = next(e for e in logs if e["event"] == "content_chunks_stale_after_write")
    assert event["requeued"] is over_run
    assert event["over_run"] is over_run
    if isinstance(stored, Exception):
        failed = next(e for e in logs if e["event"] == "content_chunks_superseded_check_failed")
        assert failed["stage"] == "over_run"


async def test_a_sleep_write_whose_success_re_queue_moved_the_row_reads_no_hash():
    memory = _memory(LONG)
    db = _stale_over_run_db([1])
    with patch.object(writer, "chunk_points_content_hash", AsyncMock()) as read:
        n, _upsert, _trim = await _write_with(db, memory)
    assert n == 0
    read.assert_not_awaited()
    assert db.execute.await_count == 6


async def test_requeue_embedding_of_a_row_not_success_is_a_quiet_no_op():
    import structlog

    with structlog.testing.capture_logs() as logs:
        assert await writer.requeue_embedding(_requeue_db(0), uuid4(), reason="r") is False
    assert not [e for e in logs if e["event"] == "embedding_requeued"]


async def test_requeue_embedding_raises_a_failed_update():
    db = MagicMock()
    db.execute = AsyncMock(side_effect=RuntimeError("db"))
    with pytest.raises(RuntimeError):
        await writer.requeue_embedding(db, uuid4(), reason="r")


async def test_a_sleep_write_overtaken_by_a_newer_write_is_re_queued():
    """#2061: a run without a claim reads the row once more after its write; if
    an edit landed meanwhile its chunks may have replaced the current ones, so
    the row goes back to ``pending`` (fenced on ``success``) and 0 is returned."""
    import structlog
    from sqlalchemy.dialects import postgresql

    memory = _memory(LONG)
    db = _db_superseded_check(True, current_after_write=False)
    db.execute.side_effect = [*db.execute.side_effect, MagicMock(rowcount=1)]
    with structlog.testing.capture_logs() as logs:
        n, upsert, trim = await _write_with(db, memory)

    assert n == 0
    upsert.assert_awaited_once()
    trim.assert_awaited_once()
    requeue = db.execute.await_args_list[-1].args[0]
    sql = str(requeue.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "SET embedding_status='pending'" in sql
    assert "embedding_status = 'success'" in sql
    event = next(e for e in logs if e["event"] == "content_chunks_stale_after_write")
    assert event["requeued"] is True
    assert next(e for e in logs if e["event"] == "embedding_requeued")["reason"] == (
        "stale_after_write"
    )
    assert not [e for e in logs if e["event"] == "content_chunks_written"]


async def test_a_sleep_delete_of_shrunk_content_is_checked_after_the_write():
    import structlog

    memory = _memory("now short")
    flag = MagicMock()
    flag.scalar_one_or_none.return_value = True
    db = MagicMock()
    db.execute = AsyncMock(
        side_effect=[
            flag,
            _check(True),
            _check(False),
            MagicMock(rowcount=0),
            MagicMock(rowcount=1),
        ]
    )
    with (
        structlog.testing.capture_logs() as logs,
        patch.object(writer, "chunk_points_supported", return_value=True),
        patch.object(writer, "delete_chunk_points", AsyncMock()) as delete,
        patch.object(writer, "chunk_points_content_hash", AsyncMock()) as read_hash,
    ):
        n = await writer.write_content_chunks(
            db, memory, base_payload={}, embed_svc=_embed_svc(), collection_name="c"
        )
    assert n == 0
    delete.assert_awaited_once()
    # flag, check, check after the write, the success-only re-queue, and —
    # #2095: it moved nothing, and the delete may have removed a run's new
    # chunks; nothing to compare, so the re-queue over the run, no read.
    assert db.execute.await_count == 5
    read_hash.assert_not_awaited()
    _assert_requeue_over_run(db.execute.await_args_list[-1].args[0])
    event = next(e for e in logs if e["event"] == "content_chunks_stale_after_write")
    assert event["requeued"] is True and event["over_run"] is True


async def test_an_embedding_run_does_not_check_after_its_write():
    """A claim holder's lost claim is caught by its fenced outcome write instead."""
    from datetime import datetime

    from services.embedding_claim import EmbeddingClaim

    memory = _memory(LONG)
    db = _db_superseded_check(True, current_after_write=False)
    with (
        patch.object(writer, "chunk_points_supported", return_value=True),
        patch.object(writer, "upsert_chunk_points", AsyncMock()),
        patch.object(writer, "chunk_points_content_hash", AsyncMock()) as read_hash,
    ):
        n = await writer.write_content_chunks(
            db,
            memory,
            base_payload={},
            embed_svc=_embed_svc(),
            collection_name="c",
            claim=EmbeddingClaim(db, memory.id, datetime(2026, 1, 2)),
        )
    assert n == len(split_content(LONG))
    assert db.execute.await_count == 4
    read_hash.assert_not_awaited()  # #2095: no extra read on the embedding path


async def test_a_failed_check_after_the_write_keeps_the_write():
    """The chunks were written; a failed read after them is logged, not a failure
    (that would re-queue a memory whose chunks are fine)."""
    import structlog

    flag = MagicMock()
    flag.scalar_one_or_none.return_value = True
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[flag, _check(True), flag, _check(True), RuntimeError("db")])
    with (
        structlog.testing.capture_logs() as logs,
        patch.object(writer, "chunk_points_supported", return_value=True),
        patch.object(writer, "upsert_chunk_points", AsyncMock()),
    ):
        n = await writer.write_content_chunks(
            db,
            _memory(LONG),
            base_payload={},
            embed_svc=_embed_svc(),
            collection_name="c",
            fail_open=False,
        )
    assert n == len(split_content(LONG))
    event = next(e for e in logs if e["event"] == "content_chunks_superseded_check_failed")
    assert event["stage"] == "after_write"


@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_a_failed_write_of_an_embedding_run_removes_the_old_chunks(cleanup_fails):
    """#2061: an embedding run follows an edit, so its old chunks may hold body
    text the memory no longer has; they go while the row waits for its retry."""
    import structlog

    from services.embedding_claim import EmbeddingClaim
    from utils.exceptions import SelfHostedUnreachableError

    memory = _memory(LONG)
    db = _db(True)
    # Flag on and the claim still this run's: no row with another stamp.
    still_ours = MagicMock()
    still_ours.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(side_effect=[db.execute.return_value] * 2 + [still_ours])
    svc = MagicMock()
    svc.embed_batch = AsyncMock(side_effect=SelfHostedUnreachableError("down"))
    delete = AsyncMock(side_effect=RuntimeError("q") if cleanup_fails else None)
    with (
        structlog.testing.capture_logs() as logs,
        patch.object(writer, "chunk_points_supported", return_value=True),
        patch.object(writer, "delete_chunk_points", delete),
        pytest.raises(SelfHostedUnreachableError),
    ):
        await writer.write_content_chunks(
            db,
            memory,
            base_payload={},
            embed_svc=svc,
            collection_name="c",
            fail_open=False,
            claim=EmbeddingClaim(db, memory.id, None),
        )
    delete.assert_awaited_once_with([memory.id], "c")
    cleanup = [e for e in logs if e["event"] == "content_chunks_cleanup_failed"]
    assert len(cleanup) == (1 if cleanup_fails else 0)


@pytest.mark.parametrize("check_fails", [False, True])
async def test_a_reclaimed_run_keeps_the_newer_runs_chunks_on_failure(check_fails):
    """#2061: a run whose claim a newer run took writes nothing and must not
    delete what that run wrote either: the drop after a failure is fenced on
    the claim. A failed check keeps the drop (stale body text must go)."""
    import structlog

    from services.embedding_claim import EmbeddingClaim
    from utils.exceptions import SelfHostedUnreachableError

    memory = _memory(LONG)
    current = MagicMock()
    current.scalar_one_or_none.return_value = memory.id
    db = MagicMock()
    # The check before the write sees this run's claim; by the failure a newer
    # run has claimed (and written) the row: its stamp is not this run's.
    db.execute = AsyncMock(side_effect=[current, RuntimeError("db") if check_fails else current])
    svc = MagicMock()
    svc.embed_batch = AsyncMock(side_effect=SelfHostedUnreachableError("down"))
    delete = AsyncMock()
    with (
        structlog.testing.capture_logs() as logs,
        patch.object(writer, "chunk_points_supported", return_value=True),
        patch.object(writer, "delete_chunk_points", delete),
        pytest.raises(SelfHostedUnreachableError),
    ):
        await writer.write_content_chunks(
            db,
            memory,
            base_payload={},
            embed_svc=svc,
            collection_name="c",
            chunks_enabled=True,
            fail_open=False,
            claim=EmbeddingClaim(db, memory.id, None),
        )
    events = [e["event"] for e in logs]
    if check_fails:
        delete.assert_awaited_once_with([memory.id], "c")
        assert "content_chunks_superseded_check_failed" in events
    else:
        delete.assert_not_awaited()
        skipped = next(e for e in logs if e["event"] == "content_chunks_cleanup_skipped_superseded")
        assert skipped["memory_id"] == str(memory.id)


async def test_the_cleanup_fence_compares_only_the_claim():
    """An edit, a re-queue or a soft delete since the run started wrote no
    chunks: only a different claim stamp keeps them (#2061)."""
    from datetime import datetime

    from services.embedding_claim import EmbeddingClaim

    memory = _memory(LONG)
    claimed_at = datetime(2026, 1, 2, 3, 4, 5)
    db = _db(None)
    await writer._claim_taken(db, memory.id, EmbeddingClaim(db, memory.id, claimed_at))
    stmt = db.execute.await_args.args[0]
    sql = str(stmt.compile(compile_kwargs={"literal_binds": False}))
    assert "embedding_attempted_at IS DISTINCT FROM" in sql
    for column in ("content", "deleted_at", "embedding_status"):
        assert f"memories.{column}" not in sql.split("WHERE", 1)[1]


# ---------------------------------------------------------------------------
# #2103: a context whose chunks are inactive (plan, cap) keeps its points
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _kept_hash():
    """The hash on the memory's chunk point 0: none of the current content."""
    with patch.object(writer, "chunk_points_content_hash", AsyncMock(return_value="old")) as h:
        yield h


@pytest.fixture
def inactive():
    """CONTENT_CHUNKS_REQUIRE_PLAN on and the context past its plan's cap."""
    from services.content_chunk_policy import ContentChunksStatus

    status = ContentChunksStatus(
        stored=True, supported=True, gated=True, plan_has_feature=True, limit=1, earlier=1
    )
    with patch.object(writer, "content_chunks_status", AsyncMock(return_value=status)) as st:
        yield st


async def test_an_inactive_context_writes_no_chunks_and_drops_the_memorys_own(inactive):
    memory = _memory(LONG)
    svc = _embed_svc()
    with (
        patch.object(writer, "chunk_points_supported", return_value=True),
        patch.object(writer, "delete_chunk_points", AsyncMock()) as delete,
        patch.object(writer, "upsert_chunk_points", AsyncMock()) as upsert,
    ):
        n = await writer.write_content_chunks(
            _db(True), memory, base_payload={}, embed_svc=svc, collection_name="c"
        )
    assert n == 0
    # Its points would hold the old content when the context is active again.
    delete.assert_awaited_once_with([memory.id], "c")
    svc.embed_batch.assert_not_awaited()
    upsert.assert_not_awaited()


async def test_a_preloaded_inactive_flag_drops_only_what_is_stored():
    memory = _memory(LONG)
    with (
        patch.object(writer, "chunk_points_supported", return_value=True),
        patch.object(writer, "delete_chunk_points", AsyncMock()) as delete,
    ):
        for stored in (True, False, None):
            await writer.write_content_chunks(
                MagicMock(),
                memory,
                base_payload={},
                embed_svc=_embed_svc(),
                collection_name="c",
                chunks_enabled=False,
                chunks_stored=stored,
            )
    # Only the stored-on call deletes; stored off / unknown has no points.
    assert delete.await_count == 1


async def test_a_failed_drop_is_logged_not_raised(inactive):
    import structlog

    with (
        structlog.testing.capture_logs() as logs,
        patch.object(writer, "chunk_points_supported", return_value=True),
        patch.object(writer, "delete_chunk_points", AsyncMock(side_effect=RuntimeError("x"))),
    ):
        n = await writer.write_content_chunks(
            _db(True),
            _memory(LONG),
            base_payload={},
            embed_svc=_embed_svc(),
            collection_name="c",
            fail_open=False,
        )
    assert n == 0
    assert any(e["event"] == "content_chunks_failed" for e in logs)


async def test_a_context_gone_inactive_while_embedding_writes_nothing():
    """Active at the first read, inactive at the re-read before the upsert."""
    from services.content_chunk_policy import ContentChunksStatus

    def _status(effective):
        return ContentChunksStatus(
            stored=True, supported=True, gated=True, limit=1, earlier=0 if effective else 1
        )

    memory = _memory(LONG)
    with (
        patch.object(
            writer,
            "content_chunks_status",
            AsyncMock(side_effect=[_status(True), _status(False)]),
        ),
        patch.object(writer, "content_chunks_enabled", AsyncMock(return_value=True)),
        patch.object(writer, "chunk_points_supported", return_value=True),
        patch.object(writer, "delete_chunk_points", AsyncMock()) as delete,
        patch.object(writer, "upsert_chunk_points", AsyncMock()) as upsert,
        patch.object(writer, "_superseded_before_write", AsyncMock(return_value=False)),
        patch.object(writer, "_check_superseded", AsyncMock(return_value=False)),
    ):
        n = await writer.write_content_chunks(
            MagicMock(), memory, base_payload={}, embed_svc=_embed_svc(), collection_name="c"
        )
    assert n == 0
    upsert.assert_not_awaited()
    delete.assert_awaited_once_with([memory.id], "c")


async def test_an_inactive_contexts_points_still_get_the_new_payload(inactive):
    """Payload syncs follow the STORED flag, so kept points stay current."""
    with (
        patch.object(writer, "chunk_points_supported", return_value=True),
        patch.object(writer, "sync_chunk_points_payload", AsyncMock(return_value=True)) as sync,
    ):
        ok = await writer.sync_content_chunk_payload(
            _db(True), _memory(LONG), base_payload={"summary": "s"}, collection_name="c"
        )
    assert ok is True
    sync.assert_awaited_once()
    inactive.assert_not_awaited()


async def test_the_cascade_follows_the_stored_flag(inactive):
    assert await writer.chunk_cascade_needed(_db(True), CTX) is True
    inactive.assert_not_awaited()


async def test_an_inactive_context_keeps_the_chunks_of_unchanged_content(inactive, _kept_hash):
    """A re-embed without a content change (re-queue, migration switch,
    restore) must not lose points that are still right."""
    from utils.content_chunks import content_hash

    memory = _memory(LONG)
    _kept_hash.return_value = content_hash(LONG)
    with (
        patch.object(writer, "chunk_points_supported", return_value=True),
        patch.object(writer, "delete_chunk_points", AsyncMock()) as delete,
    ):
        n = await writer.write_content_chunks(
            _db(True), memory, base_payload={}, embed_svc=_embed_svc(), collection_name="c"
        )
    assert n == 0
    _kept_hash.assert_awaited_once_with(memory.id, "c")
    delete.assert_not_awaited()


async def test_an_inactive_context_drops_chunks_of_content_now_too_short(inactive, _kept_hash):
    memory = _memory("short")
    with (
        patch.object(writer, "chunk_points_supported", return_value=True),
        patch.object(writer, "delete_chunk_points", AsyncMock()) as delete,
    ):
        await writer.write_content_chunks(
            _db(True), memory, base_payload={}, embed_svc=_embed_svc(), collection_name="c"
        )
    _kept_hash.assert_not_awaited()
    delete.assert_awaited_once_with([memory.id], "c")


# ---------------------------------------------------------------------------
# #2107: a long memory left without current chunks stamps a gap
# ---------------------------------------------------------------------------


@pytest.fixture
def gap():
    with patch.object(writer, "record_content_chunk_gap", AsyncMock(return_value=True)) as g:
        yield g


async def _write_inactive(memory, db=None):
    with (
        patch.object(writer, "chunk_points_supported", return_value=True),
        patch.object(writer, "delete_chunk_points", AsyncMock()),
    ):
        return await writer.write_content_chunks(
            db or _db(True), memory, base_payload={}, embed_svc=_embed_svc(), collection_name="c"
        )


async def test_an_inactive_write_of_a_long_memory_stamps_a_gap(inactive, gap):
    db = _db(True)
    memory = _memory(LONG)
    await _write_inactive(memory, db)
    gap.assert_awaited_once_with(db, memory.context_id, reason="inactive_write")


async def test_kept_chunks_of_unchanged_content_stamp_no_gap(inactive, gap, _kept_hash):
    from utils.content_chunks import content_hash

    _kept_hash.return_value = content_hash(LONG)
    await _write_inactive(_memory(LONG))
    gap.assert_not_awaited()


async def test_a_short_memory_stamps_no_gap(inactive, gap):
    await _write_inactive(_memory("short"))
    gap.assert_not_awaited()


async def test_a_failed_hash_read_still_stamps_the_gap(inactive, gap, _kept_hash):
    _kept_hash.side_effect = RuntimeError("qdrant down")
    await _write_inactive(_memory(LONG))
    gap.assert_awaited_once()


async def test_an_active_write_stamps_no_gap(gap):
    memory = _memory(LONG)
    with (
        patch.object(writer, "chunk_points_supported", return_value=True),
        patch.object(writer, "upsert_chunk_points", AsyncMock()),
        patch.object(writer, "_superseded_before_write", AsyncMock(return_value=False)),
        patch.object(writer, "_check_superseded", AsyncMock(return_value=False)),
        patch.object(writer, "_chunk_flags", AsyncMock(return_value=(True, True))),
    ):
        n = await writer.write_content_chunks(
            MagicMock(), memory, base_payload={}, embed_svc=_embed_svc(), collection_name="c"
        )
    assert n > 0
    gap.assert_not_awaited()
