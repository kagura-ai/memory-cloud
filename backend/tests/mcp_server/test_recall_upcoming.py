"""Tests for the MCP recall_upcoming tool (Issue #877).

Follows the mock-db handler convention of the other tests/mcp_server/ suites
(patch db.base.get_db + the context resolver, then inspect the emitted SQL and
the response). The actual lexical window-overlap + sort behavior against
PostgreSQL is covered by the migration round-trip test and the /memory/list
SQL-shape test, which exercise the identical predicate and ORDER BY.
"""

import contextlib
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


@contextlib.contextmanager
def _patched(mock_db):
    """Patch the handler's DB session, read-path context resolver, response
    fields, and usage logger so the test exercises pure handler logic."""

    async def mock_get_db():
        yield mock_db

    with (
        patch("db.base.get_db", new=mock_get_db),
        patch(
            "mcp_server.tools.memory._resolve_context_for_read",
            new=AsyncMock(return_value=MagicMock()),
        ),
        patch("mcp_server.tools.memory._context_response_fields", return_value={}),
        patch("mcp_server.tools.memory._log_tool_usage", new=AsyncMock()),
    ):
        yield


def _emitted_sql(mock_db) -> str:
    stmt = mock_db.execute.call_args.args[0]
    return str(stmt.compile(compile_kwargs={"literal_binds": False}))


@pytest.mark.asyncio
async def test_recall_upcoming_returns_overlapping_time_memories():
    """Happy path: returns type='time' rows, and the emitted query filters on
    the window and sorts by trigger_from ascending (soonest first)."""
    rows = [_row(7), _row(9)]  # DB returns them in trigger_from asc order
    mock_db = _mock_db_returning(rows)

    with _patched(mock_db):
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
    # #1599: the default item carries the trigger itself, not the whole details.
    months = [m["trigger"]["month"] for m in payload["results"]]
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

    with _patched(mock_db):
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
async def test_recall_upcoming_now_resolves_to_iso_bound():
    """from='now' is resolved server-side to a fixed-width ISO lower bound, not
    passed through literally (which would match nothing lexically)."""
    mock_db = _mock_db_returning([])

    with _patched(mock_db):
        result = await handle_recall_upcoming(
            {"context_id": str(uuid4()), "from": "now"},
            user_id="u1",
            workspace_id=None,
        )

    assert json.loads(result[0].text)["status"] == "success"
    sql = str(mock_db.execute.call_args.args[0].compile(compile_kwargs={"literal_binds": True}))
    assert "'now'" not in sql  # the literal 'now' must not reach the query
    assert "memories.trigger_until >=" in sql


@pytest.mark.asyncio
@pytest.mark.parametrize("k,expected_limit", [(0, 1), (-5, 1), (200, 100), (20, 20)])
async def test_recall_upcoming_clamps_k(k, expected_limit):
    """k is clamped into [1, 100]: 0/negative no longer produce LIMIT 0 / no-cap."""
    mock_db = _mock_db_returning([])

    with _patched(mock_db):
        await handle_recall_upcoming(
            {"context_id": str(uuid4()), "k": k},
            user_id="u1",
            workspace_id=None,
        )

    # literal_binds so the LIMIT value renders inline rather than as a bind param.
    sql = str(mock_db.execute.call_args.args[0].compile(compile_kwargs={"literal_binds": True}))
    assert f"LIMIT {expected_limit}" in sql


@pytest.mark.asyncio
async def test_recall_upcoming_non_integer_k_is_validation_error():
    """A non-numeric k returns a structured validation_error, not an unhandled
    ValueError crash."""
    result = await handle_recall_upcoming(
        {"context_id": str(uuid4()), "k": "abc"}, user_id="u1", workspace_id=None
    )
    payload = json.loads(result[0].text)
    assert payload["status"] == "error"
    assert payload["error"] == "validation_error"


@pytest.mark.asyncio
async def test_recall_upcoming_malformed_bound_is_validation_error():
    """A non-ISO 'from'/'until' returns a structured validation_error rather than
    silently producing wrong lexical-comparison results."""
    result = await handle_recall_upcoming(
        {"context_id": str(uuid4()), "from": "2026-7-1"}, user_id="u1", workspace_id=None
    )
    payload = json.loads(result[0].text)
    assert payload["status"] == "error"


