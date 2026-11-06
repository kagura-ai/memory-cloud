"""Public tool definitions and the URL narrowing (#2675)."""

from __future__ import annotations

import json

import pytest

from mcp_server.public.definitions import (
    PUBLIC_TOOL_DEFINITIONS,
    PUBLIC_TOOLS,
    public_argument_names,
    public_tool_definitions,
    public_tool_view,
)
from mcp_server.tools._profiles import ToolProfileError

FIVE = ("recall", "reference", "explore", "list_tags", "get_context_info")


def test_exactly_the_five_public_tools_in_order() -> None:
    assert PUBLIC_TOOLS == FIVE
    assert tuple(d["name"] for d in PUBLIC_TOOL_DEFINITIONS) == FIVE


def test_definitions_fit_the_budget() -> None:
    total = len(json.dumps(PUBLIC_TOOL_DEFINITIONS, ensure_ascii=False, separators=(",", ":")))
    assert total <= 6_000, total
    for definition in PUBLIC_TOOL_DEFINITIONS:
        assert len(definition["description"]) <= 600, definition["name"]


@pytest.mark.parametrize("definition", PUBLIC_TOOL_DEFINITIONS, ids=lambda d: d["name"])
def test_every_tool_is_annotated_read_only(definition: dict) -> None:
    annotations = definition["annotations"]
    assert annotations["readOnlyHint"] is True
    assert annotations["destructiveHint"] is False
    assert annotations["idempotentHint"] is True
    assert annotations["openWorldHint"] is False
    assert annotations["title"] == definition["title"]
    assert definition["readOnly"] is True
    schema = definition["inputSchema"]
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert "context_id" in schema["properties"]


BANNED = (
    "describe_tools",
    "profile",
    "add_tools",
    "sign in",
    "sign-in",
    "login",
    "list_contexts",
    "remember",
    "forget",
    "workspace",
    "context_ids",
    "version",
    "api key",
    "guide(",
)


@pytest.mark.parametrize("definition", PUBLIC_TOOL_DEFINITIONS, ids=lambda d: d["name"])
def test_descriptions_offer_no_bypass(definition: dict) -> None:
    text = json.dumps(definition).lower()
    for word in BANNED:
        assert word not in text, (definition["name"], word)


def test_reference_schema_has_no_version_and_explicit_arguments() -> None:
    assert public_argument_names("reference") == {
        "memory_id",
        "content_offset",
        "max_chars",
        "context_id",
    }
    assert public_argument_names("recall") == {
        "query",
        "k",
        "search_mode",
        "tags",
        "max_chars",
        "context_id",
    }
    assert public_argument_names("explore") == {"memory_id", "depth", "limit", "context_id"}
    assert public_argument_names("list_tags") == {
        "limit",
        "min_count",
        "sort",
        "prefix",
        "with_tags",
        "context_id",
    }
    assert public_argument_names("get_context_info") == {"context_id"}


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        (b"", FIVE),
        (None, FIVE),
        (b"profile=full", FIVE),
        (b"profile=core", FIVE),
        (b"add_tools=remember,describe_tools", FIVE),
        (b"tools=remember,recall", ("recall",)),
        (b"tools=recall", ("recall",)),
        (b"tools=reference,recall", ("recall", "reference")),
        (b"profile=full&tools=remember,list_tags", ("list_tags",)),
        (b"tools=recall&add_tools=remember", ("recall",)),
        (b"tools=%20recall%20,explore", ("recall", "explore")),
        ("tools=get_context_info", ("get_context_info",)),
    ],
)
def test_view_only_narrows(query, expected) -> None:
    assert public_tool_view(query) == expected
    assert tuple(d["name"] for d in public_tool_definitions(query)) == expected


@pytest.mark.parametrize("query", [b"tools=remember", b"tools=", b"profile=full&tools=forget"])
def test_view_matching_nothing_is_an_error(query) -> None:
    with pytest.raises(ToolProfileError, match="matches no tool of this endpoint"):
        public_tool_view(query)


def test_listed_definitions_are_copies() -> None:
    listed = public_tool_definitions(b"")
    listed[0]["inputSchema"]["properties"]["injected"] = {"type": "string"}
    assert "injected" not in PUBLIC_TOOL_DEFINITIONS[0]["inputSchema"]["properties"]
