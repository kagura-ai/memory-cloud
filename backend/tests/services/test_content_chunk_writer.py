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
