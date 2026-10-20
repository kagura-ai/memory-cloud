"""#2015: Qdrant helpers for content chunk points."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from qdrant_client.models import (
    FieldCondition,
    FilterSelector,
    IsEmptyCondition,
    MatchAny,
    MatchValue,
    PointIdsList,
)

import db.qdrant as qmod
from db.qdrant import (
    KAGURA_MEMORIES_VECTOR_NAME,
    delete_chunk_points,
    delete_chunk_points_from,
    delete_memory_from_qdrant,
    search_memories_qdrant,
    update_memory_payload_in_qdrant,
    upsert_chunk_points,
)
from utils.content_chunks import CHUNK_OF_FIELD
from utils.exceptions import QdrantError

pytestmark = pytest.mark.asyncio

WS = "11111111-1111-4111-8111-111111111111"
CTX = "22222222-2222-4222-8222-222222222222"
UID = "google-oauth2|user-123"


@pytest.fixture
def mock_client(monkeypatch):
    client = AsyncMock()
    client.query_points.return_value = SimpleNamespace(points=[])
    monkeypatch.setattr(qmod, "get_qdrant_client", lambda: client)
    monkeypatch.setattr(qmod, "_active_store", lambda: None)
    return client


def _is_chunk_exclusion(cond) -> bool:
    return isinstance(cond, IsEmptyCondition) and cond.is_empty.key == CHUNK_OF_FIELD


async def test_search_excludes_chunk_points_by_default(mock_client):
    await search_memories_qdrant(UID, [0.1], WS, CTX)
    flt = mock_client.query_points.await_args.kwargs["query_filter"]
    assert any(_is_chunk_exclusion(c) for c in flt.must)


async def test_search_includes_chunk_points_when_asked(mock_client):
    await search_memories_qdrant(UID, [0.1], WS, CTX, include_chunks=True)
    flt = mock_client.query_points.await_args.kwargs["query_filter"]
    assert not any(_is_chunk_exclusion(c) for c in flt.must)


async def test_delete_memory_also_deletes_its_chunks(mock_client):
    mid = uuid4()
    await delete_memory_from_qdrant(UID, mid)
    calls = mock_client.delete.await_args_list
    assert isinstance(calls[0].kwargs["points_selector"], PointIdsList)
    selector = calls[1].kwargs["points_selector"]
    assert isinstance(selector, FilterSelector)
    cond = selector.filter.must[0]
    assert cond.key == CHUNK_OF_FIELD and cond.match == MatchAny(any=[str(mid)])


async def test_delete_memory_chunk_cascade_failure_is_soft(mock_client):
    mock_client.delete.side_effect = [None, RuntimeError("chunks")]
    await delete_memory_from_qdrant(UID, uuid4())  # no raise


async def test_payload_update_cascades_to_chunks_without_token_fields(mock_client):
    mid = uuid4()
    await update_memory_payload_in_qdrant(
        mid, {"tags": ["a"], "summary_tokens": "t"}, delete_keys=["location"]
    )
    set_calls = mock_client.set_payload.await_args_list
    assert set_calls[0].kwargs["points"] == [str(mid)]
    chunk_set = set_calls[1].kwargs
    assert chunk_set["payload"] == {"tags": ["a"]}
    cond = chunk_set["points"].must[0]
    assert isinstance(cond, FieldCondition) and cond.match == MatchValue(value=str(mid))
    del_calls = mock_client.delete_payload.await_args_list
    assert del_calls[0].kwargs["points"] == [str(mid)]
    assert del_calls[1].kwargs["keys"] == ["location"]


async def test_payload_update_with_only_token_fields_skips_chunk_set(mock_client):
    await update_memory_payload_in_qdrant(uuid4(), {"summary_tokens": "t"})
    assert mock_client.set_payload.await_count == 1


async def test_upsert_chunk_points_one_call_dense_only_with_isolation(mock_client):
    pts = [("id-0", [0.1, 0.2], {CHUNK_OF_FIELD: "m"}), ("id-1", [0.3, 0.4], {CHUNK_OF_FIELD: "m"})]
    await upsert_chunk_points(pts, user_id=UID, workspace_id=WS, context_id=CTX)
    assert mock_client.upsert.await_count == 1
    points = mock_client.upsert.await_args.kwargs["points"]
    assert [p.id for p in points] == ["id-0", "id-1"]
    assert set(points[0].vector) == {KAGURA_MEMORIES_VECTOR_NAME}
    assert points[0].payload["workspace_id"] == WS
    assert points[0].payload["context_id"] == CTX
    assert points[0].payload["user_id"] == UID


async def test_upsert_chunk_points_empty_is_noop(mock_client):
    await upsert_chunk_points([], user_id=UID, workspace_id=WS, context_id=CTX)
    mock_client.upsert.assert_not_awaited()


async def test_delete_chunk_points_by_memory_ids(mock_client):
    ids = [uuid4(), uuid4()]
    await delete_chunk_points(ids)
    selector = mock_client.delete.await_args.kwargs["points_selector"]
    assert isinstance(selector, FilterSelector)
    assert selector.filter.must[0].match == MatchAny(any=[str(i) for i in ids])


async def test_delete_chunk_points_empty_is_noop(mock_client):
    await delete_chunk_points([])
    mock_client.delete.assert_not_awaited()


async def test_delete_chunk_points_from_trims_the_chunks_past_the_new_count(mock_client):
    """#2061: the trim after an in-place rewrite deletes chunk_index >= start of
    that memory only — by filter, so a chunk past the cap goes too."""
    memory_id = uuid4()
    await delete_chunk_points_from(memory_id, 3, "coll")
    kwargs = mock_client.delete.await_args.kwargs
    assert kwargs["collection_name"] == "coll"
    selector = kwargs["points_selector"]
    assert isinstance(selector, FilterSelector)
    by_key = {cond.key: cond for cond in selector.filter.must}
    assert by_key[CHUNK_OF_FIELD].match == MatchValue(value=str(memory_id))
    assert by_key["chunk_index"].range.gte == 3
    assert by_key["chunk_index"].range.lt is None


async def test_delete_chunk_points_from_raises_a_qdrant_error(mock_client):
    mock_client.delete.side_effect = RuntimeError("down")
    with pytest.raises(QdrantError, match="trim content chunks"):
        await delete_chunk_points_from(uuid4(), 0)


async def test_chunk_helpers_are_noops_on_a_non_qdrant_store(monkeypatch):
    client = AsyncMock()
    monkeypatch.setattr(qmod, "get_qdrant_client", lambda: client)
    monkeypatch.setattr(qmod, "_active_store", lambda: object())
    await upsert_chunk_points([("i", [0.1], {})], user_id=UID, workspace_id=WS, context_id=CTX)
    await delete_chunk_points([uuid4()])
    await delete_chunk_points_from(uuid4(), 0)
    client.upsert.assert_not_awaited()
    client.delete.assert_not_awaited()


async def test_purge_context_chunk_points_targets_chunks_of_one_context(mock_client):
    from db.qdrant import purge_context_chunk_points

    await purge_context_chunk_points(WS, CTX, "coll")
    kwargs = mock_client.delete.await_args.kwargs
    assert kwargs["collection_name"] == "coll"
    flt = kwargs["points_selector"].filter
    keys = {c.key: c.match.value for c in flt.must}
    assert keys == {"workspace_id": WS, "context_id": CTX}
    assert [c.is_empty.key for c in flt.must_not] == [CHUNK_OF_FIELD]


async def test_purge_context_chunk_points_is_a_noop_off_qdrant(monkeypatch):
    from db.qdrant import purge_context_chunk_points

    client = AsyncMock()
    monkeypatch.setattr(qmod, "get_qdrant_client", lambda: client)
    monkeypatch.setattr(qmod, "_active_store", lambda: object())
    await purge_context_chunk_points(WS, CTX, "coll")
    client.delete.assert_not_awaited()


async def test_failed_payload_cascade_deletes_the_chunks_instead(mock_client):
    """An isolation field (user_id, creator transfer) must never stay behind
    on a chunk: when the cascade fails, the chunks go (re-embed rebuilds)."""
    mid = uuid4()
    mock_client.set_payload.side_effect = [None, RuntimeError("cascade")]
    await update_memory_payload_in_qdrant(mid, {"user_id": "new-owner"})
    selector = mock_client.delete.await_args.kwargs["points_selector"]
    assert isinstance(selector, FilterSelector)
    assert selector.filter.must[0].match == MatchValue(value=str(mid))


async def test_failed_payload_cascade_and_failed_delete_raise(mock_client):
    from utils.exceptions import QdrantError

    mock_client.set_payload.side_effect = [None, RuntimeError("cascade")]
    mock_client.delete.side_effect = RuntimeError("delete")
    with pytest.raises(QdrantError):
        await update_memory_payload_in_qdrant(uuid4(), {"user_id": "new-owner"})


async def test_cascade_can_be_skipped_for_a_context_without_chunks(mock_client):
    await update_memory_payload_in_qdrant(uuid4(), {"tags": ["a"]}, cascade_chunks=False)
    assert mock_client.set_payload.await_count == 1


async def test_chunk_of_index_pass_adds_the_index_where_missing(mock_client):
    from db.qdrant import ensure_chunk_of_index_on_all

    mock_client.get_collections.return_value = SimpleNamespace(
        collections=[
            SimpleNamespace(name="kagura_memories"),
            SimpleNamespace(name="kagura_memories_x_8"),
            SimpleNamespace(name="other"),
        ]
    )
    schemas = {
        "kagura_memories": {"tags": object()},
        "kagura_memories_x_8": {CHUNK_OF_FIELD: object()},
    }
    mock_client.get_collection.side_effect = lambda name: SimpleNamespace(
        payload_schema=schemas[name]
    )

    assert await ensure_chunk_of_index_on_all() == ["kagura_memories"]
    kwargs = mock_client.create_payload_index.await_args.kwargs
    assert kwargs == {
        "collection_name": "kagura_memories",
        "field_name": CHUNK_OF_FIELD,
        "field_schema": "keyword",
    }


async def test_chunk_of_index_pass_is_fail_soft(mock_client):
    from db.qdrant import ensure_chunk_of_index_on_all

    mock_client.get_collections.return_value = SimpleNamespace(
        collections=[
            SimpleNamespace(name="kagura_memories"),
            SimpleNamespace(name="kagura_memories_b"),
        ]
    )
    mock_client.get_collection.side_effect = [
        RuntimeError("down"),
        SimpleNamespace(payload_schema={}),
    ]
    assert await ensure_chunk_of_index_on_all() == ["kagura_memories_b"]

    mock_client.get_collections.side_effect = RuntimeError("down")
    assert await ensure_chunk_of_index_on_all() == []


async def test_list_context_point_ids_leaves_chunk_points_out(mock_client):
    """#2015: the embedding-migration verify must not delete chunks as stale."""
    from db.qdrant import list_context_point_ids

    mock_client.scroll.return_value = ([], None)
    await list_context_point_ids(WS, CTX, "coll")
    flt = mock_client.scroll.await_args.kwargs["scroll_filter"]
    assert any(_is_chunk_exclusion(c) for c in flt.must)


