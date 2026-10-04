"""``describe_tools`` — find the tools the URL's ``tools/list`` left out (#1849).

``tools/list`` defaults to the ``core`` profile: a model sees the memory and
context tools and nothing else. The server itself accepts ``tools/call`` for
every registered tool, but mainstream clients (Claude Code, Codex, ChatGPT,
Cursor) let the model invoke only what their ``tools/list`` returned — so a
hidden tool is, in practice, out of reach until the client's URL lists it.
This tool is the discovery lane for that gap: without arguments it names every
tool outside the current view with a one-line summary; with ``names`` it
returns those tools' complete definitions so the model can decide, and tell the
user, which URL to reconnect with (``?profile=full`` or ``?tools=a,b``).

"The current view" is derived from the request URL's query, which the
transport stores per request (``set_mcp_tool_view_query``); the registry work
happens here, once per call. A direct call with no request (tests) uses the
core set; a URL whose selection is broken is reported, not papered over.
"""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from mcp.types import TextContent

from mcp_server.tools._definitions import get_tool_definitions
from mcp_server.tools._helpers import (
    _error_response,
    _success_response,
    get_mcp_tool_view_query,
)
from mcp_server.tools._profiles import CORE_TOOLS, ToolProfileError, tool_view_names

MAX_NAMES = 20
SUMMARY_CHARS = 160

URL_HINT: dict[str, str] = {
    "list_all": "?profile=full on the MCP URL lists every tool",
    "allowlist": "?tools=<name>,<name> lists exactly those (wins over profile)",
    "note": (
        "A profile only picks what tools/list shows; most clients let the model call "
        "listed tools only, so reconnect with one of these URLs to use a hidden tool."
    ),
}

# A sentence ends at ``. `` / ``! `` / ``? `` — except after the abbreviations
# the descriptions use mid-sentence (``e.g.``, ``i.e.``, ``vs.``, ``etc.``).
_SENTENCE_END = re.compile(r"(?<!\be\.g\.)(?<!\bi\.e\.)(?<!\bvs\.)(?<!\betc\.)(?<=[.!?])\s")


def summarize(description: str) -> str:
    """The first sentence of a description, cut to ``SUMMARY_CHARS`` on a word."""
    first = description.strip().splitlines()[0] if description.strip() else ""
    sentence = _SENTENCE_END.split(first, maxsplit=1)[0].strip()
    if len(sentence) <= SUMMARY_CHARS:
        return sentence
    cut = sentence[: SUMMARY_CHARS - 1].rsplit(" ", 1)[0]
    return cut + "…"


def current_view() -> tuple[frozenset[str], str | None]:
    """The names the request URL's ``tools/list`` returns, and the URL's error if any.

    No request (a direct handler call) → the core set. A broken selection
    (``?profile=typo``, ``?tools=`` matching nothing) → the core set plus the
    same message ``tools/list`` would fail with, so the caller is not told that
    core is in effect when the URL actually lists nothing.
    """
    query = get_mcp_tool_view_query()
    if query is None:
        return frozenset(CORE_TOOLS), None
    try:
        return tool_view_names(query), None
    except ToolProfileError as e:
        return frozenset(CORE_TOOLS), e.message


def hidden_tools(
    registry: list[dict[str, Any]], view: frozenset[str], query: str | None = None
) -> list[dict[str, str]]:
    """Tools outside ``view`` as ``{name, title, summary}``, registry order."""
    needle = (query or "").strip().lower()
    rows: list[dict[str, str]] = []
    for tool in registry:
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


def full_definitions(
    registry: list[dict[str, Any]], names: list[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    """The registry entries for ``names`` (registry order) and the names it does not know.

    Names are trimmed and de-duplicated first, the way ``?tools=`` is read.
    """
    requested = list(dict.fromkeys(n.strip() for n in names if n.strip()))
    by_name = {tool["name"]: tool for tool in registry}
    found = [tool for name, tool in by_name.items() if name in requested]
    unknown = [n for n in requested if n not in by_name]
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
    Either way ``url`` says how to list more, and ``url_error`` carries the
    message ``tools/list`` fails with when the URL's selection is broken.
    """
    names = args.get("names")
    query = args.get("query")
    if query is not None and not isinstance(query, str):
        return _error_response("validation_error", "query must be a string.")
    if names is not None and not isinstance(names, list):
        return _error_response(
            "validation_error", 'names must be a list of tool names, e.g. ["get_usage"].'
        )
    if names is not None and not all(isinstance(n, str) for n in names):
        return _error_response(
            "validation_error", 'names must be a list of tool names, e.g. ["get_usage"].'
        )
    if names is not None and len(names) > MAX_NAMES:
        return _error_response(
            "validation_error", f"names holds at most {MAX_NAMES} tools (got {len(names)})."
        )
    registry = get_tool_definitions()  # built once per call
    view, url_error = current_view()
    extra: dict[str, Any] = {"url": URL_HINT}
    if url_error:
        extra["url_error"] = url_error
    if names:  # an empty list means "list the hidden tools"
        found, unknown = full_definitions(registry, names)
        payload: dict[str, Any] = {"definitions": found, **extra}
        if unknown:
            payload["unknown"] = unknown
            payload["hint"] = "describe_tools() with no arguments lists every tool you can ask for."
        return _success_response(**payload)
    rows = hidden_tools(registry, view, query)
    return _success_response(
        tools=rows,
        count=len(rows),
        listed=sorted(view),
        hint=(
            'describe_tools(names=["<tool>"]) returns a tool\'s full schema; to use it, '
            "reconnect with a URL that lists it (see url)."
        ),
        **extra,
    )
