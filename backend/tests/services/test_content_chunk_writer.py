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


def _memory(content):
    return SimpleNamespace(
        id=uuid4(), user_id="u", workspace_id=WS, context_id=CTX, content=content
    )


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


async def test_short_content_costs_no_query_and_no_qdrant_call():
    db = _db(True)
    with (
        patch.object(writer, "delete_chunk_points", AsyncMock()) as delete,
        patch.object(writer, "upsert_chunk_points", AsyncMock()) as upsert,
    ):
        n = await writer.write_content_chunks(
            db, _memory("short"), base_payload={}, embed_svc=_embed_svc(), collection_name="c"
        )
    assert n == 0
    db.execute.assert_not_awaited()
    delete.assert_not_awaited()
    upsert.assert_not_awaited()


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
