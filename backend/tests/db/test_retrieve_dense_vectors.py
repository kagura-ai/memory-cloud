"""``db.qdrant.retrieve_dense_vectors`` (#2256): stored vectors without an embed call.

The Qdrant path runs against a mocked client; the LanceDB preview path (#2520)
runs against an on-disk table under ``tmp_path`` and is skipped when the
``lite`` extra (lancedb, pyarrow) is not installed.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

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
async def test_a_non_qdrant_store_answers_the_read(monkeypatch):
    """#2520: the active store is asked, Qdrant never is."""
    calls = []

    class _Store:
        async def retrieve_dense_vectors(self, collection_name, point_ids):
            calls.append((collection_name, list(point_ids)))
            return {"a": [0.5, 0.25]}

    def _no_qdrant():
        raise AssertionError("the Qdrant client was used with another store active")

    monkeypatch.setattr(qmod, "_active_store", _Store)
    monkeypatch.setattr(qmod, "get_qdrant_client", _no_qdrant)

    assert await retrieve_dense_vectors("col", ["a", "b"]) == {"a": [0.5, 0.25]}
    assert calls == [("col", ["a", "b"])]


# ---------------------------------------------------------------------------
# LanceDB preview store against a real table (#2520)
# ---------------------------------------------------------------------------
WS = "11111111-1111-4111-8111-111111111111"
CTX = "22222222-2222-4222-8222-222222222222"
USER = "google-oauth2|123"
DEFAULT = "kagura_memories"
VARIANT = DEFAULT + "_voyage_2_1024"


@pytest.fixture
def lance(tmp_path, monkeypatch):
    """An on-disk LanceDB store, active for ``db.qdrant``'s dispatch."""
    pytest.importorskip("lancedb")
    from db.lance_store import LanceVectorStore

    store = LanceVectorStore(str(tmp_path / "lance"))
    monkeypatch.setattr(qmod, "_active_store", lambda: store)
    return store


async def _add(store, memory_id: UUID, vector: list[float], collection: str = DEFAULT) -> None:
    await store.add_memory(
        user_id=USER,
        memory_id=memory_id,
        vector=vector,
        payload={"summary": "s", "type": "note", "scope": "working"},
        workspace_id=WS,
        context_id=CTX,
        collection_name=collection,
    )


class TestLanceStore:
    async def test_is_part_of_the_store_contract(self, lance):
        from db.vector_store import VectorStore

        assert isinstance(lance, VectorStore)

    async def test_returns_the_stored_vector_of_each_existing_point(self, lance):
        first, second, third = uuid4(), uuid4(), uuid4()
        await _add(lance, first, [0.1, 0.2, 0.3, 0.4])
        await _add(lance, second, [0.5, 0.6, 0.7, 0.8])
        await _add(lance, third, [1.0, 0.0, 0.0, 0.0])
        missing = str(uuid4())

        vectors = await retrieve_dense_vectors(DEFAULT, [str(first), str(third), missing])

        assert set(vectors) == {str(first), str(third)}
        assert vectors[str(first)] == pytest.approx([0.1, 0.2, 0.3, 0.4])
        assert vectors[str(third)] == [1.0, 0.0, 0.0, 0.0]
        for vector in vectors.values():
            assert isinstance(vector, list)
            assert all(type(x) is float for x in vector)

    async def test_no_ids_opens_no_table(self, lance, monkeypatch):
        await _add(lance, uuid4(), [0.1, 0.2, 0.3, 0.4])
        opened = []
        monkeypatch.setattr(lance, "_open", lambda *args, **kwargs: opened.append(args))

        assert await retrieve_dense_vectors(DEFAULT, []) == {}
        assert opened == []

    async def test_a_missing_collection_reads_nothing(self, lance):
        await _add(lance, uuid4(), [0.1, 0.2, 0.3, 0.4])

        assert await retrieve_dense_vectors(VARIANT, [str(uuid4())]) == {}
        assert await lance.list_collections() == [DEFAULT]  # nothing was created

    async def test_collections_are_read_apart(self, lance):
        memory_id = uuid4()
        await _add(lance, memory_id, [0.1, 0.2, 0.3, 0.4], collection=VARIANT)
        await _add(lance, uuid4(), [0.5, 0.6, 0.7, 0.8])

        assert await retrieve_dense_vectors(DEFAULT, [str(memory_id)]) == {}
        assert set(await retrieve_dense_vectors(VARIANT, [str(memory_id)])) == {str(memory_id)}

    async def test_more_ids_than_one_statement_holds(self, lance):
        first, last = uuid4(), uuid4()
        await _add(lance, first, [0.1, 0.2, 0.3, 0.4])
        await _add(lance, last, [0.5, 0.6, 0.7, 0.8])
        # 501 ids → two IN (...) reads; the two that exist sit in different ones.
        ids = [str(first)] + [str(uuid4()) for _ in range(499)] + [str(last)]

        vectors = await retrieve_dense_vectors(DEFAULT, ids)

        assert set(vectors) == {str(first), str(last)}

    async def test_a_quote_in_an_id_is_data(self, lance):
        await _add(lance, uuid4(), [0.1, 0.2, 0.3, 0.4])

        assert await retrieve_dense_vectors(DEFAULT, ["x' OR '1'='1"]) == {}

    async def test_a_failed_read_is_a_qdrant_error(self, lance, monkeypatch):
        def _broken(*args, **kwargs):
            raise RuntimeError("disk gone")

        monkeypatch.setattr(lance, "_open", _broken)

        with pytest.raises(QdrantError):
            await retrieve_dense_vectors(DEFAULT, [str(uuid4())])