# --------------------------------------------------------------------- #2041


async def test_summary_points_filter_appends_the_chunk_exclusion():
    from qdrant_client.models import PayloadField

    from db.qdrant import summary_points_filter

    ws = FieldCondition(key="workspace_id", match=MatchValue(value=WS))
    located = IsEmptyCondition(is_empty=PayloadField(key="location"))

    flt = summary_points_filter(ws, must_not=[located])

    assert flt.must is not None and len(flt.must) == 2
    assert flt.must[0] is ws  # caller conditions first, order kept
    assert _is_chunk_exclusion(flt.must[1])
    assert flt.must_not == [located]
    bare = summary_points_filter()
    assert bare.must is not None and [_is_chunk_exclusion(c) for c in bare.must] == [True]
    assert bare.must_not is None


async def test_admin_context_scroll_leaves_chunk_points_out(mock_client):
    """#2041: the BM25 reveal-terms scroll reads token fields chunks never carry."""
    from db.qdrant import _admin_scroll_context_points

    mock_client.scroll.return_value = ([], None)
    _ = [page async for page in _admin_scroll_context_points(CTX)]
    flt = mock_client.scroll.await_args.kwargs["scroll_filter"]
    assert any(_is_chunk_exclusion(c) for c in flt.must)
    assert any(isinstance(c, FieldCondition) and c.key == "context_id" for c in flt.must)


