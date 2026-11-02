"""``db.qdrant.retrieve_dense_vectors`` (#2256): stored vectors without an embed call."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import db.qdrant as qmod
from db.qdrant import KAGURA_MEMORIES_VECTOR_NAME, retrieve_dense_vectors
from utils.exceptions import QdrantError


@pytest.fixture
def client(monkeypatch):
    client = AsyncMock()
    monkeypatch.setattr(qmod, "get_qdrant_client", lambda: client)
    monkeypatch.setattr(qmod, "_active_store", lambda: None)
    return client


@pytest.mark.asyncio
async def test_returns_the_dense_vector_of_each_point(client):
    client.retrieve.return_value = [
        SimpleNamespace(id="a", vector={KAGURA_MEMORIES_VECTOR_NAME: [0.1, 0.2]}),
        SimpleNamespace(id="b", vector={"other": [0.3]}),  # no dense vector
        SimpleNamespace(id="c", vector=None),
    ]

    vectors = await retrieve_dense_vectors("col", ["a", "b", "c", "missing"])

    assert vectors == {"a": [0.1, 0.2]}
    kwargs = client.retrieve.await_args.kwargs
    assert kwargs == {
        "collection_name": "col",
        "ids": ["a", "b", "c", "missing"],
        "with_vectors": [KAGURA_MEMORIES_VECTOR_NAME],
        "with_payload": False,
    }


@pytest.mark.asyncio
async def test_no_ids_reads_nothing(client):
    assert await retrieve_dense_vectors("col", []) == {}
    client.retrieve.assert_not_called()


@pytest.mark.asyncio
async def test_a_failed_retrieve_is_a_qdrant_error(client):
    client.retrieve.side_effect = RuntimeError("down")
    with pytest.raises(QdrantError):
        await retrieve_dense_vectors("col", ["a"])


@pytest.mark.asyncio
async def test_a_non_qdrant_store_is_not_supported(monkeypatch):
    monkeypatch.setattr(qmod, "_active_store", lambda: object())
    with pytest.raises(NotImplementedError):
        await retrieve_dense_vectors("col", ["a"])
