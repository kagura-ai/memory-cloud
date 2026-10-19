"""The ``bootstrap`` tool's registration, argument parsing and budget fitting (#1851).

The composition against Postgres is in tests/services/test_session_bootstrap_service.py.
"""

from __future__ import annotations

import json
from contextlib import ExitStack
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_server.tools import _TOOLS_WITHOUT_CONTEXT_ID, get_tool_definitions
from mcp_server.tools._annotations import TOOL_ANNOTATIONS
from mcp_server.tools._profiles import CORE_TOOLS
from mcp_server.tools.bootstrap import (
    BOOTSTRAP_INDEX_MIN_CHARS,
    MAX_SINCE_DAYS,
    envelope_budget,
    handle_bootstrap,
    parse_format,
    parse_include,
    parse_since,
    resolve_format,
)
from mcp_server.tools.guide import GUIDE_INDEX, GUIDE_TOPICS
from services.session_bootstrap_service import (
    COMPONENTS,
    FORMATS,
    _fit,
    index_change_row,
    index_components,
    index_guardrails,
    window_start_cursor,
)
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
    assert tool["inputSchema"]["required"] == []  # #2125: context_id may come from ?context=
    assert list(tool["inputSchema"]["properties"]) == [
        "context_id",
        "since",
        "include",
        "max_chars",
        "format",  # #2126: last, the skeleton is order-sensitive
    ]
    assert tool["inputSchema"]["properties"]["include"]["items"]["enum"] == list(COMPONENTS)
    assert tool["inputSchema"]["properties"]["format"]["enum"] == list(FORMATS)


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


def test_since_accepts_the_whole_relative_range():
    now = utcnow()
    assert MAX_SINCE_DAYS == 999
    assert abs((now - parse_since("999d")) - timedelta(days=999)) < timedelta(seconds=5)
    assert abs((now - parse_since(" 7d ")) - timedelta(days=7)) < timedelta(seconds=5)


@pytest.mark.parametrize("raw", ["1000d", "1000D", " 12345d ", "0001d"])
def test_since_past_the_relative_range_names_the_range(raw):
    """Four digits used to fall through to the ISO 8601 parser, whose message
    does not say what the limit is (#1883)."""
    with pytest.raises(ValueError, match="'0d' to '999d'") as excinfo:
        parse_since(raw)
    assert "ISO 8601" in str(excinfo.value)  # the way to reach further back


def test_every_since_error_names_the_range():
    with pytest.raises(ValueError, match="'0d' to '999d'"):
        parse_since("1w")
    with pytest.raises(ValueError, match="'0d' to '999d'"):
        parse_since(7)


def test_the_since_schema_states_the_accepted_range():
    tool = next(t for t in get_tool_definitions() if t["name"] == "bootstrap")
    assert "'0d' to '999d'" in tool["inputSchema"]["properties"]["since"]["description"]


def test_the_guide_topic_names_every_input_property():
    """``guide(["bootstrap"])`` documented ``since`` only; ``include`` and
    ``max_chars`` were in the schema alone (#1883)."""
    tool = next(t for t in get_tool_definitions() if t["name"] == "bootstrap")
    topic = GUIDE_TOPICS["bootstrap.usage"]
    properties = tool["inputSchema"]["properties"]
    assert [name for name in properties if name not in topic] == []
    assert "bootstrap(context_id, since?, include?, max_chars?, format?)" in topic
    assert "include narrows" in topic
    for component in COMPONENTS:
        assert component in topic
    assert "max_chars defaults to 20,000" in topic
    # #2126: the selection rule, the floor, the dropped instructions and the
    # index-only guardrails cut are in the manual.
    assert "under 10,000" in topic and "2,000" in topic
    assert "instructions is left out" in topic
    assert "guardrails.truncated: true" in topic
    assert "every lane comes back empty" in topic


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
        ({"context_id": CTX, "since": "1000d"}, "validation_error", "'0d' to '999d'"),
        ({"context_id": CTX, "include": ["recall"]}, "validation_error", "include"),
        ({"context_id": CTX, "max_chars": "lots"}, "validation_error", "max_chars"),
        ({"context_id": CTX, "max_chars": 1_999}, "validation_error", "2000"),
        ({"context_id": CTX, "format": "compact"}, "validation_error", "full, index"),
        ({"context_id": CTX, "format": "full", "max_chars": 3_000}, "validation_error", "index"),
    ],
)
async def test_handler_rejects_bad_arguments_before_touching_the_database(args, error, needle):
    (block,) = await handle_bootstrap(args, "user-1", None)
    payload = json.loads(block.text)
    assert payload["error"] == error
    assert needle in payload["message"]


