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

    collection_stub = MagicMock()
    collection_stub.name = "kagura_memories"
    client = AsyncMock()
    client.get_collections.return_value = MagicMock(collections=[collection_stub])
    p1, p2 = MagicMock(), MagicMock()
    p1.id = str(located_id)
    p2.id = str(stale_id)
    client.scroll = AsyncMock(return_value=([p1, p2], None))

    db = MagicMock()
    pg_result = MagicMock()
    pg_result.scalars.return_value = [located_id]
    db.execute = AsyncMock(return_value=pg_result)

    with (
        patch("db.qdrant.get_qdrant_client", return_value=client),
        patch("db.vector_store.get_active_store", return_value=None),
        patch("db.qdrant.update_memory_payload_in_qdrant", new=AsyncMock()) as update_mock,
    ):
        cleared = await reconcile_stale_location_payloads(db)

    assert cleared == 1
    kwargs = update_mock.await_args.kwargs
    assert kwargs["memory_id"] == UUID(str(stale_id))
    assert kwargs["delete_keys"] == ["location"]
