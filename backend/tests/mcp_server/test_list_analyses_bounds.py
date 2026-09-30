"""#1750: list_analyses previews each run's error and holds its page to the budget.

The analysis writer stores up to 8,000 characters of ``error`` per run and a
page holds up to 100 runs, so a page of failed runs returned whole could reach
~165k characters. The list view now carries a 300-character preview
(``error_truncated: true`` when cut, the #1743 ``list_agents`` convention) and
stops at the shared 20,000-character default budget, handing back a cursor that
resumes after the last run returned. ``get_analysis`` keeps the full text.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from mcp_server.tools.analysis import handle_get_analysis, handle_list_analyses
from services.analysis.query_service import _decode_list_cursor
from utils.response_budget import DEFAULT_MAX_CHARS

WRITER_ERROR_CAP = 8_000  # services/analysis/reporter.py truncates error here
PREVIEW = 300


def _fake_get_db(db_mock):
    async def _gen():
        yield db_mock

    return _gen


def _run(index: int, error: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        workspace_id=uuid4(),
        context_id=uuid4(),
        status="failed" if error else "succeeded",
        triggered_by="user-1",
        started_at=datetime(2026, 9, 1) - timedelta(minutes=index),
        finished_at=datetime(2026, 9, 1) - timedelta(minutes=index) + timedelta(seconds=30),
        input_count=10,
        cost_estimated_cents=1,
        cost_actual_cents=1,
        error=error,
        cancellation_reason=None,
    )


async def _list(rows, next_cursor=None) -> tuple[str, dict]:
    with (
        patch("db.base.get_db", _fake_get_db(MagicMock())),
        patch(
            "mcp_server.tools.analysis._verify_context_in_workspace_mcp",
            AsyncMock(return_value=None),
        ),
        patch("auth.analysis_gates.check_memory_analysis_access_mcp", AsyncMock()),
        patch(
            "services.analysis.query_service.list_analyses",
            AsyncMock(return_value=(rows, next_cursor)),
        ),
        patch("mcp_server.tools.analysis._log_tool_usage", AsyncMock()),
    ):
        result = await handle_list_analyses(
            {"context_id": str(uuid4()), "limit": 100}, "u1", uuid4()
        )
    text = result[0].text
    return text, json.loads(text)


@pytest.mark.asyncio
async def test_a_long_error_is_a_preview_with_a_flag():
    long_error = "Traceback line\n" * 600  # well past the preview
    body = (await _list([_run(0, long_error[:WRITER_ERROR_CAP])]))[1]
    item = body["items"][0]
    assert item["error_truncated"] is True
    assert item["error"].endswith("…")
    assert len(item["error"]) <= PREVIEW + 1
    assert long_error.startswith(item["error"][:-1])


@pytest.mark.asyncio
async def test_a_short_or_missing_error_is_returned_as_is_without_a_flag():
    short = "x" * PREVIEW
    body = (await _list([_run(0, short), _run(1, None)]))[1]
    first, second = body["items"]
    assert first["error"] == short
    assert "error_truncated" not in first
    assert second["error"] is None
    assert "error_truncated" not in second


@pytest.mark.asyncio
async def test_a_page_of_100_failed_runs_stays_under_the_default_budget():
    rows = [_run(i, "E" * WRITER_ERROR_CAP) for i in range(100)]
    text, body = await _list(rows, next_cursor="older-page-cursor")
    assert len(text) <= DEFAULT_MAX_CHARS
    placed = len(body["items"])
    assert 0 < placed < 100  # the page stopped early …
    # … and the cursor resumes right after the last run returned, in the
    # query service's (started_at, id) keyset format — not the service's own
    # cursor, which would skip the runs this page left out.
    last = rows[placed - 1]
    cursor_dt, cursor_id = _decode_list_cursor(body["next_cursor"])
    assert cursor_id == last.id
    assert cursor_dt == last.started_at
    assert [item["run_id"] for item in body["items"]] == [str(r.id) for r in rows[:placed]]


@pytest.mark.asyncio
async def test_a_page_that_fits_keeps_the_service_cursor():
    rows = [_run(i, "E" * WRITER_ERROR_CAP) for i in range(3)]
    body = (await _list(rows, next_cursor="svc-cursor"))[1]
    assert len(body["items"]) == 3
    assert body["next_cursor"] == "svc-cursor"


@pytest.mark.asyncio
async def test_the_last_page_that_fits_has_no_cursor():
    body = (await _list([_run(0, None)], next_cursor=None))[1]
    assert body["next_cursor"] is None


@pytest.mark.asyncio
async def test_get_analysis_returns_the_full_error():
    row = _run(0, "E" * WRITER_ERROR_CAP)
    with (
        patch("db.base.get_db", _fake_get_db(MagicMock())),
        patch("auth.analysis_gates.check_memory_analysis_access_mcp", AsyncMock()),
        patch(
            "services.analysis.query_service.get_analysis",
            AsyncMock(return_value=row),
        ),
        patch(
            "services.agent_binding_service.agent_binding_permits",
            AsyncMock(return_value=True),
        ),
        patch("mcp_server.tools.analysis._log_tool_usage", AsyncMock()),
    ):
        result = await handle_get_analysis({"run_id": str(uuid4())}, "u1", uuid4())
    body = json.loads(result[0].text)
    assert body["error"] == "E" * WRITER_ERROR_CAP
    assert "error_truncated" not in body
    assert UUID(body["run_id"]) == row.id
