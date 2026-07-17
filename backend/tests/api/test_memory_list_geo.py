"""Tests for the WHERE-axis bbox filter + location field on GET /memory/list (#1334).

Follows the direct-call + mock-db + compiled-SQL-shape convention of
test_memory_list.py / test_memory_list_time.py: the endpoint is unit-tested by
inspecting the emitted SQL (WHERE predicates), not via an HTTP round-trip. The
generated columns' actual population behavior against PostgreSQL is covered by
tests/integration/test_location_cols_migration.py.
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from api.routes.memory import list_memories

MOCK_USER = {"user_id": "test_user_123"}


def _mock_memory_row(lat: float | None = None, lon: float | None = None):
    mem = MagicMock()
    mem.id = uuid4()
    mem.summary = "Test memory"
    mem.type = "note"
    mem.scope = "persistent"
    mem.importance = 0.8
    mem.created_at = datetime(2026, 4, 1)
    mem.updated_at = datetime(2026, 4, 2)
    mem.location_lat = lat
    mem.location_lon = lon
    return mem


def _db_with_rows(total: int = 0, rows: list | None = None):
    mock_db = AsyncMock()
    count_result = MagicMock()
    count_result.scalar.return_value = total
    rows_result = MagicMock()
    scalars = MagicMock()
    scalars.all.return_value = rows or []
    rows_result.scalars.return_value = scalars
    mock_db.execute.side_effect = [count_result, rows_result]
    return mock_db


def _where_sql(mock_db, call_index: int) -> str:
    stmt = mock_db.execute.call_args_list[call_index].args[0]
    return str(stmt.whereclause.compile(compile_kwargs={"literal_binds": False}))


@pytest.mark.asyncio
async def test_bbox_predicates_applied_to_both_queries():
    """All four bbox bounds add range predicates on the generated columns to
    BOTH the count and data queries (anti-drift: same rationale as tag_filter
    and the time-axis window_filters)."""
    mock_db = _db_with_rows()
    await list_memories(
        user=MOCK_USER,
        db=mock_db,
        scope=None,
        type=None,
        context_id=None,
        q=None,
        tags=None,
        lat_min=-10.0,
        lat_max=10.0,
        lon_min=100.0,
        lon_max=120.0,
        limit=50,
        offset=0,
    )
    for call_index in (0, 1):  # 0 = count query, 1 = data query
        sql = _where_sql(mock_db, call_index)
        assert "memories.location_lat >=" in sql, sql
        assert "memories.location_lat <=" in sql, sql
        assert "memories.location_lon >=" in sql, sql
        assert "memories.location_lon <=" in sql, sql


@pytest.mark.asyncio
async def test_one_sided_bound_applied_independently():
    """Each bound applies independently (time-axis mirror): lat_min alone adds
    only the lat lower-bound predicate — no lat upper bound, no lon bounds."""
    mock_db = _db_with_rows()
    await list_memories(
        user=MOCK_USER,
        db=mock_db,
        scope=None,
        type=None,
        context_id=None,
        q=None,
        tags=None,
        lat_min=35.0,
        limit=50,
        offset=0,
    )
    for call_index in (0, 1):
        sql = _where_sql(mock_db, call_index)
        assert "memories.location_lat >=" in sql, sql
        assert "memories.location_lat <=" not in sql, sql
        assert "location_lon" not in sql, sql


@pytest.mark.asyncio
async def test_no_geo_params_no_predicates():
    """Without bbox params the queries carry no location predicate (backward
    compatible — rows without location stay visible)."""
    mock_db = _db_with_rows()
    await list_memories(
        user=MOCK_USER,
        db=mock_db,
        scope=None,
        type=None,
        context_id=None,
        q=None,
        tags=None,
        limit=50,
        offset=0,
    )
    for call_index in (0, 1):
        assert "location_" not in _where_sql(mock_db, call_index)


@pytest.mark.asyncio
async def test_location_included_in_response_items():
    """Rows expose the generated columns as a nullable ``location`` object so
    the UI can plot pins; rows without coordinates serialize location=None."""
    located = _mock_memory_row(lat=35.6812345, lon=139.7671234)
    bare = _mock_memory_row()
    mock_db = _db_with_rows(total=2, rows=[located, bare])
    response = await list_memories(
        user=MOCK_USER,
        db=mock_db,
        scope=None,
        type=None,
        context_id=None,
        q=None,
        tags=None,
        limit=50,
        offset=0,
    )
    assert response.memories[0].location is not None
    assert response.memories[0].location.lat == pytest.approx(35.6812345)
    assert response.memories[0].location.lon == pytest.approx(139.7671234)
    assert response.memories[1].location is None


@pytest.mark.asyncio
async def test_location_omitted_when_only_one_coordinate_present():
    """A half-populated pair (raw-SQL writer artifact: one coordinate NULLed by
    the e69 regex guard) must not serialize a partial location object."""
    half = _mock_memory_row(lat=35.6812345, lon=None)
    mock_db = _db_with_rows(total=1, rows=[half])
    response = await list_memories(
        user=MOCK_USER,
        db=mock_db,
        scope=None,
        type=None,
        context_id=None,
        q=None,
        tags=None,
        limit=50,
        offset=0,
    )
    assert response.memories[0].location is None
