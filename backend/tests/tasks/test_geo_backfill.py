"""Startup geo payload backfill (#1332)."""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from tasks.geo_backfill import backfill_location_payloads


def _row(lat: float, lon: float):
    row = MagicMock()
    row.id = uuid4()
    row.context_id = uuid4()
    row.location_lat = lat
    row.location_lon = lon
    row.summary_embedding_id = None
    return row


@pytest.mark.asyncio
async def test_backfill_writes_location_payload_per_located_row():
    rows = [_row(35.68, 139.76), _row(-33.0, -70.65)]
    db = MagicMock()
    result = MagicMock()
    result.all.return_value = rows
    db.execute = AsyncMock(return_value=result)

    with (
        patch(
            "services.context_routing.resolve_collection_name",
            new=AsyncMock(return_value="kagura_memories"),
        ),
        patch("db.qdrant.update_memory_payload_in_qdrant", new=AsyncMock()) as mock_update,
    ):
        count = await backfill_location_payloads(db)

    assert count == 2
    assert mock_update.await_count == 2
    first = mock_update.await_args_list[0].kwargs
    assert first["payload_updates"]["location"] == {"lat": 35.68, "lon": 139.76}
    assert first["memory_id"] == rows[0].id
    # #2015: chunk points copy location when they are written; no cascade.
    assert all(c.kwargs["cascade_chunks"] is False for c in mock_update.await_args_list)


@pytest.mark.asyncio
async def test_backfill_noop_when_no_located_rows():
    db = MagicMock()
    result = MagicMock()
    result.all.return_value = []
    db.execute = AsyncMock(return_value=result)

    with patch("db.qdrant.update_memory_payload_in_qdrant", new=AsyncMock()) as mock_update:
        count = await backfill_location_payloads(db)

    assert count == 0
    mock_update.assert_not_awaited()


@pytest.mark.asyncio
async def test_backfill_isolates_row_failures_and_memoizes_collections():
    rows = [_row(35.68, 139.76), _row(1.0, 2.0), _row(-33.0, -70.65)]
    rows[1].context_id = rows[0].context_id  # same context → one resolution
    db = MagicMock()
    result = MagicMock()
    result.all.return_value = rows
    db.execute = AsyncMock(return_value=result)

    update_mock = AsyncMock(side_effect=[None, RuntimeError("qdrant blip"), None])
    resolve_mock = AsyncMock(return_value="kagura_memories")
    with (
        patch("services.context_routing.resolve_collection_name", new=resolve_mock),
        patch("db.qdrant.update_memory_payload_in_qdrant", new=update_mock),
    ):
        count = await backfill_location_payloads(db)

    # Row 2 failed but rows 1 and 3 were still written (per-row isolation).
    assert count == 2
    assert update_mock.await_count == 3
    # Two distinct contexts → exactly two collection resolutions (memoized).
    assert resolve_mock.await_count == 2


@pytest.mark.asyncio
async def test_reconcile_clears_stale_location_payload():
    """A point still carrying `location` whose PG row is no longer located
    (swallowed delete_payload failure, #439 patch path) gets cleared."""
    from uuid import UUID

    from tasks.geo_backfill import reconcile_stale_location_payloads

    located_id = uuid4()
    stale_id = uuid4()
    located_mock = AsyncMock(return_value={located_id})

    collection_stub = MagicMock()
    collection_stub.name = "kagura_memories"
    client = AsyncMock()
    client.get_collections.return_value = MagicMock(collections=[collection_stub])
    p1, p2 = MagicMock(), MagicMock()
    p1.id = str(located_id)
    p2.id = str(stale_id)
    client.scroll = AsyncMock(return_value=([p1, p2], None))

    db = MagicMock()

    with (
        patch("db.qdrant.get_qdrant_client", return_value=client),
        patch("db.vector_store.get_active_store", return_value=None),
        patch("db.qdrant.update_memory_payload_in_qdrant", new=AsyncMock()) as update_mock,
        patch("tasks.geo_backfill.located_point_ids", new=located_mock),
        patch("services.content_chunk_writer.chunk_cascade_needed", new=AsyncMock()),
    ):
        cleared = await reconcile_stale_location_payloads(db)

    assert cleared == 1
    located_mock.assert_awaited_once_with(db, [located_id, stale_id])
    kwargs = update_mock.await_args.kwargs
    assert kwargs["memory_id"] == UUID(str(stale_id))
    assert kwargs["delete_keys"] == ["location"]