async def test_sync_chunk_points_payload_sets_only_the_chunks(mock_client):
    """#2015 PR 2b: a metadata-only Sleep change, no summary point touched."""
    from db.qdrant import sync_chunk_points_payload

    mid = uuid4()
    ok = await sync_chunk_points_payload(mid, {"scope": "persistent", "summary_tokens": "t"}, "c")
    assert ok is True
    call = mock_client.set_payload.await_args.kwargs
    assert call["payload"] == {"scope": "persistent"}
    assert call["points"].must[0].match == MatchValue(value=str(mid))
    assert mock_client.set_payload.await_count == 1
    mock_client.delete.assert_not_awaited()


async def test_sync_chunk_points_payload_reports_a_fallback_delete(mock_client):
    from db.qdrant import sync_chunk_points_payload

    mock_client.set_payload.side_effect = RuntimeError("down")
    assert await sync_chunk_points_payload(uuid4(), {"scope": "persistent"}, "c") is False
    mock_client.delete.assert_awaited_once()


async def test_sync_chunk_points_payload_is_a_no_op_off_qdrant(monkeypatch):
    from db.qdrant import sync_chunk_points_payload

    client = AsyncMock()
    monkeypatch.setattr(qmod, "get_qdrant_client", lambda: client)
    monkeypatch.setattr(qmod, "_active_store", lambda: object())
    assert await sync_chunk_points_payload(uuid4(), {"scope": "persistent"}, "c") is True
    client.set_payload.assert_not_awaited()


