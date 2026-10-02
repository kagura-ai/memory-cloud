"""#1808: the orphan sweep's LanceDB reads and deletes against a real table.

``test_orphan_sweep_store_reads.py`` fakes the table; this file runs the same
three calls (``list_collections``, ``list_point_refs``, ``delete_points``)
through an on-disk LanceDB store under ``tmp_path``. Skipped when the ``lite``
extra (lancedb, pyarrow) is not installed.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

pytest.importorskip("lancedb")

from db.lance_store import DEFAULT_COLLECTION, LanceVectorStore  # noqa: E402

WS = "11111111-1111-4111-8111-111111111111"
CTX = "22222222-2222-4222-8222-222222222222"
CTX2 = "33333333-3333-4333-8333-333333333333"
USER = "google-oauth2|123"
VARIANT = DEFAULT_COLLECTION + "_voyage_2_1024"


async def _add(
    store: LanceVectorStore,
    memory_id: UUID,
    *,
    context_id: str = CTX,
    collection: str = DEFAULT_COLLECTION,
) -> None:
    await store.add_memory(
        user_id=USER,
        memory_id=memory_id,
        vector=[0.1, 0.2, 0.3, 0.4],
        payload={"summary": "s", "type": "note", "scope": "working"},
        workspace_id=WS,
        context_id=context_id,
        collection_name=collection,
    )


@pytest.fixture
def store(tmp_path) -> LanceVectorStore:
    return LanceVectorStore(str(tmp_path / "lance"))


class TestListCollections:
    async def test_empty_store_has_none(self, store):
        assert await store.list_collections() == []

    async def test_lists_memory_tables_only(self, store):
        await _add(store, uuid4(), collection=VARIANT)
        await _add(store, uuid4())
        # A table that is not a memory collection must never be swept.
        store._connect().create_table("unrelated", data=[{"id": "x"}])

        assert await store.list_collections() == [DEFAULT_COLLECTION, VARIANT]


class TestListPointRefs:
    async def test_every_row_with_its_context(self, store):
        ids = [uuid4(), uuid4(), uuid4()]
        await _add(store, ids[0])
        await _add(store, ids[1], context_id=CTX2)
        await _add(store, ids[2])

        refs = await store.list_point_refs(DEFAULT_COLLECTION)

        assert sorted(refs) == sorted([(str(ids[0]), CTX), (str(ids[1]), CTX2), (str(ids[2]), CTX)])

    async def test_missing_collection_lists_nothing(self, store):
        assert await store.list_point_refs(VARIANT) == []

    async def test_collections_are_listed_apart(self, store):
        in_default, in_variant = uuid4(), uuid4()
        await _add(store, in_default)
        await _add(store, in_variant, collection=VARIANT)

        assert await store.list_point_refs(VARIANT) == [(str(in_variant), CTX)]

    async def test_lists_every_row_of_a_larger_table(self, store):
        # A vector query defaults to 10 rows; a listing cut short at any page
        # size leaves the rest of the table unjudged by the sweep.
        ids = [uuid4() for _ in range(25)]
        for memory_id in ids:
            await _add(store, memory_id)

        refs = await store.list_point_refs(DEFAULT_COLLECTION)

        assert {point_id for point_id, _ in refs} == {str(i) for i in ids}


class TestDeletePoints:
    async def test_deletes_exactly_the_given_ids(self, store):
        keep, gone_a, gone_b = uuid4(), uuid4(), uuid4()
        for memory_id in (keep, gone_a, gone_b):
            await _add(store, memory_id)

        await store.delete_points([str(gone_a), str(gone_b)], DEFAULT_COLLECTION)

        assert await store.list_point_refs(DEFAULT_COLLECTION) == [(str(keep), CTX)]

    async def test_more_ids_than_one_statement_holds(self, store):
        ids = [uuid4() for _ in range(3)]
        for memory_id in ids:
            await _add(store, memory_id)
        # 501 ids → two IN (...) statements; only two of them exist.
        doomed = [str(ids[0])] + [str(uuid4()) for _ in range(499)] + [str(ids[2])]

        await store.delete_points(doomed, DEFAULT_COLLECTION)

        assert await store.list_point_refs(DEFAULT_COLLECTION) == [(str(ids[1]), CTX)]

    async def test_a_quote_in_an_id_is_data(self, store):
        memory_id = uuid4()
        await _add(store, memory_id)

        await store.delete_points(["x' OR '1'='1"], DEFAULT_COLLECTION)

        assert await store.list_point_refs(DEFAULT_COLLECTION) == [(str(memory_id), CTX)]

    async def test_missing_collection_is_a_no_op(self, store):
        await store.delete_points([str(uuid4())], VARIANT)

        assert await store.list_collections() == []
