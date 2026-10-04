"""``describe_tools`` and the core default of ``tools/list`` (#1849).

A profile is a view: a tool left out of ``tools/list`` stays callable, but a
model cannot call what it has never seen. ``describe_tools`` lists what the
URL left out and returns the hidden tools' complete definitions on request,
reading the request's view from the contextvar the transport sets.
"""

from __future__ import annotations

import json

import pytest

from mcp_server.tools import _RATE_LIMIT_EXEMPT_TOOLS, _TOOLS_WITHOUT_CONTEXT_ID
from mcp_server.tools._annotations import TOOL_ANNOTATIONS
from mcp_server.tools._definitions import get_tool_definitions
from mcp_server.tools._helpers import set_mcp_tool_view
from mcp_server.tools._profiles import CORE_TOOLS, DEFAULT_PROFILE, PROFILES
from mcp_server.tools.describe import (
    MAX_NAMES,
    SUMMARY_CHARS,
    full_definitions,
    handle_describe_tools,
    hidden_tools,
    summarize,
)


@pytest.fixture(autouse=True)
def _no_request_view():
    set_mcp_tool_view(None)
    yield
    set_mcp_tool_view(None)


def test_the_default_profile_is_core():
    assert DEFAULT_PROFILE == "core"
    assert PROFILES[DEFAULT_PROFILE] == CORE_TOOLS


def test_describe_tools_is_a_core_read_tool_that_needs_no_context():
    assert "describe_tools" in CORE_TOOLS
    assert TOOL_ANNOTATIONS["describe_tools"]["readOnlyHint"] is True
    assert "describe_tools" in _TOOLS_WITHOUT_CONTEXT_ID
    assert "describe_tools" in _RATE_LIMIT_EXEMPT_TOOLS


def test_hidden_tools_are_exactly_the_registry_minus_the_view():
    names = [row["name"] for row in hidden_tools()]
    expected = [t["name"] for t in get_tool_definitions() if t["name"] not in CORE_TOOLS]
    assert names == expected  # registry order, nothing from the core set
    assert "get_usage" in names and "recall" not in names
    for row in hidden_tools():
        assert set(row) == {"name", "title", "summary"}
        assert row["title"] == TOOL_ANNOTATIONS[row["name"]]["title"]
        assert 0 < len(row["summary"]) <= SUMMARY_CHARS


def test_the_view_comes_from_the_request_contextvar():
    set_mcp_tool_view(frozenset({"recall"}))
    names = {row["name"] for row in hidden_tools()}
    assert "remember" in names and "recall" not in names


def test_query_narrows_by_name_title_or_summary_case_insensitively():
    rows = hidden_tools("SLEEP")
    assert rows and all("sleep" in " ".join(row.values()).lower() for row in rows)
    assert hidden_tools("no-such-tool-xyz") == []


def test_full_definitions_are_the_registry_dicts_with_annotations():
    found, unknown = full_definitions(["get_usage", "nope", " recall "])
    by_name = {t["name"]: t for t in get_tool_definitions()}
    assert [t["name"] for t in found] == ["recall", "get_usage"]  # registry order
    assert found[1] == by_name["get_usage"]
    assert "annotations" in found[1] and "title" in found[1]
    assert unknown == ["nope"]


def test_summarize_takes_the_first_sentence_and_cuts_on_a_word():
    assert summarize("Do a thing. Then another.\nMore.") == "Do a thing."
    long = " ".join(["word"] * 60) + "."
    cut = summarize(long)
    assert cut.endswith("…") and len(cut) <= SUMMARY_CHARS


@pytest.mark.asyncio
async def test_handle_lists_hidden_tools_with_the_url_hint():
    (block,) = await handle_describe_tools({}, "u", None)
    payload = json.loads(block.text)
    assert payload["status"] == "success"
    assert payload["count"] == len(payload["tools"]) > 40
    assert payload["listed"] == sorted(CORE_TOOLS)
    assert "profile=full" in payload["url"]["list_all"]
    assert "describe_tools(names=" in payload["hint"]


@pytest.mark.asyncio
async def test_handle_returns_full_schemas_for_names_and_reports_unknown():
    (block,) = await handle_describe_tools({"names": ["get_usage", "bogus"]}, "u", None)
    payload = json.loads(block.text)
    assert [d["name"] for d in payload["definitions"]] == ["get_usage"]
    assert payload["definitions"][0]["inputSchema"]["type"] == "object"
    assert payload["unknown"] == ["bogus"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    [{"names": "get_usage"}, {"names": ["x"] * (MAX_NAMES + 1)}, {"query": 3}],
)
async def test_handle_validates_its_arguments(args):
    (block,) = await handle_describe_tools(args, "u", None)
    assert json.loads(block.text)["error"] == "validation_error"
