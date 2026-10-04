"""The ``bootstrap`` tool's registration, argument parsing and budget fitting (#1851).

The composition against Postgres is in tests/services/test_session_bootstrap_service.py.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from mcp_server.tools import _TOOLS_WITHOUT_CONTEXT_ID, get_tool_definitions
from mcp_server.tools._annotations import TOOL_ANNOTATIONS
from mcp_server.tools._profiles import CORE_TOOLS
from mcp_server.tools.bootstrap import handle_bootstrap, parse_include, parse_since
from mcp_server.tools.guide import GUIDE_INDEX
from services.session_bootstrap_service import COMPONENTS, _fit, window_start_cursor
from utils.datetime import utcnow
from utils.response_budget import json_chars

CTX = "00000000-0000-4000-8000-000000000001"
SINCE = datetime(2026, 9, 27)


def test_bootstrap_is_a_core_read_tool_with_a_manual():
    assert "bootstrap" in CORE_TOOLS
    assert TOOL_ANNOTATIONS["bootstrap"]["readOnlyHint"] is True
    assert "bootstrap" not in _TOOLS_WITHOUT_CONTEXT_ID
    assert "bootstrap" in GUIDE_INDEX
    tool = next(t for t in get_tool_definitions() if t["name"] == "bootstrap")
    assert tool["inputSchema"]["required"] == ["context_id"]
    assert set(tool["inputSchema"]["properties"]) == {"context_id", "since", "include", "max_chars"}
    assert tool["inputSchema"]["properties"]["include"]["items"]["enum"] == list(COMPONENTS)


# --------------------------------------------------------------------- parsing


def test_since_defaults_to_seven_days_back_and_accepts_relative_days():
    now = utcnow()
    assert abs((now - parse_since(None)) - timedelta(days=7)) < timedelta(seconds=5)
    assert abs((now - parse_since("")) - timedelta(days=7)) < timedelta(seconds=5)
    assert abs((now - parse_since("30d")) - timedelta(days=30)) < timedelta(seconds=5)
    assert abs((now - parse_since("30D")) - timedelta(days=30)) < timedelta(seconds=5)
    assert abs(now - parse_since("0d")) < timedelta(seconds=5)


def test_since_accepts_iso_8601_and_normalises_to_naive_utc():
    assert parse_since("2026-10-01T00:00:00Z") == datetime(2026, 10, 1)
    assert parse_since("2026-10-01T09:00:00+09:00") == datetime(2026, 10, 1)
    assert parse_since("2026-10-01") == datetime(2026, 10, 1)  # naive = UTC


@pytest.mark.parametrize("raw", [7, "7", "7 d", "d7", "yesterday", "1w", "1000d", ["7d"]])
def test_since_rejects_everything_else(raw):
    with pytest.raises(ValueError):
        parse_since(raw)


def test_since_error_names_the_relative_form():
    with pytest.raises(ValueError, match="'<N>d'"):
        parse_since("1w")


def test_include_defaults_to_all_components_and_dedupes():
    assert parse_include(None) == COMPONENTS
    assert parse_include(["changes", "pinned", "changes"]) == ("changes", "pinned")


@pytest.mark.parametrize("raw", [[], ["recall"], "pinned", ["pinned", 1]])
def test_include_rejects_unknown_components(raw):
    with pytest.raises(ValueError):
        parse_include(raw)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("args", "error", "needle"),
    [
        ({}, "missing_fields", "context_id"),
        ({"context_id": CTX, "since": "tomorrow"}, "validation_error", "since"),
        ({"context_id": CTX, "include": ["recall"]}, "validation_error", "include"),
        ({"context_id": CTX, "max_chars": "lots"}, "validation_error", "max_chars"),
    ],
)
async def test_handler_rejects_bad_arguments_before_touching_the_database(args, error, needle):
    (block,) = await handle_bootstrap(args, "user-1", None)
    payload = json.loads(block.text)
    assert payload["error"] == error
    assert needle in payload["message"]


# --------------------------------------------------------------------- fitting


def _envelope(n_pinned=6, n_upcoming=6, n_changes=6):
    item = lambda i, key: {key: f"id-{i}", "summary": "s" * 200, "context_summary": "c" * 300}  # noqa: E731
    return {
        "status": "success",
        "degraded": False,
        "context": {"id": CTX, "name": "ctx"},
        "instructions": "",
        "components": {
            "pinned": {
                "status": "ok",
                "memories": [item(i, "memory_id") for i in range(n_pinned)],
                "total_available": n_pinned,
                "truncated": False,
                "cap": 20,
            },
            "upcoming": {
                "status": "ok",
                "results": [item(i, "memory_id") for i in range(n_upcoming)],
                "from": "2026-10-04T00:00:00",
            },
            "changes": {
                "status": "ok",
                "changes": [item(i, "memory_id") for i in range(n_changes)],
                "has_more": False,
                "next_cursor": None,
            },
        },
        "since": "2026-09-27T00:00:00",
        "generated_at": "2026-10-04T00:00:00",
    }


def test_fit_returns_the_envelope_untouched_when_it_fits():
    env = _envelope()
    assert _fit(env, json_chars(env), change_cursors=[], since=SINCE) is env


def test_fit_output_never_exceeds_max_chars_even_with_a_real_cursor():
    """The keyset cursor (~90 chars) is appended after the cut; the shell reserves it."""
    cursors = ["c" * 90 for _ in range(6)]
    env = _envelope()
    full = json_chars(env)
    for max_chars in range(full // 3, full, 53):
        out = _fit(env, max_chars, change_cursors=cursors, since=SINCE)
        assert json_chars(out) <= max_chars, max_chars


def test_fit_drops_context_summary_first_then_cuts_the_lowest_lanes():
    env = _envelope()
    full = json_chars(env)
    out = _fit(env, full // 2, change_cursors=[f"cur-{i}" for i in range(6)], since=SINCE)
    assert json_chars(out) <= full // 2
    assert out["context_summary_omitted"] is True
    comps = out["components"]
    # Pinned is the first lane to keep its items; changes the first to lose them.
    assert len(comps["pinned"]["memories"]) >= len(comps["changes"]["changes"])
    for name, key in (("pinned", "memories"), ("upcoming", "results"), ("changes", "changes")):
        if len(comps[name][key]) < 6:
            assert comps[name]["truncated"] is True
    if comps["changes"].get("truncated"):
        assert comps["changes"]["has_more"] is True  # a cut page is not the end of the log
        kept = len(comps["changes"]["changes"])
        expected = f"cur-{kept - 1}" if kept else window_start_cursor(SINCE)
        assert comps["changes"]["next_cursor"] == expected
    assert out["context"] == env["context"]  # the context block is never cut


def test_fit_skips_failed_lanes_and_keeps_their_error():
    env = _envelope()
    env["components"]["upcoming"] = {"status": "error", "error": "component_failed"}
    out = _fit(env, json_chars(env) // 2, change_cursors=[], since=SINCE)
    assert out["components"]["upcoming"] == {"status": "error", "error": "component_failed"}
    assert json_chars(out) <= json_chars(env) // 2
