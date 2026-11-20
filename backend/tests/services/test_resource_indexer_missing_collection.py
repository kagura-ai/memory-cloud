"""The resource indexer creates a missing collection once and retries (#2952)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest
from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.models import PointStruct

from services import resource_indexer
from services.resource_indexer import ResourceIndexer


def _qdrant_error(status: int) -> UnexpectedResponse:
    return UnexpectedResponse(
        status_code=status,
        reason_phrase="Not Found" if status == 404 else "Error",
        content=b'{"status":{"error":"Collection `c` doesn\'t exist!"}}',
        headers=httpx.Headers(),
    )


def _indexer(upsert: AsyncMock) -> ResourceIndexer:
    indexer = object.__new__(ResourceIndexer)
    indexer.qdrant_client = AsyncMock()
    indexer.qdrant_client.upsert = upsert
    return indexer


POINT = PointStruct(id="00000000-0000-0000-0000-000000000001", vector={}, payload={})


async def test_missing_collection_is_ensured_and_the_point_written(monkeypatch):
    ensure = AsyncMock()
    monkeypatch.setattr(resource_indexer, "ensure_kagura_memories_collection", ensure)
    upsert = AsyncMock(side_effect=[_qdrant_error(404), None])
    await _indexer(upsert)._upsert_point("kagura_memories_m_1024", POINT, 1024)
    ensure.assert_awaited_once_with(1024, "kagura_memories_m_1024")
    assert upsert.await_count == 2


async def test_other_qdrant_errors_are_not_retried(monkeypatch):
    ensure = AsyncMock()
    monkeypatch.setattr(resource_indexer, "ensure_kagura_memories_collection", ensure)
    upsert = AsyncMock(side_effect=_qdrant_error(500))
    with pytest.raises(UnexpectedResponse):
        await _indexer(upsert)._upsert_point("c", POINT, 1024)
    ensure.assert_not_awaited()
    assert upsert.await_count == 1


async def test_a_second_404_after_the_ensure_propagates(monkeypatch):
    monkeypatch.setattr(resource_indexer, "ensure_kagura_memories_collection", AsyncMock())
    upsert = AsyncMock(side_effect=[_qdrant_error(404), _qdrant_error(404)])
    with pytest.raises(UnexpectedResponse):
        await _indexer(upsert)._upsert_point("c", POINT, 1024)
    assert upsert.await_count == 2


async def test_a_failed_ensure_still_retries_the_upsert(monkeypatch):
    """Another writer created the collection first (409 on create): the point still lands."""
    ensure = AsyncMock(side_effect=RuntimeError("already exists"))
    monkeypatch.setattr(resource_indexer, "ensure_kagura_memories_collection", ensure)
    upsert = AsyncMock(side_effect=[_qdrant_error(404), None])
    await _indexer(upsert)._upsert_point("c", POINT, 1024)
    assert upsert.await_count == 2


def test_dense_dimensions_reads_the_named_dense_vector():
    from db.qdrant import KAGURA_MEMORIES_VECTOR_NAME
    from services.resource_indexer import _dense_dimensions

    point = PointStruct(
        id="00000000-0000-0000-0000-000000000002",
        vector={KAGURA_MEMORIES_VECTOR_NAME: [0.1] * 1024},
        payload={},
    )
    assert _dense_dimensions(point) == 1024