@pytest.mark.asyncio
async def test_full_under_ten_thousand_is_refused_with_help_naming_index():
    (block,) = await handle_bootstrap(
        {"context_id": CTX, "format": "full", "max_chars": 9_999}, "user-1", None
    )
    payload = json.loads(block.text)
    assert payload["error"] == "validation_error"
    assert 'format="index"' in payload["message"]
    assert 'format="index"' in payload["help"]


# ---------------------------------------------------------------------- format


def test_parse_format_accepts_the_two_values_and_absent():
    assert parse_format(None) is None
    assert parse_format("full") == "full"
    assert parse_format("index") == "index"


@pytest.mark.parametrize("raw", ["", "Index", "compact", 1, ["index"], True])
def test_parse_format_rejects_everything_else(raw):
    with pytest.raises(ValueError, match="full, index"):
        parse_format(raw)


@pytest.mark.parametrize(
    ("requested", "max_chars", "expected"),
    [
        (None, 20_000, "full"),  # an old plugin's call: unchanged
        (None, 3_000, "index"),
        (None, 9_999, "index"),
        (None, 10_000, "full"),
        (None, BOOTSTRAP_INDEX_MIN_CHARS, "index"),
        ("index", 20_000, "index"),
        ("full", 10_000, "full"),
        ("full", 100_000, "full"),
    ],
)
def test_format_selection_matrix(requested, max_chars, expected):
    assert resolve_format(requested, max_chars) == expected


def test_full_below_ten_thousand_is_refused():
    with pytest.raises(ValueError, match='format="index"'):
        resolve_format("full", 3_000)


def test_max_chars_floor_is_two_thousand_for_bootstrap_only():
    from utils.response_budget import BudgetArgumentError, parse_max_chars

    assert BOOTSTRAP_INDEX_MIN_CHARS == 2_000
    assert parse_max_chars(2_000, minimum=BOOTSTRAP_INDEX_MIN_CHARS) == 2_000
    with pytest.raises(BudgetArgumentError, match="between 2000 and 100000"):
        parse_max_chars(1_999, minimum=BOOTSTRAP_INDEX_MIN_CHARS)
    with pytest.raises(BudgetArgumentError, match="between 10000 and 100000"):
        parse_max_chars(2_000)  # every other tool keeps the shared floor


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


# ----------------------------------------------------------------- index mode


def _index_envelope(n=6, guardrails=True):
    """An index-mode envelope: rows already projected, no instructions."""
    row = lambda i: {"memory_id": f"id-{i}", "summary": "s" * 79 + "…", "summary_cut": True}  # noqa: E731
    env = {
        "status": "success",
        "format": "index",
        "degraded": False,
        "context": {"id": CTX, "name": "ctx"},
        "components": {
            "pinned": {
                "status": "ok",
                "memories": [
                    {"memory_id": f"p-{i}", "summary": "p" * 200, "type": "note", "importance": 0.9}
                    for i in range(n)
                ],
                "total_available": n,
                "truncated": False,
                "cap": 20,
            },
            "upcoming": {
                "status": "ok",
                "results": [
                    {**row(i), "from": "2026-11-01T00:00:00", "until": "2026-11-30T23:59:59"}
                    for i in range(n)
                ],
                "from": "2026-10-04T00:00:00",
            },
            "changes": {
                "status": "ok",
                "changes": [
                    {"memory_id": f"id-{i}", "kind": "created", "at": "2026-10-03T00:00:00Z"}
                    | {"summary": "c" * 79 + "…", "summary_cut": True}
                    for i in range(n)
                ],
                "has_more": False,
                "next_cursor": None,
            },
        },
        "since": "2026-09-27T00:00:00",
        "generated_at": "2026-10-04T00:00:00",
    }
    if guardrails:
        env = {
            **{k: v for k, v in env.items() if k != "components"},
            "guardrails": {
                "provenance": "notes",
                "items": [{**row(i), "importance": 0.8} for i in range(n)],
                "total_available": n,
                "truncated": False,
                "tool_triggered_version": 3,
            },
            "components": env["components"],
        }
    return env


def test_index_fit_never_exceeds_max_chars_with_a_guardrails_lane():
    cursors = ["c" * 90 for _ in range(6)]
    env = _index_envelope()
    full = json_chars(env)
    shell = json_chars(_fit(env, 1, change_cursors=cursors, since=SINCE, index=True))
    assert shell < 1_000  # the index shell, not the full-mode one
    for max_chars in range(shell, full, 7):
        out = _fit(env, max_chars, change_cursors=cursors, since=SINCE, index=True)
        assert json_chars(out) <= max_chars, max_chars
        assert "context_summary_omitted" not in out


