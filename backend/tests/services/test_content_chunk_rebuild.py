"""#2015 PR 2a: rebuilding a page's content chunk points in a migration target."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services import content_chunk_writer as writer
from utils.content_chunks import CHUNK_OF_FIELD, chunk_point_id, split_content

pytestmark = pytest.mark.asyncio

WS = "11111111-1111-4111-8111-111111111111"
CTX = "22222222-2222-4222-8222-222222222222"
TARGET = "kagura_memories_qwen3_embedding_4b_2560"
LONG = "y" * 5000  # 5 chunks


def _memory(content, user_id="u", **extra):
    fields = {
        "id": uuid4(),
        "user_id": user_id,
        "workspace_id": WS,
        "context_id": CTX,
        "content": content,
        "resource_id": None,
    }
    fields.update(extra)
    return SimpleNamespace(**fields)


def _db():
    db = MagicMock()
    db.commit = AsyncMock()
    return db


def _embed_svc(order=None):
    svc = MagicMock()

    async def _embed(texts, *a, **k):
        if order is not None:
            order.append("embed")
        return [[float(len(t))] for t in texts]

    svc.embed_batch = AsyncMock(side_effect=_embed)
    return svc


async def _rebuild(rows, *, flags=(True, True), embed_svc=None, db=None):
    flag = AsyncMock(side_effect=list(flags))
    with (
        patch.object(writer, "chunk_points_supported", return_value=True),
        patch.object(writer, "content_chunks_enabled", flag),
        patch.object(writer, "delete_chunk_points", AsyncMock()) as delete,
        patch.object(writer, "upsert_chunk_points", AsyncMock()) as upsert,
    ):
        n = await writer.rebuild_content_chunks(
            db or _db(),
            rows,
            base_payloads={m.id: {"summary": "s", "summary_tokens": "t"} for m in rows},
            embed_svc=embed_svc or _embed_svc(),
            collection_name=TARGET,
            context_id=CTX,
        )
    return n, delete, upsert, flag


async def test_writes_every_long_memorys_chunks_into_the_target_collection():
    long_a, long_b, short = _memory(LONG), _memory("z" * 2500), _memory("short")
    svc = _embed_svc()
    n, delete, upsert, _ = await _rebuild([long_a, long_b, short], embed_svc=svc)

    # Delete first, for every plain row of the page (short content loses
    # chunks an earlier migration left), in the target.
    delete.assert_awaited_once_with([long_a.id, long_b.id, short.id], TARGET)
    # One request for the whole page's chunks (same user, under the cap).
    svc.embed_batch.assert_awaited_once()
    assert svc.embed_batch.await_args.args[0] == split_content(LONG) + split_content("z" * 2500)
    # One upsert for the user's chunks, ids deterministic, payload without
    # BM25 tokens, each vector on its own chunk.
    upsert.assert_awaited_once()
    call = upsert.await_args
    assert call.kwargs["collection_name"] == TARGET
    assert call.kwargs["context_id"] == CTX
    points = call.args[0]
    assert [p[0] for p in points] == [chunk_point_id(long_a.id, i) for i in range(5)] + [
        chunk_point_id(long_b.id, i) for i in range(3)
    ]
    assert [p[2][CHUNK_OF_FIELD] for p in points] == [str(long_a.id)] * 5 + [str(long_b.id)] * 3
    assert all("summary_tokens" not in p[2] for p in points)
    texts = split_content(LONG) + split_content("z" * 2500)
    assert [p[1] for p in points] == [[float(len(t))] for t in texts]
    assert n == 5 + 3


async def test_embeds_in_runs_of_one_user_capped_per_request(monkeypatch):
    monkeypatch.setattr(writer, "CHUNK_EMBED_BATCH_SIZE", 4)
    a1, a2, b1 = _memory(LONG, "a"), _memory(LONG, "a"), _memory(LONG, "b")
    svc = _embed_svc()
    n, _, upsert, _ = await _rebuild([a1, a2, b1], embed_svc=svc)
    calls = svc.embed_batch.await_args_list
    assert [(c.args[1], len(c.args[0])) for c in calls] == [
        ("a", 4),
        ("a", 4),
        ("a", 2),
        ("b", 4),
        ("b", 1),
    ]
    assert n == 15
    # Upserts follow the same batches; vectors land on the right chunk
    # across request boundaries.
    assert [(c.kwargs["user_id"], len(c.args[0])) for c in upsert.await_args_list] == [
        ("a", 4),
        ("a", 4),
        ("a", 2),
        ("b", 4),
        ("b", 1),
    ]
    a_points = [p for c in upsert.await_args_list[:3] for p in c.args[0]]
    assert [p[0] for p in a_points] == [chunk_point_id(a1.id, i) for i in range(5)] + [
        chunk_point_id(a2.id, i) for i in range(5)
    ]
    assert [p[1] for p in a_points] == [[float(len(t))] for t in split_content(LONG)] * 2


async def test_interleaved_users_are_grouped_across_the_page():
    """A shared context with alternating authors still fills its requests."""
    a1, b1, a2 = _memory(LONG, "a"), _memory(LONG, "b"), _memory(LONG, "a")
    svc = _embed_svc()
    await _rebuild([a1, b1, a2], embed_svc=svc)
    calls = svc.embed_batch.await_args_list
    assert [(c.args[1], len(c.args[0])) for c in calls] == [("a", 10), ("b", 5)]


async def test_flag_off_at_the_first_read_touches_nothing():
    svc = _embed_svc()
    n, delete, upsert, flag = await _rebuild([_memory(LONG)], flags=(False,), embed_svc=svc)
    assert n == 0
    delete.assert_not_awaited()
    svc.embed_batch.assert_not_awaited()
    upsert.assert_not_awaited()


async def test_flag_turned_off_while_embedding_writes_nothing():
    n, delete, upsert, flag = await _rebuild([_memory(LONG)], flags=(True, False))
    assert n == 0
    delete.assert_awaited_once()
    upsert.assert_not_awaited()
    assert flag.await_count == 2


async def test_each_flag_read_ends_its_transaction_before_the_next_call():
    order: list[str] = []
    db = _db()
    db.commit = AsyncMock(side_effect=lambda: order.append("commit"))
    await _rebuild([_memory(LONG)], embed_svc=_embed_svc(order), db=db)
    assert order == ["commit", "embed", "commit"]


async def test_resource_rows_and_short_pages_embed_nothing():
    doc = _memory(LONG, resource_id="res")
    short = _memory("short")
    svc = _embed_svc()
    with patch("services.resource_indexer.owns_resource_point", return_value=True):
        n, delete, upsert, _ = await _rebuild([doc, short], embed_svc=svc)
    assert n == 0
    delete.assert_awaited_once_with([short.id], TARGET)
    svc.embed_batch.assert_not_awaited()
    upsert.assert_not_awaited()


async def test_a_failed_delete_is_logged_for_the_page_and_raised():
    """F23: the page-wide delete failing is logged as a page event (context,
    memory count, error class), not blamed on the page's first memory."""
    with (
        patch.object(writer, "chunk_points_supported", return_value=True),
        patch.object(writer, "content_chunks_enabled", AsyncMock(return_value=True)),
        patch.object(writer, "delete_chunk_points", AsyncMock(side_effect=RuntimeError("q"))),
        patch.object(writer, "_log_failure") as per_memory,
        patch.object(writer, "logger") as logger,
        pytest.raises(RuntimeError),
    ):
        await writer.rebuild_content_chunks(
            _db(),
            [_memory(LONG), _memory(LONG)],
            base_payloads={},
            embed_svc=_embed_svc(),
            collection_name=TARGET,
            context_id=CTX,
        )
    per_memory.assert_not_called()
    logger.warning.assert_called_once_with(
        "content_chunks_page_delete_failed",
        context_id=CTX,
        collection=TARGET,
        memories=2,
        error_class="RuntimeError",
    )


