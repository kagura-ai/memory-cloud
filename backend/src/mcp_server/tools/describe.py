"""``describe_tools`` — find the tools the URL's ``tools/list`` left out (#1849).

``tools/list`` defaults to the ``core`` profile: a model sees the memory and
context tools and nothing else. A profile is a view, not authorization — every
registered tool stays callable through ``tools/call`` — but a model cannot call
what it has never seen. This tool closes that gap from inside the session:
without arguments it lists every tool outside the current view as a name, a
title and one summary line; with ``names`` it returns those tools' complete
definitions (the same dicts ``tools/list`` would send) so the model can call
them right away. The response always says how to list more by URL.

"The current view" is the request URL's selection, stored per request by the
transport (``set_mcp_tool_view``). A direct call with no request (tests) falls
back to the core set.
"""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from mcp.types import TextContent

from mcp_server.tools._definitions import get_tool_definitions
from mcp_server.tools._helpers import _error_response, _success_response, get_mcp_tool_view
from mcp_server.tools._profiles import CORE_TOOLS

MAX_NAMES = 20
SUMMARY_CHARS = 160

URL_HINT: dict[str, str] = {
    "list_all": "?profile=full on the MCP URL lists every tool",
    "allowlist": "?tools=<name>,<name> lists exactly those (wins over profile)",
    "note": "Every tool is callable now, listed or not; a profile only picks what tools/list shows.",
}

_SENTENCE_END = re.compile(r"(?<=[.!?])\s")


def summarize(description: str) -> str:
    """The first sentence of a description, cut to ``SUMMARY_CHARS`` on a word."""
    first = description.strip().splitlines()[0] if description.strip() else ""
    sentence = _SENTENCE_END.split(first, maxsplit=1)[0].strip()
    if len(sentence) <= SUMMARY_CHARS:
        return sentence
    cut = sentence[: SUMMARY_CHARS - 1].rsplit(" ", 1)[0]
    return cut + "…"


def current_view() -> frozenset[str]:
    """The names the request's URL lists, or the core set when no request set one."""
    view = get_mcp_tool_view()
    return view if view is not None else frozenset(CORE_TOOLS)


def hidden_tools(query: str | None = None) -> list[dict[str, str]]:
    """Tools outside the current view as ``{name, title, summary}``, registry order."""
    view = current_view()
    needle = (query or "").strip().lower()
    rows: list[dict[str, str]] = []
    for tool in get_tool_definitions():
        if tool["name"] in view:
            continue
        row = {
            "name": tool["name"],
            "title": tool.get("title", tool["name"]),
            "summary": summarize(tool.get("description", "")),
        }
        if needle and needle not in " ".join(row.values()).lower():
            continue
        rows.append(row)
    return rows


def full_definitions(names: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    """The registry entries for ``names`` (registry order) and the names it does not know."""
    by_name = {tool["name"]: tool for tool in get_tool_definitions()}
    wanted = {n.strip() for n in names}
    found = [tool for name, tool in by_name.items() if name in wanted]
    unknown = [n for n in names if n.strip() not in by_name]
    return found, unknown


async def handle_describe_tools(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """List the tools this URL's ``tools/list`` left out, or return their full schemas.

    Static registry data: no database, nothing caller-specific beyond the
    request URL's own selection. ``names`` → the complete definitions (title,
    annotations, inputSchema) for up to ``MAX_NAMES`` tools, ``unknown`` for
    the rest; otherwise ``tools`` → one line per hidden tool, optionally
    narrowed by ``query`` (case-insensitive substring on name, title, summary).
    """
    names = args.get("names")
    query = args.get("query")
    if query is not None and not isinstance(query, str):
        return _error_response("validation_error", "query must be a string.")
    if names is not None:
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            return _error_response(
                "validation_error", 'names must be a list of tool names, e.g. ["get_usage"].'
            )
        if len(names) > MAX_NAMES:
            return _error_response(
                "validation_error", f"names holds at most {MAX_NAMES} tools (got {len(names)})."
            )
        found, unknown = full_definitions(names)
        payload: dict[str, Any] = {"definitions": found, "url": URL_HINT}
        if unknown:
            payload["unknown"] = unknown
            payload["hint"] = "describe_tools() with no arguments lists every tool you can ask for."
        return _success_response(**payload)
    rows = hidden_tools(query)
    return _success_response(
        tools=rows,
        count=len(rows),
        listed=sorted(current_view()),
        url=URL_HINT,
        hint='describe_tools(names=["<tool>"]) returns a tool\'s full schema so you can call it.',
    )