def test_index_fit_cuts_guardrails_right_after_pinned():
    env = _index_envelope()
    pinned_chars = json_chars(env["components"]["pinned"]["memories"])
    shell = _fit(env, 1, change_cursors=[], since=SINCE, index=True)
    # Room for every pinned row and about two guardrail rows, nothing after.
    budget = json_chars(shell) + pinned_chars + 2 * 120
    out = _fit(env, budget, change_cursors=[], since=SINCE, index=True)
    comps = out["components"]
    assert len(comps["pinned"]["memories"]) == 6 and comps["pinned"]["truncated"] is False
    assert 0 < len(out["guardrails"]["items"]) < 6
    assert out["guardrails"]["truncated"] is True  # the block's own flag
    assert out["guardrails"]["total_available"] == 6  # says how many exist
    assert comps["upcoming"]["results"] == [] and comps["upcoming"]["truncated"] is True
    assert comps["changes"]["changes"] == [] and comps["changes"]["has_more"] is True
    assert comps["changes"]["next_cursor"] == window_start_cursor(SINCE)


def test_full_fit_never_cuts_guardrails():
    env = _envelope()
    env = {
        **{k: v for k, v in env.items() if k != "components"},
        "guardrails": _index_envelope()["guardrails"],
        "components": env["components"],
    }
    cursors = [f"cur-{i}" for i in range(6)]
    out = _fit(env, json_chars(env) // 2, change_cursors=cursors, since=SINCE)
    assert out["guardrails"] == env["guardrails"]
    assert out["context_summary_omitted"] is True  # the full-mode cut still ran


def test_shell_over_budget_returns_empty_lanes_that_say_so():
    """A long usage_guide with max_chars near the floor: no exception, every
    lane empty and truncated, the reply over max_chars but never silent."""
    env = _index_envelope()
    env["context"]["usage_guide"] = "u" * 3_000
    out = _fit(env, BOOTSTRAP_INDEX_MIN_CHARS, change_cursors=[], since=SINCE, index=True)
    comps = out["components"]
    assert comps["pinned"]["memories"] == [] and comps["pinned"]["truncated"] is True
    assert out["guardrails"]["items"] == [] and out["guardrails"]["truncated"] is True
    assert comps["upcoming"]["results"] == [] and comps["upcoming"]["truncated"] is True
    assert comps["changes"]["changes"] == [] and comps["changes"]["truncated"] is True
    assert comps["changes"]["has_more"] is True
    assert comps["changes"]["next_cursor"] == window_start_cursor(SINCE)  # not the reserve
    assert out["context"] == env["context"]  # the context block is never cut


def test_index_projection_rows():
    long = "word " * 40
    components = {
        "pinned": {
            "status": "ok",
            "memories": [
                {
                    "memory_id": "p",
                    "summary": long,
                    "context_summary": "why",
                    "type": "rule",
                    "importance": 0.9,
                    "delivery_mode": "always",
                }
            ],
            "total_available": 1,
            "truncated": False,
            "cap": 20,
        },
        "upcoming": {
            "status": "ok",
            "results": [
                {
                    "memory_id": "u",
                    "summary": "renew",
                    "type": "time",
                    "trigger": {"year": 2026, "from": "2026-11-01T00:00:00", "until": "2026-11-30"},
                }
            ],
            "from": "2026-10-04T00:00:00",
        },
        "changes": {"status": "error", "error": "component_failed"},
    }
    out = index_components(components)
    assert out["pinned"]["memories"] == [
        {"memory_id": "p", "summary": long, "type": "rule", "importance": 0.9}
    ]
    assert out["upcoming"]["results"] == [
        {"memory_id": "u", "summary": "renew", "from": "2026-11-01T00:00:00", "until": "2026-11-30"}
    ]
    assert list(out["pinned"]["memories"][0]) == ["memory_id", "summary", "type", "importance"]
    assert list(out["upcoming"]["results"][0]) == ["memory_id", "summary", "from", "until"]
    assert out["changes"] == components["changes"]  # a failed lane passes through
    block = index_guardrails(
        {
            "provenance": "notes",
            "items": [
                {
                    "memory_id": "g",
                    "summary": long,
                    "importance": 0.8,
                    "authored_by_caller": False,
                    "source_type": "manual",
                }
            ],
            "total_available": 1,
            "truncated": False,
            "tool_triggered_version": 2,
        }
    )
    (row,) = block["items"]
    assert list(row) == ["memory_id", "summary", "summary_cut", "importance"]
    assert len(row["summary"]) <= 80 and row["summary"].endswith("…")
    assert list(block) == [
        "provenance",
        "items",
        "total_available",
        "truncated",
        "tool_triggered_version",
    ]
    assert index_guardrails(None) is None  # the read failed: null passes through


def test_index_budget_leaves_room_for_the_context_fields():
    """The handler adds context_* after the fit: in index mode their room comes
    off the budget first; the full shape keeps its v0.114.0 fit."""
    fields = {
        "context_id": CTX,
        "context_name": "ctx",
        "context_display_name": "Ctx",
        "context_is_private": False,
        "context_is_locked": False,
    }
    budget = envelope_budget(3_000, "index", fields)
    env = _index_envelope()
    out = _fit(env, budget, change_cursors=["c" * 90] * 6, since=SINCE, index=True)
    assert json_chars({**out, **fields}) <= 3_000
    assert envelope_budget(20_000, "full", fields) == 20_000


def test_index_rows_never_carry_a_null_summary():
    block = index_guardrails(
        {"items": [{"memory_id": "g", "summary": None, "importance": 0.5}], "truncated": False}
    )
    assert block["items"] == [{"memory_id": "g", "importance": 0.5}]


def test_index_change_row_keeps_change_item_order():
    long = "word " * 40
    row = index_change_row(
        {"memory_id": "m", "kind": "superseded", "at": "t", "summary": long, "superseded_by": "n"}
    )
    assert list(row) == ["memory_id", "kind", "at", "summary", "summary_cut", "superseded_by"]
    forgotten = {"memory_id": "m", "kind": "forgotten", "at": "t"}
    assert index_change_row(forgotten) == forgotten
    short = {"memory_id": "m", "kind": "created", "at": "t", "summary": "short"}
    assert index_change_row(short) == short


def test_index_upcoming_row_leaves_a_missing_window_out():
    """Absent, never null: a legacy trigger without until, or no trigger at all."""
    from services.session_bootstrap_service import index_upcoming_row

    no_until = {"memory_id": "u", "summary": "s", "trigger": {"year": 2026, "from": "2026-11-01"}}
    assert index_upcoming_row(no_until) == {"memory_id": "u", "summary": "s", "from": "2026-11-01"}
    no_trigger = {"memory_id": "u", "summary": "s", "trigger": None}
    assert index_upcoming_row(no_trigger) == {"memory_id": "u", "summary": "s"}
    long = {"memory_id": "u", "summary": "word " * 40, "trigger": {"from": "a", "until": "b"}}
    assert list(index_upcoming_row(long)) == [
        "memory_id",
        "summary",
        "summary_cut",
        "from",
        "until",
    ]


# ------------------------------------------------------------ handler wiring


async def _handler_build_kwargs(args):
    """Run handle_bootstrap with the database and the service patched; return
    the kwargs it passed to ``SessionBootstrapService.build`` and the reply."""
    from mcp_server.tools import bootstrap as module

    async def gen():
        yield AsyncMock()

    fields = {
        "context_id": CTX,
        "context_name": "ctx",
        "context_display_name": "Ctx",
        "context_is_private": False,
        "context_is_locked": False,
    }
    service = MagicMock()
    service.build = AsyncMock(return_value={"status": "success"})
    with ExitStack() as stack:
        stack.enter_context(patch("db.base.get_db", new=gen))
        stack.enter_context(
            patch.object(module, "_resolve_context_for_read", new=AsyncMock(return_value=object()))
        )
        stack.enter_context(patch.object(module, "_log_tool_usage", new=AsyncMock()))
        stack.enter_context(patch.object(module, "_context_response_fields", return_value=fields))
        stack.enter_context(
            patch(
                "services.session_bootstrap_service.SessionBootstrapService",
                return_value=service,
            )
        )
        (block,) = await module.handle_bootstrap({"context_id": CTX, **args}, "user-1", None)
    service.build.assert_awaited_once()
    return service.build.await_args.kwargs, fields, json.loads(block.text)


@pytest.mark.asyncio
async def test_handler_passes_index_and_the_reduced_budget():
    kwargs, fields, reply = await _handler_build_kwargs({"max_chars": 3_000})
    assert kwargs["format"] == "index"
    assert kwargs["max_chars"] == 3_000 - json_chars(fields)
    assert reply["context_id"] == CTX  # the fields are added after the fit


@pytest.mark.asyncio
async def test_handler_default_call_is_full_at_twenty_thousand():
    kwargs, _, _ = await _handler_build_kwargs({})
    assert kwargs["format"] == "full"
    assert kwargs["max_chars"] == 20_000


@pytest.mark.asyncio
async def test_handler_explicit_index_keeps_the_default_budget_minus_the_fields():
    kwargs, fields, _ = await _handler_build_kwargs({"format": "index"})
    assert kwargs["format"] == "index"
    assert kwargs["max_chars"] == 20_000 - json_chars(fields)
