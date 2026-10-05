"""#1798: the vector-store reads and deletes behind context deletion and the orphan sweep.

Pure unit tests — the Qdrant client and the LanceDB table are faked.
"""

from __future__ import annotations

import threading
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import db.qdrant as qmod
from db.lance_store import LanceVectorStore
from db.qdrant import (
    KAGURA_MEMORIES_COLLECTION,
    PointRef,
    delete_context_points_everywhere,
    list_memory_collections,
    scroll_point_refs,
)
from utils.exceptions import QdrantError

WS = "11111111-1111-4111-8111-111111111111"
CTX = "22222222-2222-4222-8222-222222222222"
VARIANT = KAGURA_MEMORIES_COLLECTION + "_voyage_2_1024"


@pytest.fixture
def mock_client(monkeypatch):
    client = AsyncMock()
    monkeypatch.setattr(qmod, "get_qdrant_client", lambda: client)
    monkeypatch.setattr(qmod, "_active_store", lambda: None)
    return client


def _collections(*names: str) -> SimpleNamespace:
    return SimpleNamespace(collections=[SimpleNamespace(name=name) for name in names])


class TestListMemoryCollections:
    async def test_keeps_only_the_memory_collections(self, mock_client):
        mock_client.get_collections.return_value = _collections(
            VARIANT, "something_else", KAGURA_MEMORIES_COLLECTION
        )

        assert await list_memory_collections() == [KAGURA_MEMORIES_COLLECTION, VARIANT]

    async def test_failure_wrapped(self, mock_client):
        mock_client.get_collections.side_effect = RuntimeError("down")

        with pytest.raises(QdrantError, match="Failed to list collections"):
            await list_memory_collections()

    async def test_delegates_to_the_active_store(self, monkeypatch):
        store = AsyncMock()
        store.list_collections.return_value = [KAGURA_MEMORIES_COLLECTION]
        monkeypatch.setattr(qmod, "_active_store", lambda: store)

        assert await list_memory_collections() == [KAGURA_MEMORIES_COLLECTION]


class TestDeleteContextPointsEverywhere:
    async def test_deletes_from_every_collection_and_reports_the_ones_that_held_points(
        self, mock_client
    ):
        mock_client.get_collections.return_value = _collections(KAGURA_MEMORIES_COLLECTION, VARIANT)
        mock_client.count.side_effect = [SimpleNamespace(count=0), SimpleNamespace(count=5)]

        deleted = await delete_context_points_everywhere(WS, CTX)

        assert deleted == {VARIANT: 5}
        swept = [call.kwargs["collection_name"] for call in mock_client.delete.await_args_list]
        assert swept == [KAGURA_MEMORIES_COLLECTION, VARIANT]

    async def test_a_failing_collection_raises(self, mock_client):
        mock_client.get_collections.return_value = _collections(KAGURA_MEMORIES_COLLECTION)
        mock_client.count.side_effect = RuntimeError("x")

        with pytest.raises(QdrantError):
            await delete_context_points_everywhere(WS, CTX)


