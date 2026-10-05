"""``describe_tools`` and the core default of ``tools/list`` (#1849).

A profile is a view: a tool left out of ``tools/list`` stays callable, but a
model cannot call what it has never seen. ``describe_tools`` lists what the
URL left out and returns the hidden tools' complete definitions on request,
reading the request's view from the contextvar the transport sets.
"""

from __future__ import annotations

import json
import re

import pytest

from mcp_server.tools import _RATE_LIMIT_EXEMPT_TOOLS, _TOOLS_WITHOUT_CONTEXT_ID
from mcp_server.tools._annotations import TOOL_ANNOTATIONS
from mcp_server.tools._definitions import get_tool_definitions
from mcp_server.tools._helpers import set_mcp_tool_view_query
from mcp_server.tools._profiles import CORE_TOOLS, DEFAULT_PROFILE, PROFILES
from mcp_server.tools.describe import (
    MAX_NAMES,
    SUMMARY_CHARS,
    current_view,
    full_definitions,
    handle_describe_tools,
    hidden_tools,
    summarize,
)

REGISTRY = get_tool_definitions()


def _hidden(query=None):
    view, _ = current_view()
    return hidden_tools(REGISTRY, view, query)


@pytest.fixture(autouse=True)
def _no_request_view():
    set_mcp_tool_view_query(None)
    yield
    set_mcp_tool_view_query(None)


def test_the_default_profile_is_core():
    assert DEFAULT_PROFILE == "core"
    assert PROFILES[DEFAULT_PROFILE] == CORE_TOOLS


def test_describe_tools_is_a_core_read_tool_that_needs_no_context():
    assert "describe_tools" in CORE_TOOLS
    assert TOOL_ANNOTATIONS["describe_tools"]["readOnlyHint"] is True
    assert "describe_tools" in _TOOLS_WITHOUT_CONTEXT_ID
    assert "describe_tools" in _RATE_LIMIT_EXEMPT_TOOLS


def test_hidden_tools_are_exactly_the_registry_minus_the_view():
    names = [row["name"] for row in _hidden()]
    expected = [t["name"] for t in REGISTRY if t["name"] not in CORE_TOOLS]
    assert names == expected  # registry order, nothing from the core set
    assert "get_usage" in names and "recall" not in names
    for row in _hidden():
        assert set(row) == {"name", "title", "summary"}
        assert row["title"] == TOOL_ANNOTATIONS[row["name"]]["title"]
        assert 0 < len(row["summary"]) <= SUMMARY_CHARS


def test_the_view_is_derived_from_the_request_url_query():
    set_mcp_tool_view_query(b"tools=recall")
    names = {row["name"] for row in _hidden()}
    assert "remember" in names and "recall" not in names
    set_mcp_tool_view_query(b"profile=full")
    assert _hidden() == []


def test_a_broken_url_selection_is_reported_not_papered_over():
    set_mcp_tool_view_query(b"profile=typo")
    view, error = current_view()
    assert view == frozenset(CORE_TOOLS)
    assert error and "unknown tool profile" in error


def test_query_narrows_by_name_title_or_summary_case_insensitively():
    rows = _hidden("SLEEP")
    assert rows and all("sleep" in " ".join(row.values()).lower() for row in rows)
    assert _hidden("no-such-tool-xyz") == []


def test_full_definitions_are_the_registry_dicts_with_annotations():
    found, unknown = full_definitions(REGISTRY, ["get_usage", "nope", " recall ", "nope", " nope "])
    by_name = {t["name"]: t for t in REGISTRY}
    assert [t["name"] for t in found] == ["recall", "get_usage"]  # registry order
    assert found[1] == by_name["get_usage"]
    assert "annotations" in found[1] and "title" in found[1]
    assert unknown == ["nope"]  # trimmed and de-duplicated


def test_no_registry_summary_ends_on_an_abbreviation():
    for row in _hidden():
        assert not re.search(r"\b(e\.g\.|i\.e\.|vs\.|etc\.)$", row["summary"]), row


def test_summarize_does_not_split_after_an_abbreviation():
    assert (
        summarize("Copy memories, e.g. for a merge. Then stop.")
        == "Copy memories, e.g. for a merge."
    )


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
    assert "describe_tools(names=" in payload["hint"] and "reconnect" in payload["hint"]
    assert "url_error" not in payload


@pytest.mark.asyncio
async def test_handle_reports_a_broken_url_selection():
    set_mcp_tool_view_query(b"tools=bogus")
    (block,) = await handle_describe_tools({}, "u", None)
    payload = json.loads(block.text)
    assert "matches no known tool" in payload["url_error"]


@pytest.mark.asyncio
async def test_an_empty_names_list_lists_the_hidden_tools():
    (block,) = await handle_describe_tools({"names": []}, "u", None)
    assert "tools" in json.loads(block.text)


@pytest.mark.asyncio
@pytest.mark.parametrize("names", [[""], [" "], ["", " ", "\t"]])
async def test_names_with_no_non_blank_entry_list_the_hidden_tools(names):
    """Blank names used to reach the ``names`` branch and return an empty
    ``definitions`` list with no ``unknown`` and no hint (#1883)."""
    (block,) = await handle_describe_tools({"names": names}, "u", None)
    payload = json.loads(block.text)
    (listing,) = await handle_describe_tools({}, "u", None)
    assert payload == json.loads(listing.text)
    assert "definitions" not in payload and payload["count"] > 40


@pytest.mark.asyncio
async def test_blank_names_are_dropped_next_to_real_ones():
    (block,) = await handle_describe_tools({"names": [" ", " get_usage ", ""]}, "u", None)
    payload = json.loads(block.text)
    assert [d["name"] for d in payload["definitions"]] == ["get_usage"]
    assert "unknown" not in payload


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
    [
        {"names": "get_usage"},
        {"names": ["get_usage", 3]},
        {"names": ["x"] * (MAX_NAMES + 1)},
        {"names": [" "] * (MAX_NAMES + 1)},
        {"query": 3},
    ],
)
async def test_handle_validates_its_arguments(args):
    (block,) = await handle_describe_tools(args, "u", None)
    assert json.loads(block.text)["error"] == "validation_error"
