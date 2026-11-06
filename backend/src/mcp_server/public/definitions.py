"""The five tools of the public MCP endpoint and the URL narrowing (#2675).

Hand-written definitions: the same names as the authenticated tools (client
habits carry over), but public descriptions and schemas that accept only the
arguments the public handlers take (``additionalProperties: false``). The
allowlist is ``PUBLIC_TOOLS``; a URL's ``?tools=`` may only narrow it, and
the same narrowed view decides what ``tools/call`` accepts. ``profile=``,
``add_tools=`` and the other ``/mcp`` parameters are ignored here.
"""

from __future__ import annotations

import copy
from typing import Any
from urllib.parse import parse_qsl

from mcp_server.public.limits import (
    PUBLIC_DEFAULT_EXPLORE_LIMIT,
    PUBLIC_DEFAULT_K,
    PUBLIC_DEFAULT_MAX_CHARS,
    PUBLIC_DEFAULT_TAGS_LIMIT,
    PUBLIC_MAX_EXPLORE_DEPTH,
    PUBLIC_MAX_EXPLORE_LIMIT,
    PUBLIC_MAX_FILTER_TAGS,
    PUBLIC_MAX_K,
    PUBLIC_MAX_MAX_CHARS,
    PUBLIC_MAX_MIN_COUNT,
    PUBLIC_MAX_QUERY_CHARS,
    PUBLIC_MAX_TAG_CHARS,
    PUBLIC_MAX_TAGS_LIMIT,
    PUBLIC_MIN_MAX_CHARS,
)
from mcp_server.tools._annotations import _read
from mcp_server.tools._profiles import ToolProfileError
from utils.logger import get_logger

logger = get_logger(__name__)

PUBLIC_TOOLS: tuple[str, ...] = (
    "recall",
    "reference",
    "explore",
    "list_tags",
    "get_context_info",
)

# Query parameters of /mcp that this endpoint does not honour.
_IGNORED_QUERY_KEYS = ("profile", "add_tools", "context", "guardrails")

_CONTEXT_ID = {
    "type": "string",
    "description": "Optional. Only this endpoint's own dataset id is accepted.",
}
_MEMORY_ID = {"type": "string", "description": "An entry's memory_id from recall or explore."}
_MAX_CHARS = {
    "type": "integer",
    "minimum": PUBLIC_MIN_MAX_CHARS,
    "maximum": PUBLIC_MAX_MAX_CHARS,
    "default": PUBLIC_DEFAULT_MAX_CHARS,
    "description": "Response size limit in characters.",
}


