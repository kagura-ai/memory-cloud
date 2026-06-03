"""Tests for the Time Memory window filter + sort on GET /memory/list (#877).

Follows the direct-call + mock-db + compiled-SQL-shape convention of
test_memory_list.py: this endpoint is unit-tested by inspecting the emitted SQL
(WHERE predicates + ORDER BY), not via an HTTP round-trip. The actual lexical
window-overlap behavior against PostgreSQL is covered by the migration
round-trip test (TEXT columns) + the recall_upcoming integration test.
"""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from api.routes.memory import list_memories, patch_memory
from models.schemas import PatchMemoryRequest

MOCK_USER = {"user_id": "test_user_123"}


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


def _full_sql(mock_db, call_index: int) -> str:
    stmt = mock_db.execute.call_args_list[call_index].args[0]
    return str(stmt.compile(compile_kwargs={"literal_binds": False}))


@pytest.mark.asyncio
async def test_window_predicates_applied_to_both_queries():
    """trigger_from/trigger_until add the overlap predicate to BOTH the count
    and data queries: trigger_until >= qfrom AND trigger_from <= quntil."""
    mock_db = _db_with_rows()
    await list_memories(
        user=MOCK_USER,
        db=mock_db,
        scope=None,
        type="time",
        context_id=None,
        q=None,
        tags=None,
        trigger_from="2026-06-01T00:00:00",
        trigger_until="2026-12-31T23:59:59",
        order_by="trigger_from",
        limit=50,
        offset=0,
    )
    for call_index in (0, 1):  # 0 = count query, 1 = data query
        sql = _where_sql(mock_db, call_index)
        assert "memories.trigger_until >=" in sql, sql
        assert "memories.trigger_from <=" in sql, sql


@pytest.mark.asyncio
async def test_order_by_trigger_from_and_open_ended_window():
    """order_by='trigger_from' sorts ascending; omitting trigger_until leaves an
    open-ended (lower-bound-only) window."""
    mock_db = _db_with_rows()
    await list_memories(
        user=MOCK_USER,
        db=mock_db,
        scope=None,
        type="time",
        context_id=None,
        q=None,
        tags=None,
        trigger_from="2026-06-01T00:00:00",
        trigger_until=None,
        order_by="trigger_from",
        limit=50,
        offset=0,
    )
    assert "ORDER BY memories.trigger_from ASC" in _full_sql(mock_db, 1)
    where = _where_sql(mock_db, 1)
    assert "memories.trigger_until >=" in where
    assert "memories.trigger_from <=" not in where  # no upper bound


@pytest.mark.asyncio
async def test_default_order_and_no_window_when_params_omitted():
    """Without trigger params the endpoint keeps its legacy created_at desc sort
    and adds no window predicate (backward compatible)."""
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
    assert "ORDER BY memories.created_at DESC" in _full_sql(mock_db, 1)
    assert "trigger_from" not in _where_sql(mock_db, 1)


@pytest.mark.asyncio
async def test_now_bound_resolved_not_passed_literally():
    """trigger_from='now' is resolved to a fixed-width ISO bound, not compared
    literally (which would match nothing)."""
    mock_db = _db_with_rows()
    await list_memories(
        user=MOCK_USER,
        db=mock_db,
        scope=None,
        type="time",
        context_id=None,
        q=None,
        tags=None,
        trigger_from="now",
        order_by="trigger_from",
        limit=50,
        offset=0,
    )
    data_sql = str(
        mock_db.execute.call_args_list[1]
        .args[0]
        .whereclause.compile(compile_kwargs={"literal_binds": True})
    )
    assert "'now'" not in data_sql  # literal must not reach the query
    assert "memories.trigger_until >=" in _where_sql(mock_db, 1)


@pytest.mark.asyncio
async def test_malformed_trigger_bound_returns_422():
    """A non-zero-padded / non-ISO bound is a 422, not silent wrong results."""
    mock_db = _db_with_rows()
    with pytest.raises(HTTPException) as exc:
        await list_memories(
            user=MOCK_USER,
            db=mock_db,
            scope=None,
            type="time",
            context_id=None,
            q=None,
            tags=None,
            trigger_from="2026-7-1",
            order_by="trigger_from",
            limit=50,
            offset=0,
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_patch_route_maps_time_trigger_value_error_to_422():
    """A PATCH that flips type to 'time' with an invalid trigger raises ValueError
    from MemoryService._apply_time_trigger; the route must map it to 422 (same as
    the remember route), not an unhandled 500."""
    svc = MagicMock()
    svc.patch_memory = AsyncMock(
        side_effect=ValueError("invalid details.trigger: trigger.month out of range (1-12)")
    )
    req = PatchMemoryRequest(type="time", details={"trigger": {"year": 2026, "month": 13}})
    with pytest.raises(HTTPException) as exc:
        await patch_memory(
            memory_id=uuid4(),
            request=req,
            user=MOCK_USER,
            memory_service=svc,
        )
    assert exc.value.status_code == 422