async def test_sync_chunk_points_payload_removes_the_given_keys(mock_client):
    from db.qdrant import sync_chunk_points_payload

    mid = uuid4()
    await sync_chunk_points_payload(mid, {"scope": "persistent"}, "c", delete_keys=["location"])
    call = mock_client.delete_payload.await_args.kwargs
    assert call["keys"] == ["location"]
    assert call["points"].must[0].match == MatchValue(value=str(mid))


async def test_chunk_points_content_hash_reads_chunk_zeros_payload(mock_client):
    """#2103: the hash an inactive context's kept chunks were split from."""
    from db.qdrant import chunk_points_content_hash
    from utils.content_chunks import chunk_point_id

    memory_id = uuid4()
    mock_client.retrieve.return_value = [
        SimpleNamespace(id=chunk_point_id(memory_id, 0), payload={"content_hash": "h1"})
    ]
    assert await chunk_points_content_hash(memory_id, "coll") == "h1"
    kwargs = mock_client.retrieve.await_args.kwargs
    assert kwargs["collection_name"] == "coll"
    assert kwargs["ids"] == [chunk_point_id(memory_id, 0)]
    assert kwargs["with_vectors"] is False

    mock_client.retrieve.return_value = []
    assert await chunk_points_content_hash(memory_id, "coll") is None
    mock_client.retrieve.side_effect = RuntimeError("down")
    with pytest.raises(QdrantError):
        await chunk_points_content_hash(memory_id, "coll")


async def test_chunk_points_content_hashes_maps_each_memory(mock_client):
    """#2107: one retrieve for a page; absent = no chunk 0, None = no hash."""
    from db.qdrant import chunk_points_content_hashes
    from utils.content_chunks import chunk_point_id

    hashed, unhashed, missing = uuid4(), uuid4(), uuid4()
    mock_client.retrieve.return_value = [
        SimpleNamespace(id=chunk_point_id(hashed, 0), payload={"content_hash": "h"}),
        SimpleNamespace(id=chunk_point_id(unhashed, 0), payload={}),
    ]
    out = await chunk_points_content_hashes([hashed, unhashed, missing], "coll")
    assert out == {str(hashed): "h", str(unhashed): None}
    kwargs = mock_client.retrieve.await_args.kwargs
    assert kwargs["ids"] == [chunk_point_id(m, 0) for m in (hashed, unhashed, missing)]
    assert kwargs["with_payload"] == ["content_hash"]

    mock_client.retrieve.reset_mock()
    assert await chunk_points_content_hashes([], "coll") == {}
    mock_client.retrieve.assert_not_awaited()