def _definition(
    name: str, title: str, description: str, properties: dict[str, Any], required: list[str]
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {**properties, "context_id": _CONTEXT_ID},
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return {
        "name": name,
        "title": title,
        "readOnly": True,
        "description": description,
        "inputSchema": schema,
        "annotations": _read(title),
    }


PUBLIC_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    _definition(
        "recall",
        "Search Public Data",
        "Search this read-only public dataset by meaning and keywords. Returns "
        "{status, count, results: [{memory_id, doc_id, summary, score, type, tags?, "
        "source_url?, created_at, updated_at, observed_at?}], truncated?, degraded?}. "
        "Use reference(memory_id) for an entry's full text.",
        {
            "query": {
                "type": "string",
                "minLength": 1,
                "maxLength": PUBLIC_MAX_QUERY_CHARS,
                "description": "What to look for.",
            },
            "k": {
                "type": "integer",
                "minimum": 1,
                "maximum": PUBLIC_MAX_K,
                "default": PUBLIC_DEFAULT_K,
                "description": "Number of results.",
            },
            "search_mode": {
                "type": "string",
                "enum": ["hybrid", "semantic", "keyword"],
                "default": "hybrid",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string", "maxLength": PUBLIC_MAX_TAG_CHARS},
                "maxItems": PUBLIC_MAX_FILTER_TAGS,
                "description": "Only entries carrying all of these tags (see list_tags).",
            },
            "max_chars": _MAX_CHARS,
        },
        ["query"],
    ),
    _definition(
        "reference",
        "Read Public Entry",
        "Read one entry of this public dataset in full. Returns {status, memory: "
        "{memory_id, doc_id, summary, type, tags?, source_url?, created_at, updated_at, "
        "observed_at?, content, content_truncated, content_next_offset}}. When "
        "content_truncated is true, call again with content_offset=content_next_offset.",
        {
            "memory_id": _MEMORY_ID,
            "content_offset": {
                "type": "integer",
                "minimum": 0,
                "description": "Continue the content from this character offset.",
            },
            "max_chars": _MAX_CHARS,
        },
        ["memory_id"],
    ),
    _definition(
        "explore",
        "Explore Related Public Entries",
        "List entries of this public dataset related to one entry. Returns {status, "
        "exploration: {seed: {memory_id, doc_id, summary}, related: [{memory_id, doc_id, "
        "summary, hop, edge_type}], returned, truncated?}}.",
        {
            "memory_id": _MEMORY_ID,
            "depth": {
                "type": "integer",
                "minimum": 1,
                "maximum": PUBLIC_MAX_EXPLORE_DEPTH,
                "default": 1,
                "description": "1 = direct relations, 2 = also their relations.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": PUBLIC_MAX_EXPLORE_LIMIT,
                "default": PUBLIC_DEFAULT_EXPLORE_LIMIT,
            },
        },
        ["memory_id"],
    ),
    _definition(
        "list_tags",
        "List Public Tags",
        "List the categories (tags) of this public dataset with entry counts, to pick "
        "recall tag filters. Returns {status, tags: [{tag, count}], total, has_more}.",
        {
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": PUBLIC_MAX_TAGS_LIMIT,
                "default": PUBLIC_DEFAULT_TAGS_LIMIT,
            },
            "min_count": {"type": "integer", "minimum": 1, "maximum": PUBLIC_MAX_MIN_COUNT},
            "sort": {"type": "string", "enum": ["count", "recent", "alpha"], "default": "count"},
            "prefix": {"type": "string", "maxLength": PUBLIC_MAX_TAG_CHARS},
            "with_tags": {
                "type": "array",
                "items": {"type": "string", "maxLength": PUBLIC_MAX_TAG_CHARS},
                "maxItems": PUBLIC_MAX_FILTER_TAGS,
                "description": "Only tags that co-occur with all of these.",
            },
        },
        [],
    ),
    _definition(
        "get_context_info",
        "Describe Public Dataset",
        "Describe this public dataset: its name, description, usage guide and "
        "identifiers. Returns {status, endpoint: {public_id, name, description?, "
        "usage_guide?}, resource: {resource_id}, context: {id, name}}. Call it first.",
        {},
        [],
    ),
]

_BY_NAME: dict[str, dict[str, Any]] = {d["name"]: d for d in PUBLIC_TOOL_DEFINITIONS}


def public_tool_view(query_string: bytes | str | None) -> tuple[str, ...]:
    """The public tools the endpoint URL asks for, in ``PUBLIC_TOOLS`` order.

    Only ``tools=`` is read, and only as an intersection with
    ``PUBLIC_TOOLS``: it can hide tools, never add one. Every other
    parameter, including ``profile=`` and ``add_tools=``, is ignored.

    Raises:
        ToolProfileError: ``tools=`` names no tool of this endpoint.
    """
    if isinstance(query_string, bytes):
        query_string = query_string.decode("utf-8", "replace")
    params: dict[str, str] = {}
    for key, value in parse_qsl(query_string or "", keep_blank_values=True):
        params.setdefault(key, value)
    ignored = sorted(k for k in _IGNORED_QUERY_KEYS if k in params)
    if ignored:
        logger.info("public_mcp_query_ignored", parameters=ignored)
    if "tools" not in params:
        return PUBLIC_TOOLS
    wanted = {name.strip() for name in params["tools"].split(",") if name.strip()}
    view = tuple(name for name in PUBLIC_TOOLS if name in wanted)
    if not view:
        raise ToolProfileError(
            "The tools parameter matches no tool of this endpoint. It serves: "
            + ", ".join(PUBLIC_TOOLS)
            + "."
        )
    return view


def public_tool_definitions(query_string: bytes | str | None) -> list[dict[str, Any]]:
    """Copies of the public definitions in the URL's view (``tools/list``)."""
    return [copy.deepcopy(_BY_NAME[name]) for name in public_tool_view(query_string)]


def public_argument_names(name: str) -> frozenset[str]:
    """The argument names the public schema of ``name`` accepts."""
    return frozenset(_BY_NAME[name]["inputSchema"]["properties"])