@pytest.mark.asyncio
async def test_recall_upcoming_requires_context_id():
    result = await handle_recall_upcoming({}, user_id="u1", workspace_id=None)
    payload = json.loads(result[0].text)
    assert payload["status"] == "error"


# ============================================================================
# #1599: default items are {memory_id, summary, type, trigger}
# ============================================================================


def _row_with_notes(month: int):
    """A Time Memory whose details carry more than the trigger — the payload
    the default response no longer ships."""
    m = _row(month)
    m.summary = f"{month}月のリリース判定会議"
    m.details = {
        "trigger": {"year": 2026, "month": month},
        "agenda": ["進捗確認", "リスクの洗い出し"],
        "notes": "長いメモ " * 50,
    }
    return m


@pytest.mark.asyncio
async def test_recall_upcoming_default_items_carry_trigger_not_details():
    rows = [_row_with_notes(7)]
    mock_db = _mock_db_returning(rows)

    with _patched(mock_db):
        result = await handle_recall_upcoming(
            {"context_id": str(uuid4()), "from": "2026-06-01T00:00:00"},
            user_id="u1",
            workspace_id=None,
        )

    text = result[0].text
    (item,) = json.loads(text)["results"]
    assert item == {
        "memory_id": str(rows[0].id),
        "summary": "7月のリリース判定会議",
        "type": "time",
        "trigger": {"year": 2026, "month": 7},
    }
    assert "長いメモ" not in text
    assert "\\u" not in text  # UTF-8 as-is, like every other tool result


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", [False, None])
async def test_recall_upcoming_falsy_include_details_is_the_default_shape(flag):
    mock_db = _mock_db_returning([_row_with_notes(7)])

    with _patched(mock_db):
        result = await handle_recall_upcoming(
            {"context_id": str(uuid4()), "include_details": flag},
            user_id="u1",
            workspace_id=None,
        )

    (item,) = json.loads(result[0].text)["results"]
    assert "details" not in item
    assert item["trigger"] == {"year": 2026, "month": 7}


@pytest.mark.asyncio
async def test_recall_upcoming_include_details_restores_the_full_shape():
    """include_details=true is the pre-#1599 item, byte for byte: the full
    details and no separate trigger key (it is already inside details)."""
    rows = [_row_with_notes(9)]
    mock_db = _mock_db_returning(rows)

    with _patched(mock_db):
        result = await handle_recall_upcoming(
            {"context_id": str(uuid4()), "include_details": True},
            user_id="u1",
            workspace_id=None,
        )

    (item,) = json.loads(result[0].text)["results"]
    assert item == {
        "memory_id": str(rows[0].id),
        "summary": "9月のリリース判定会議",
        "type": "time",
        "details": rows[0].details,
    }
    assert "trigger" not in item


@pytest.mark.asyncio
@pytest.mark.parametrize("details", [None, {}, {"note": "no trigger key"}, "legacy string"])
async def test_recall_upcoming_missing_trigger_is_null_not_a_crash(details):
    """The query filters on the generated trigger columns, so a row without a
    trigger should not come back — but a legacy / hand-written details value
    must degrade to ``trigger: null`` rather than crash the read."""
    row = _row(7)
    row.details = details
    mock_db = _mock_db_returning([row])

    with _patched(mock_db):
        result = await handle_recall_upcoming(
            {"context_id": str(uuid4())}, user_id="u1", workspace_id=None
        )

    (item,) = json.loads(result[0].text)["results"]
    assert item["trigger"] is None
    assert "details" not in item


def test_recall_upcoming_definition_declares_include_details():
    from mcp_server.tools._definitions import get_tool_definitions

    tool = next(t for t in get_tool_definitions() if t["name"] == "recall_upcoming")
    prop = tool["inputSchema"]["properties"]["include_details"]
    assert prop["type"] == "boolean"
    assert "default" in prop["description"].lower()

    returns = tool["description"].split("Returns:", 1)[1]
    assert "trigger" in returns
    assert "include_details" in returns


def test_recall_upcoming_include_details_string_is_coerced_at_dispatch():
    """Some clients send booleans as strings; declaring the param as a boolean
    is what makes the dispatch-level coercion apply to it."""
    from mcp_server.tools._arg_coercion import coerce_mcp_arguments

    coerced = coerce_mcp_arguments("recall_upcoming", {"include_details": "true"})
    assert coerced["include_details"] is True
