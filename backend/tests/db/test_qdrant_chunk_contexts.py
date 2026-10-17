"""#2015 PR 2a: per-context chunk filter, chunk purge in every collection, chunk counts."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from qdrant_client.models import (
    FieldCondition,
    FilterSelector,
    IsEmptyCondition,
    MatchAny,
)

import db.qdrant as qmod
from db.qdrant import (
    count_context_chunk_points,
    purge_context_chunk_points_everywhere,
    search_memories_qdrant,
)
from utils.content_chunks import CHUNK_OF_FIELD
from utils.exceptions import QdrantError

pytestmark = pytest.mark.asyncio

WS = "11111111-1111-4111-8111-111111111111"
CTX_ON = "22222222-2222-4222-8222-222222222222"
CTX_OFF = "33333333-3333-4333-8333-333333333333"
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


async def test_a_context_list_admits_chunks_of_those_contexts_only(mock_client):
    await search_memories_qdrant(
        UID, [0.1], WS, [CTX_ON, CTX_OFF], include_chunks=[CTX_ON], is_shared_context=True
    )
    flt = mock_client.query_points.await_args.kwargs["query_filter"]
    assert not any(_is_chunk_exclusion(c) for c in flt.must)
    nested = [c for c in flt.must if getattr(c, "should", None)]
    assert len(nested) == 1
    summary_or_chunk_of = nested[0].should
    assert _is_chunk_exclusion(summary_or_chunk_of[0])
    assert isinstance(summary_or_chunk_of[1], FieldCondition)
    assert summary_or_chunk_of[1].key == "context_id"
    assert summary_or_chunk_of[1].match == MatchAny(any=[CTX_ON])
    # The isolation filter (workspace + every listed context) still applies.
    keys = {c.key for c in flt.must if isinstance(c, FieldCondition)}
    assert {"workspace_id", "context_id"} <= keys


async def test_an_empty_context_list_is_summary_points_only(mock_client):
    await search_memories_qdrant(UID, [0.1], WS, [CTX_ON, CTX_OFF], include_chunks=[])
    flt = mock_client.query_points.await_args.kwargs["query_filter"]
    assert any(_is_chunk_exclusion(c) for c in flt.must)
    assert not [c for c in flt.must if getattr(c, "should", None)]


async def test_a_malformed_chunk_context_id_is_refused(mock_client):
    with pytest.raises(ValueError):
        await search_memories_qdrant(UID, [0.1], WS, [CTX_ON], include_chunks=["x'; drop"])
    mock_client.query_points.assert_not_awaited()


async def test_purge_everywhere_asks_every_memory_collection(mock_client, monkeypatch):
    monkeypatch.setattr(
        qmod, "list_memory_collections", AsyncMock(return_value=["kagura_memories", "kagura_b"])
    )
    names = await purge_context_chunk_points_everywhere(WS, CTX_ON)
    assert names == ["kagura_memories", "kagura_b"]
    assert [c.kwargs["collection_name"] for c in mock_client.delete.await_args_list] == names
    for call in mock_client.delete.await_args_list:
        selector = call.kwargs["points_selector"]
        assert isinstance(selector, FilterSelector)
        assert _is_chunk_exclusion(selector.filter.must_not[0])  # chunk points only


async def test_purge_everywhere_tries_every_collection_before_raising(mock_client, monkeypatch):
    monkeypatch.setattr(
        qmod, "list_memory_collections", AsyncMock(return_value=["kagura_a", "kagura_b"])
    )
    mock_client.delete.side_effect = [RuntimeError("down"), None]
    with pytest.raises(QdrantError, match="1 collection"):
        await purge_context_chunk_points_everywhere(WS, CTX_ON)
    assert mock_client.delete.await_count == 2


async def test_purge_everywhere_is_a_noop_off_qdrant(monkeypatch):
    monkeypatch.setattr(qmod, "_active_store", lambda: object())
    listed = AsyncMock()
    monkeypatch.setattr(qmod, "list_memory_collections", listed)
    assert await purge_context_chunk_points_everywhere(WS, CTX_ON) == []
    listed.assert_not_awaited()


async def test_count_context_chunk_points_groups_by_memory(mock_client):
    page1 = [SimpleNamespace(payload={CHUNK_OF_FIELD: "m1"}) for _ in range(2)]
    page2 = [SimpleNamespace(payload={CHUNK_OF_FIELD: "m2"}), SimpleNamespace(payload={})]
    mock_client.scroll.side_effect = [(page1, "next"), (page2, None)]
    counts = await count_context_chunk_points(WS, CTX_ON, "coll")
    assert counts == {"m1": 2, "m2": 1}
    kwargs = mock_client.scroll.await_args_list[0].kwargs
    assert kwargs["with_payload"] == [CHUNK_OF_FIELD]
    assert kwargs["with_vectors"] is False
    assert _is_chunk_exclusion(kwargs["scroll_filter"].must_not[0])


async def test_count_context_chunk_points_failure_is_a_qdrant_error(mock_client):
    mock_client.scroll.side_effect = RuntimeError("down")
    with pytest.raises(QdrantError):
        await count_context_chunk_points(WS, CTX_ON, "coll")
