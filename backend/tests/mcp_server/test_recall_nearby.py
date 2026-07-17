"""Tests for the MCP recall_nearby tool (#1331 WHERE axis).

Mirrors tests/mcp_server/test_recall_upcoming.py: mock-db handler tests that
inspect the emitted SQL shape (bbox + haversine + partial-index predicate) and
the structured validation errors. Real-PostgreSQL distance/ordering behavior
is covered by tests/integration/test_recall_nearby_e2e.py.
"""

import contextlib
import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from mcp_server.tools.memory import handle_recall_nearby


def _row(distance: float):
    m = MagicMock()
    m.id = uuid4()
    m.summary = f"memory at {distance}m"
    m.type = "note"
    m.details = {"location": {"lat": 35.68, "lon": 139.76}}
    return (m, distance)


def _mock_db_returning(rows):
    mock_db = AsyncMock()
    exec_result = MagicMock()
    exec_result.all.return_value = rows
    mock_db.execute = AsyncMock(return_value=exec_result)
    return mock_db


@contextlib.contextmanager
def _patched(mock_db):
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


def _args(**overrides):
    args = {"context_id": str(uuid4()), "lat": 35.68, "lon": 139.76}
    args.update(overrides)
    return args


@pytest.mark.asyncio
async def test_recall_nearby_returns_rows_with_distance():
    mock_db = _mock_db_returning([_row(12.5), _row(340.0)])
    with _patched(mock_db):
        result = await handle_recall_nearby(_args(), user_id="u1", workspace_id=None)
    payload = json.loads(result[0].text)
    assert payload["status"] == "success"
    assert [r["distance_m"] for r in payload["results"]] == [12.5, 340.0]
    assert all(
        set(r) == {"memory_id", "summary", "type", "details", "distance_m"}
        for r in payload["results"]
    )


@pytest.mark.asyncio
async def test_emitted_sql_repeats_partial_index_predicate_and_orders_by_distance():
    """The bbox query must literally include the partial index's predicate
    (location_lat IS NOT NULL AND deleted_at IS NULL) — gate1 EXPLAIN note —
    plus bbox BETWEENs and the haversine ORDER BY."""
    mock_db = _mock_db_returning([])
    with _patched(mock_db):
        await handle_recall_nearby(_args(), user_id="u1", workspace_id=None)
    sql = _emitted_sql(mock_db)
    assert "location_lat IS NOT NULL" in sql
    assert "deleted_at IS NULL" in sql
    assert sql.count("BETWEEN") >= 2  # lat window + at least one lon window
    assert "asin" in sql and "sqrt" in sql
    assert "ORDER BY distance_m" in sql
    assert "LIMIT" in sql


@pytest.mark.asyncio
async def test_antimeridian_emits_two_lon_ranges():
    mock_db = _mock_db_returning([])
    with _patched(mock_db):
        await handle_recall_nearby(
            _args(lat=0.0, lon=179.9999, radius_m=5000), user_id="u1", workspace_id=None
        )
    sql = _emitted_sql(mock_db)
    # Two lon BETWEEN windows ORed together (plus the lat window).
    assert sql.count("location_lon BETWEEN") == 2
    assert " OR " in sql


@pytest.mark.asyncio
async def test_missing_required_fields():
    result = await handle_recall_nearby({"context_id": str(uuid4())}, "u1", None)
    payload = json.loads(result[0].text)
    assert payload["error"] == "missing_fields"
    assert "lat" in payload["message"] and "lon" in payload["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["35.6", True, float("nan"), 91])
async def test_invalid_coordinates_are_validation_errors(bad):
    result = await handle_recall_nearby(_args(lat=bad), "u1", None)
    payload = json.loads(result[0].text)
    assert payload["error"] == "validation_error"


@pytest.mark.asyncio
async def test_invalid_coordinate_error_does_not_echo_value():
    # Privacy invariant #6: even rejected coordinates are location data —
    # the structured error names the rule, never the value.
    result = await handle_recall_nearby(_args(lat=89.123456, lon=200.5), "u1", None)
    payload = json.loads(result[0].text)
    assert payload["error"] == "validation_error"
    assert "200.5" not in payload["message"]
    assert "89.123456" not in payload["message"]


@pytest.mark.asyncio
async def test_invalid_k_and_radius_are_validation_errors():
    for args in (_args(k="many"), _args(radius_m="wide"), _args(radius_m=True)):
        result = await handle_recall_nearby(args, "u1", None)
        assert json.loads(result[0].text)["error"] == "validation_error"


@pytest.mark.asyncio
async def test_k_and_radius_are_clamped_into_sql():
    mock_db = _mock_db_returning([])
    with _patched(mock_db):
        await handle_recall_nearby(
            _args(k=99999, radius_m=99_999_999), user_id="u1", workspace_id=None
        )
    stmt = mock_db.execute.call_args.args[0]
    sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "LIMIT 100" in sql  # k clamp ceiling
    assert "1000000.0" in sql  # radius clamp ceiling (1000 km)


@pytest.mark.asyncio
async def test_happy_path_emits_no_raw_coordinates_to_stdout(capsys):
    # Privacy invariant #6 regression pin (#1324-style read-path check): the
    # handler + service must not log the query point.
    mock_db = _mock_db_returning([_row(5.0)])
    with _patched(mock_db):
        await handle_recall_nearby(
            _args(lat=35.6812345, lon=139.7671234), user_id="u1", workspace_id=None
        )
    captured = capsys.readouterr()
    assert "35.6812345" not in captured.out + captured.err
    assert "139.7671234" not in captured.out + captured.err