async def test_a_failed_embedding_is_raised_not_swallowed():
    """Not fail-open: the migration stops and is re-run."""
    svc = MagicMock()
    svc.embed_batch = AsyncMock(side_effect=RuntimeError("provider down"))
    with pytest.raises(RuntimeError, match="provider down"):
        await _rebuild([_memory(LONG)], embed_svc=svc)


async def test_a_short_embedding_batch_is_an_error():
    svc = MagicMock()
    svc.embed_batch = AsyncMock(return_value=[[0.1]])
    with pytest.raises(RuntimeError, match="vectors"):
        await _rebuild([_memory(LONG)], embed_svc=svc)


async def test_a_store_without_chunk_points_is_a_no_op():
    with (
        patch.object(writer, "chunk_points_supported", return_value=False),
        patch.object(writer, "content_chunks_enabled", AsyncMock()) as flag,
    ):
        n = await writer.rebuild_content_chunks(
            _db(),
            [_memory(LONG)],
            base_payloads={},
            embed_svc=_embed_svc(),
            collection_name=TARGET,
            context_id=CTX,
        )
    assert n == 0
    flag.assert_not_awaited()


async def test_page_usage_is_summed_over_the_batches_and_hashes_are_written():
    """#2015 PR 2b: the page's embed usage, and each point's content_hash."""
    from utils.content_chunks import content_hash

    rows = [_memory(LONG), _memory(LONG, user_id="v")]  # two users, two requests
    svc = MagicMock()

    async def _embed(texts, *a, usage=None, **k):
        usage.update(cached=1, requests=1, tokens=10 * len(texts))
        return [[0.0] for _ in texts]

    svc.embed_batch = AsyncMock(side_effect=_embed)
    usage: dict[str, int] = {}
    flag = AsyncMock(side_effect=[True, True])
    with (
        patch.object(writer, "chunk_points_supported", return_value=True),
        patch.object(writer, "content_chunks_enabled", flag),
        patch.object(writer, "delete_chunk_points", AsyncMock()),
        patch.object(writer, "upsert_chunk_points", AsyncMock()) as upsert,
    ):
        await writer.rebuild_content_chunks(
            _db(),
            rows,
            base_payloads={m.id: {"summary": "s"} for m in rows},
            embed_svc=svc,
            collection_name=TARGET,
            context_id=CTX,
            usage=usage,
        )
    assert usage == {"cached": 2, "requests": 2, "tokens": 100}
    hashes = {p[2]["content_hash"] for call in upsert.await_args_list for p in call.args[0]}
    assert hashes == {content_hash(LONG)}
