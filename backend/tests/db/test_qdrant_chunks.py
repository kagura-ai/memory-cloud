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
    delete_memory_from_qdrant,
    search_memories_qdrant,
    update_memory_payload_in_qdrant,
    upsert_chunk_points,
)
from utils.content_chunks import CHUNK_OF_FIELD

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


async def test_chunk_helpers_are_noops_on_a_non_qdrant_store(monkeypatch):
    client = AsyncMock()
    monkeypatch.setattr(qmod, "get_qdrant_client", lambda: client)
    monkeypatch.setattr(qmod, "_active_store", lambda: object())
    await upsert_chunk_points([("i", [0.1], {})], user_id=UID, workspace_id=WS, context_id=CTX)
    await delete_chunk_points([uuid4()])
    client.upsert.assert_not_awaited()
    client.delete.assert_not_awaited()
