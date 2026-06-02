"""Tests for the MCP recall_upcoming tool (Issue #877).

Follows the mock-db handler convention of the other tests/mcp_server/ suites
(patch db.base.get_db + the context resolver, then inspect the emitted SQL and
the response). The actual lexical window-overlap + sort behavior against
PostgreSQL is covered by the migration round-trip test and the /memory/list
SQL-shape test, which exercise the identical predicate and ORDER BY.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from mcp_server.tools.memory import handle_recall_upcoming


def _row(month: int):
    m = MagicMock()
    m.id = uuid4()
    m.summary = f"event in month {month}"
    m.type = "time"
    m.details = {"trigger": {"year": 2026, "month": month}}
    return m


def _mock_db_returning(rows):
    mock_db = AsyncMock()
    exec_result = MagicMock()
    scalars = MagicMock()
    scalars.all.return_value = rows
    exec_result.scalars.return_value = scalars
    mock_db.execute = AsyncMock(return_value=exec_result)
    return mock_db


def _emitted_sql(mock_db) -> str:
    stmt = mock_db.execute.call_args.args[0]
    return str(stmt.compile(compile_kwargs={"literal_binds": False}))


@pytest.mark.asyncio
async def test_recall_upcoming_returns_overlapping_time_memories():
    """Happy path: returns type='time' rows, and the emitted query filters on
    the window and sorts by trigger_from ascending (soonest first)."""
    rows = [_row(7), _row(9)]  # DB returns them in trigger_from asc order
    mock_db = _mock_db_returning(rows)
    mock_ctx = MagicMock()

    async def mock_get_db():
        yield mock_db

    with (
        patch("db.base.get_db", new=mock_get_db),
        patch(
            "mcp_server.tools.memory._resolve_context_for_read",
            new=AsyncMock(return_value=mock_ctx),
        ),
        patch("mcp_server.tools.memory._context_response_fields", return_value={}),
    ):
        result = await handle_recall_upcoming(
            {
                "context_id": str(uuid4()),
                "from": "2026-06-01T00:00:00",
                "until": "2026-12-31T23:59:59",
            },
            user_id="u1",
            workspace_id=None,
        )

    payload = json.loads(result[0].text)
    assert payload["status"] == "success"
    months = [m["details"]["trigger"]["month"] for m in payload["results"]]
    assert months == [7, 9]

    sql = _emitted_sql(mock_db)
    assert "memories.type =" in sql
    assert "memories.trigger_until >=" in sql  # lower-bound overlap
    assert "memories.trigger_from <=" in sql  # upper-bound overlap
    assert "ORDER BY memories.trigger_from ASC" in sql


@pytest.mark.asyncio
async def test_recall_upcoming_open_ended_window_has_no_upper_bound():
    """Omitting 'until' leaves a lower-bound-only (open-ended future) window."""
    mock_db = _mock_db_returning([_row(7)])
    mock_ctx = MagicMock()

    async def mock_get_db():
        yield mock_db

    with (
        patch("db.base.get_db", new=mock_get_db),
        patch(
            "mcp_server.tools.memory._resolve_context_for_read",
            new=AsyncMock(return_value=mock_ctx),
        ),
        patch("mcp_server.tools.memory._context_response_fields", return_value={}),
    ):
        result = await handle_recall_upcoming(
            {"context_id": str(uuid4()), "from": "2026-06-01T00:00:00"},
            user_id="u1",
            workspace_id=None,
        )

    assert json.loads(result[0].text)["status"] == "success"
    sql = _emitted_sql(mock_db)
    assert "memories.trigger_until >=" in sql
    assert "memories.trigger_from <=" not in sql


@pytest.mark.asyncio
async def test_recall_upcoming_requires_context_id():
    result = await handle_recall_upcoming({}, user_id="u1", workspace_id=None)
    payload = json.loads(result[0].text)
    assert payload["status"] == "error"