@pytest.mark.asyncio
async def test_reconcile_scrolls_summary_points_only():
    """#2015: chunk points share their memory's location and are cleared by
    the summary point's payload cascade, so the scroll leaves them out."""
    from qdrant_client.models import IsEmptyCondition

    from tasks.geo_backfill import reconcile_stale_location_payloads

    collection_stub = MagicMock()
    collection_stub.name = "kagura_memories"
    client = AsyncMock()
    client.get_collections.return_value = MagicMock(collections=[collection_stub])
    client.scroll = AsyncMock(return_value=([], None))

    with (
        patch("db.qdrant.get_qdrant_client", return_value=client),
        patch("db.vector_store.get_active_store", return_value=None),
    ):
        await reconcile_stale_location_payloads(MagicMock())

    flt = client.scroll.await_args.kwargs["scroll_filter"]
    assert any(
        isinstance(c, IsEmptyCondition) and c.is_empty.key == "chunk_of" for c in flt.must or []
    )


@pytest.mark.asyncio
async def test_reconcile_cascades_only_for_contexts_with_chunks():
    """#2015: on a deployment that never opted in, the reconcile makes no chunk
    calls — the cascade is gated on the point's context flag."""
    from types import SimpleNamespace

    from tasks.geo_backfill import reconcile_stale_location_payloads

    flagged_ctx, plain_ctx = str(uuid4()), str(uuid4())
    p1 = SimpleNamespace(id=str(uuid4()), payload={"context_id": flagged_ctx})
    p2 = SimpleNamespace(id=str(uuid4()), payload={"context_id": plain_ctx})
    collection_stub = MagicMock()
    collection_stub.name = "kagura_memories"
    client = AsyncMock()
    client.get_collections.return_value = MagicMock(collections=[collection_stub])
    client.scroll = AsyncMock(return_value=([p1, p2], None))
    db = MagicMock()

    async def _needed(_db, context_id):
        return str(context_id) == flagged_ctx

    with (
        patch("db.qdrant.get_qdrant_client", return_value=client),
        patch("db.vector_store.get_active_store", return_value=None),
        patch("db.qdrant.update_memory_payload_in_qdrant", new=AsyncMock()) as update_mock,
        patch("services.content_chunk_writer.chunk_cascade_needed", new=_needed),
        patch("tasks.geo_backfill.located_point_ids", new=AsyncMock(return_value=set())),
    ):
        await reconcile_stale_location_payloads(db)

    by_id = {c.kwargs["memory_id"]: c.kwargs["cascade_chunks"] for c in update_mock.await_args_list}
    from uuid import UUID

    assert by_id == {UUID(p1.id): True, UUID(p2.id): False}
    assert client.scroll.await_args.kwargs["with_payload"] == ["context_id"]


def _backfill_db(rows):
    db = MagicMock()
    result = MagicMock()
    result.all.return_value = rows
    db.execute = AsyncMock(return_value=result)
    return db


@pytest.mark.asyncio
async def test_backfill_writes_to_the_resource_rows_document_point():
    """#2218: an indexer row names its document's uuid5 point, not its own id."""
    from services.resource_indexer import resource_point_id

    row = _row(35.68, 139.76)
    row.summary_embedding_id = resource_point_id("reports", "doc-1", 2)
    with (
        patch(
            "services.context_routing.resolve_collection_name",
            new=AsyncMock(return_value="kagura_memories"),
        ),
        patch("db.qdrant.update_memory_payload_in_qdrant", new=AsyncMock()) as mock_update,
        patch(
            "services.resource_indexer.unshared_point_id",
            new=AsyncMock(side_effect=lambda _db, _mid, pid: pid),
        ) as unshared,
    ):
        count = await backfill_location_payloads(_backfill_db([row]))

    assert count == 1
    unshared.assert_awaited_once()
    assert unshared.await_args.args[1:] == (row.id, row.summary_embedding_id)
    assert mock_update.await_args.kwargs["memory_id"] == row.summary_embedding_id