class TestScrollPointRefs:
    async def test_pages_until_the_offset_runs_out(self, mock_client):
        memory_point = SimpleNamespace(id="m1", payload={"context_id": CTX})
        resource_point = SimpleNamespace(
            id="r1", payload={"context_id": CTX, "resource_id": "docs"}
        )
        bare_point = SimpleNamespace(id="b1", payload=None)
        mock_client.scroll.side_effect = [
            ([memory_point, resource_point], "next"),
            ([bare_point], None),
        ]

        pages = [page async for page in scroll_point_refs(KAGURA_MEMORIES_COLLECTION)]

        assert pages == [
            [
                PointRef(point_id="m1", context_id=CTX, is_resource=False),
                PointRef(point_id="r1", context_id=CTX, is_resource=True),
            ],
            [PointRef(point_id="b1", context_id=None, is_resource=False)],
        ]
        first, second = mock_client.scroll.await_args_list
        assert first.kwargs["with_vectors"] is False
        # #1829: the sweep also reads the document key and the write time of
        # resource points — never content.
        assert first.kwargs["with_payload"] == [
            "context_id",
            "resource_id",
            "doc_id",
            "version",
            "updated_at",
            "indexed_at",
        ]
        assert second.kwargs["offset"] == "next"

    async def test_resource_point_write_time_is_indexed_at(self, mock_client):
        """#1869: ``updated_at`` is the event time (recall filters read it);
        the sweep's grace period needs when the point was actually written."""
        point = SimpleNamespace(
            id="r1",
            payload={
                "context_id": CTX,
                "resource_id": "docs",
                "doc_id": "d1",
                "version": 2,
                "updated_at": "2026-01-01T00:00:00Z",
                "indexed_at": "2026-03-01T12:30:00Z",
            },
        )
        mock_client.scroll.return_value = ([point], None)

        pages = [page async for page in scroll_point_refs(KAGURA_MEMORIES_COLLECTION)]

        assert pages == [
            [
                PointRef(
                    point_id="r1",
                    context_id=CTX,
                    is_resource=True,
                    resource_key=("docs", "d1", 2),
                    written_at=datetime(2026, 3, 1, 12, 30),
                )
            ]
        ]

    @pytest.mark.parametrize("indexed_at", [None, "not a timestamp", 7])
    async def test_point_without_indexed_at_falls_back_to_updated_at(self, mock_client, indexed_at):
        """A point written before #1869 has no ``indexed_at`` and no in-flight
        row, so its ``updated_at`` still decides — it is swept once that is
        past the grace period."""
        payload = {
            "context_id": CTX,
            "resource_id": "docs",
            "doc_id": "d1",
            "version": 2,
            "updated_at": "2026-01-01T00:00:00+00:00",
        }
        if indexed_at is not None:
            payload["indexed_at"] = indexed_at
        mock_client.scroll.return_value = ([SimpleNamespace(id="r1", payload=payload)], None)

        pages = [page async for page in scroll_point_refs(KAGURA_MEMORIES_COLLECTION)]

        assert pages[0][0].written_at == datetime(2026, 1, 1)

    async def test_write_time_is_never_earlier_than_updated_at(self, mock_client):
        """An event dated ahead of the indexing host's clock: ``indexed_at``
        may only keep a point longer than ``updated_at`` alone did."""
        payload = {
            "context_id": CTX,
            "resource_id": "docs",
            "doc_id": "d1",
            "version": 2,
            "updated_at": "2026-03-02T00:00:00Z",
            "indexed_at": "2026-03-01T12:30:00Z",
        }
        mock_client.scroll.return_value = ([SimpleNamespace(id="r1", payload=payload)], None)

        pages = [page async for page in scroll_point_refs(KAGURA_MEMORIES_COLLECTION)]

        assert pages[0][0].written_at == datetime(2026, 3, 2)

    async def test_out_of_range_timestamp_is_not_a_write_time(self, mock_client):
        """An offset that pushes the value out of range is unparsable, not a crash."""
        payload = {
            "context_id": CTX,
            "resource_id": "docs",
            "doc_id": "d1",
            "version": 2,
            "updated_at": "2026-01-01T00:00:00+00:00",
            "indexed_at": "0001-01-01T00:00:00+05:00",
        }
        mock_client.scroll.return_value = ([SimpleNamespace(id="r1", payload=payload)], None)

        pages = [page async for page in scroll_point_refs(KAGURA_MEMORIES_COLLECTION)]

        assert pages[0][0].written_at == datetime(2026, 1, 1)

    async def test_empty_collection_yields_nothing(self, mock_client):
        mock_client.scroll.return_value = ([], None)

        assert [page async for page in scroll_point_refs(KAGURA_MEMORIES_COLLECTION)] == []

    async def test_failure_wrapped(self, mock_client):
        mock_client.scroll.side_effect = RuntimeError("down")

        with pytest.raises(QdrantError, match="Failed to scroll"):
            _ = [page async for page in scroll_point_refs(KAGURA_MEMORIES_COLLECTION)]

    async def test_active_store_rows_are_paged(self, monkeypatch):
        store = AsyncMock()
        store.list_point_refs.return_value = [("a", CTX), ("b", None), ("c", CTX)]
        monkeypatch.setattr(qmod, "_active_store", lambda: store)

        pages = [page async for page in scroll_point_refs(KAGURA_MEMORIES_COLLECTION, page_size=2)]

        assert [len(page) for page in pages] == [2, 1]
        assert pages[0][0] == PointRef(point_id="a", context_id=CTX, is_resource=False)


class _Query:
    def __init__(self, rows):
        self._rows = rows
        self.columns = None
        self.limited = None

    def select(self, columns):
        self.columns = columns
        return self

    def limit(self, n):
        self.limited = n
        return self

    def to_list(self):
        return self._rows


class _Table:
    def __init__(self, rows):
        self._rows = rows
        self.query = _Query(rows)

    def count_rows(self, filter=None):  # noqa: A002 - lancedb API name
        return len(self._rows)

    def search(self):
        return self.query


def _lance_store(tables: dict) -> LanceVectorStore:
    store = LanceVectorStore.__new__(LanceVectorStore)
    store._lock = threading.RLock()
    store._open = lambda name, dim=0: tables.get(name)
    store._collection_names = lambda: list(tables)
    return store


class TestLanceStoreListing:
    async def test_list_collections(self):
        store = _lance_store({VARIANT: _Table([]), KAGURA_MEMORIES_COLLECTION: _Table([])})

        assert await store.list_collections() == [KAGURA_MEMORIES_COLLECTION, VARIANT]

    async def test_list_point_refs_reads_ids_only(self):
        table = _Table([{"id": "a", "context_id": CTX}, {"id": "b", "context_id": None}])
        store = _lance_store({KAGURA_MEMORIES_COLLECTION: table})

        refs = await store.list_point_refs(KAGURA_MEMORIES_COLLECTION)

        assert refs == [("a", CTX), ("b", None)]
        assert table.query.columns == ["id", "context_id"]
        assert table.query.limited == 2

    async def test_missing_or_empty_table_lists_nothing(self):
        store = _lance_store({KAGURA_MEMORIES_COLLECTION: _Table([])})

        assert await store.list_point_refs(KAGURA_MEMORIES_COLLECTION) == []
        assert await store.list_point_refs(VARIANT) == []


class TestBatchedDelete:
    async def test_lance_deletes_ids_in_chunks_not_one_statement_each(self):
        statements: list[str] = []
        table = SimpleNamespace(delete=statements.append)
        store = _lance_store({KAGURA_MEMORIES_COLLECTION: table})

        await store.delete_points([f"id-{n}" for n in range(501)], KAGURA_MEMORIES_COLLECTION)

        assert len(statements) == 2
        assert statements[0].startswith("id IN ('id-0', 'id-1'")
        assert statements[1] == "id IN ('id-500')"

    async def test_lance_quotes_the_ids(self):
        statements: list[str] = []
        store = _lance_store(
            {KAGURA_MEMORIES_COLLECTION: SimpleNamespace(delete=statements.append)}
        )

        await store.delete_points(["a'b"], KAGURA_MEMORIES_COLLECTION)

        assert statements == ["id IN ('a''b')"]

    async def test_delete_points_goes_to_the_active_store_in_one_call(self, monkeypatch):
        store = AsyncMock()
        monkeypatch.setattr(qmod, "_active_store", lambda: store)

        await qmod.delete_points_from_qdrant(["a", "b"], KAGURA_MEMORIES_COLLECTION)

        store.delete_points.assert_awaited_once_with(["a", "b"], KAGURA_MEMORIES_COLLECTION)


class TestDeleteEverywhereKeepsGoing:
    async def test_a_failing_collection_does_not_hide_the_one_that_holds_the_points(
        self, mock_client
    ):
        mock_client.get_collections.return_value = _collections(KAGURA_MEMORIES_COLLECTION, VARIANT)
        mock_client.count.side_effect = [RuntimeError("timeout"), SimpleNamespace(count=5)]

        with pytest.raises(QdrantError, match="1 collection"):
            await delete_context_points_everywhere(WS, CTX)

        swept = [call.kwargs["collection_name"] for call in mock_client.delete.await_args_list]
        assert swept == [VARIANT]

    async def test_a_caller_that_listed_the_collections_is_not_made_to_list_again(
        self, mock_client
    ):
        mock_client.count.return_value = SimpleNamespace(count=2)

        deleted = await delete_context_points_everywhere(WS, CTX, collections=[VARIANT])

        assert deleted == {VARIANT: 2}
        mock_client.get_collections.assert_not_awaited()