@pytest.mark.asyncio
async def test_backfill_writes_an_api_row_to_its_own_id():
    row = _row(35.68, 139.76)
    row.summary_embedding_id = row.id
    with (
        patch(
            "services.context_routing.resolve_collection_name",
            new=AsyncMock(return_value="kagura_memories"),
        ),
        patch("db.qdrant.update_memory_payload_in_qdrant", new=AsyncMock()) as mock_update,
    ):
        count = await backfill_location_payloads(_backfill_db([row]))

    assert count == 1
    assert mock_update.await_args.kwargs["memory_id"] == row.id


@pytest.mark.asyncio
async def test_backfill_skips_a_shared_point():
    """A document point another live row names too is left as it is."""
    row = _row(35.68, 139.76)
    row.summary_embedding_id = uuid4()
    with (
        patch(
            "services.context_routing.resolve_collection_name",
            new=AsyncMock(return_value="kagura_memories"),
        ),
        patch("db.qdrant.update_memory_payload_in_qdrant", new=AsyncMock()) as mock_update,
        patch("services.resource_indexer.unshared_point_id", new=AsyncMock(return_value=None)),
    ):
        count = await backfill_location_payloads(_backfill_db([row]))

    assert count == 0
    mock_update.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_keeps_a_located_resource_point():
    """#2218: a uuid5 resource point is never a Memory.id; the reconcile must
    not take its location for stale while its row is located."""
    from services.resource_indexer import resource_point_id
    from tasks.geo_backfill import reconcile_stale_location_payloads

    doc_point = resource_point_id("reports", "doc-1", 2)
    collection_stub = MagicMock()
    collection_stub.name = "kagura_memories"
    client = AsyncMock()
    client.get_collections.return_value = MagicMock(collections=[collection_stub])
    point = MagicMock()
    point.id = str(doc_point)
    client.scroll = AsyncMock(return_value=([point], None))

    with (
        patch("db.qdrant.get_qdrant_client", return_value=client),
        patch("db.vector_store.get_active_store", return_value=None),
        patch("db.qdrant.update_memory_payload_in_qdrant", new=AsyncMock()) as update_mock,
        patch("tasks.geo_backfill.located_point_ids", new=AsyncMock(return_value={doc_point})),
    ):
        cleared = await reconcile_stale_location_payloads(MagicMock())

    assert cleared == 0
    update_mock.assert_not_awaited()


class TestLocatedPointIds:
    """#2218, against the real DB: a located row is found by either id."""

    @pytest.mark.asyncio
    async def test_located_point_ids_finds_rows_by_either_id(self, db_session):
        from models.auth import Context, Workspace
        from models.memory import Memory
        from services.resource_indexer import resource_point_id
        from tasks.geo_backfill import located_point_ids
        from utils.datetime import utcnow

        owner = f"owner-{uuid4().hex[:8]}"
        ws = Workspace(id=uuid4(), name=f"ws-{uuid4().hex[:8]}", owner_user_id=owner)
        ctx = Context(
            id=uuid4(),
            workspace_id=ws.id,
            name=f"ctx-{uuid4().hex[:8]}",
            created_by=owner,
            is_private=False,
        )
        db_session.add(ws)
        await db_session.flush()
        db_session.add(ctx)
        await db_session.flush()
        location = {"location": {"lat": 35.5, "lon": 139.25}}

        def _memory(point_id=None, *, located=True, deleted=False, doc="doc-1"):
            memory_id = uuid4()
            details = {"resource_id": "reports", "doc_id": doc, "version": 1}
            return Memory(
                id=memory_id,
                user_id=owner,
                workspace_id=ws.id,
                context_id=ctx.id,
                summary="s",
                content="{}",
                type="resource_data",
                client="pytest",
                details={**details, **(location if located else {})},
                summary_embedding_id=point_id or memory_id,
                deleted_at=utcnow() if deleted else None,
            )

        api_row = _memory()
        indexer_row = _memory(resource_point_id("reports", "doc-1", 1))
        unlocated = _memory(resource_point_id("reports", "doc-2", 1), located=False, doc="doc-2")
        deleted = _memory(resource_point_id("reports", "doc-3", 1), deleted=True, doc="doc-3")
        rows = [api_row, indexer_row, unlocated, deleted]
        db_session.add_all(rows)
        await db_session.flush()

        found = await located_point_ids(db_session, [m.summary_embedding_id for m in rows])

        assert found == {api_row.id, indexer_row.summary_embedding_id}
        assert await located_point_ids(db_session, []) == set()
