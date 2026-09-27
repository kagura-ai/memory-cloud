"""JSON-string argument coercion for MCP tool calls.

Issue #197 / #196: some MCP clients serialize complex arguments (arrays,
objects, booleans) as JSON strings before sending them over the wire:

    {"tags": "[\"a\", \"b\"]"}      instead of  {"tags": ["a", "b"]}
    {"is_private": "true"}           instead of  {"is_private": true}
    {"filters": "{\"type\": ...}"}   instead of  {"filters": {...}}

The server-side pydantic models reject these as validation errors even
though the intent is obvious. This module coerces such values back to
their declared types using the tool's own JSON schema, so both
well-behaved and quirky clients work.

Coercion is best-effort and non-destructive: if a value is already of
the declared type it is passed through unchanged, and if a string cannot
be decoded the original value is kept so pydantic can produce a proper
error message.
"""

from __future__ import annotations

import difflib
import json
from typing import Any

from mcp_server.tools._definitions import get_tool_definitions

_STRING_TRUTHY = frozenset({"true", "1", "yes", "on"})
_STRING_FALSY = frozenset({"false", "0", "no", "off"})


def _build_tool_schemas() -> dict[str, dict[str, dict]]:
    """Build {tool_name: {arg_name: schema}} index from the static tool definitions."""
    schemas: dict[str, dict[str, dict]] = {}
    for tool in get_tool_definitions():
        name = tool.get("name")
        props = tool.get("inputSchema", {}).get("properties", {})
        if name and isinstance(props, dict):
            schemas[name] = props
    return schemas


# Tool definitions are static literals, so build the schema index once at import
# time rather than memoizing a lookup function (lru_cache would hide staleness
# across test sessions that patch get_tool_definitions).
_TOOL_SCHEMAS: dict[str, dict[str, dict]] = _build_tool_schemas()

# #1742: tools whose inputSchema sets ``additionalProperties: false`` — every
# tool today (``test_tool_schema_policy.py``). An argument outside their
# ``properties`` is refused instead of silently dropped.
_CLOSED_TOOLS: frozenset[str] = frozenset(
    tool["name"]
    for tool in get_tool_definitions()
    if tool.get("name") and tool.get("inputSchema", {}).get("additionalProperties") is False
)

# Argument names a tool still accepts although its schema no longer
# advertises them: deliberate, deprecated aliases the handler reads.
_ACCEPTED_ALIASES: dict[str, frozenset[str]] = {
    # #990: renamed to source_context_id / target_context_id; the old names
    # stay accepted for the SDK (see ``handle_merge_contexts``).
    "merge_contexts": frozenset({"source_id", "target_id"}),
}

# Accepted on every tool and ignored: MCP request metadata some clients also
# put inside ``arguments``.
_ALWAYS_ACCEPTED: frozenset[str] = frozenset({"_meta"})

# Bounds on the client-controlled names echoed back in the refusal.
_MAX_UNKNOWN_SHOWN = 10
_MAX_NAME_CHARS = 64


def _coerce_to_array(value: Any) -> Any:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            return value
        return decoded if isinstance(decoded, list) else value
    return value


def _coerce_to_object(value: Any) -> Any:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            return value
        return decoded if isinstance(decoded, dict) else value
    return value


def _coerce_to_boolean(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _STRING_TRUTHY:
            return True
        if lowered in _STRING_FALSY:
            return False
    return value


def _coerce_any(value: Any) -> Any:
    """Best-effort decode for schema fields declared WITHOUT a scalar ``type``.

    #1322: quirky clients JSON-stringify complex values for typeless fields
    too — ``set_state``'s ``value`` arrived as ``"{\\"phase\\": ...}"`` and was
    stored verbatim into JSONB, breaking the documented round-trip. Decode
    strings ONLY when they parse to an object or array: those are
    unambiguously stringified structures. Scalars stay as sent — retyping
    ``"42"``→``42`` / ``"true"``→``True`` would corrupt legitimately-string
    values (zip codes, string ids) and diverge from the REST path, which
    stores exactly what the client sent (review finding on #1322).
    """
    if not isinstance(value, str):
        return value
    try:
        decoded = json.loads(value)
    except (ValueError, TypeError):
        return value
    return decoded if isinstance(decoded, (dict, list)) else value


_COERCERS = {
    "array": _coerce_to_array,
    "object": _coerce_to_object,
    "boolean": _coerce_to_boolean,
}


def coerce_mcp_arguments(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Coerce MCP tool arguments to their declared JSON-schema types.

    Only array / object / boolean fields are coerced — these are the types
    that MCP clients have been observed to serialize as JSON strings. Other
    types (string, number, integer) are left to pydantic to validate, since
    pydantic already coerces numeric strings when strict mode is off.

    When any coercion happens the returned dict is a shallow copy of the
    input: top-level keys can be replaced safely but mutable values that
    were passed through unchanged remain aliased to the caller's originals.
    When no coercion applies (falsy arguments, unknown tool, no coercible
    fields) the original object is returned unchanged to avoid allocating
    on every tool call — callers must not rely on identity to detect
    coercion.

    Args:
        tool_name: MCP tool name (e.g. "remember", "create_context").
        arguments: Raw arguments dict from the MCP request.

    Returns:
        Arguments with values coerced where applicable, or the original
        object if there is nothing to coerce. Unknown tools and unknown
        argument names pass through untouched.
    """
    if not arguments:
        return arguments

    props = _TOOL_SCHEMAS.get(tool_name)
    if not props:
        return arguments

    coerced: dict[str, Any] = dict(arguments)
    for arg_name, value in arguments.items():
        schema = props.get(arg_name)
        if not schema:
            continue
        declared_type = schema.get("type")
        if not isinstance(declared_type, str):
            # Typeless ("any") field — e.g. set_state's ``value`` (#1322).
            coerced[arg_name] = _coerce_any(value)
            continue
        coercer = _COERCERS.get(declared_type)
        if coercer is None:
            continue
        coerced[arg_name] = coercer(value)
    return coerced


def _shown_name(name: str) -> str:
    return repr(name[:_MAX_NAME_CHARS] + ("…" if len(name) > _MAX_NAME_CHARS else ""))


def find_unknown_arguments(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
    """Describe the arguments a closed tool schema does not declare (#1742).

    Args:
        tool_name: MCP tool name.
        arguments: The call's arguments.

    Returns:
        ``None`` when every argument is declared (or the tool's schema is
        open or unknown); otherwise ``{"message", "unknown_arguments",
        "allowed_arguments", "suggestions"}`` for an ``invalid_argument``
        envelope. ``suggestions`` maps an unknown name to its closest declared
        name (difflib) when one is close enough.
    """
    if not arguments or tool_name not in _CLOSED_TOOLS:
        return None
    props = _TOOL_SCHEMAS.get(tool_name, {})
    accepted = props.keys() | _ACCEPTED_ALIASES.get(tool_name, frozenset()) | _ALWAYS_ACCEPTED
    unknown = sorted(str(name) for name in arguments if name not in accepted)
    if not unknown:
        return None

    allowed = sorted(props)
    suggestions: dict[str, str] = {}
    parts: list[str] = []
    for name in unknown[:_MAX_UNKNOWN_SHOWN]:
        close = difflib.get_close_matches(name, allowed, n=1, cutoff=0.6)
        shown = _shown_name(name)
        if close:
            suggestions[name[:_MAX_NAME_CHARS]] = close[0]
            parts.append(f"{shown} (did you mean '{close[0]}'?)")
        else:
            parts.append(shown)
    more = len(unknown) - _MAX_UNKNOWN_SHOWN
    listed = ", ".join(parts) + (f" and {more} more" if more > 0 else "")
    noun = "argument" if len(unknown) == 1 else "arguments"
    return {
        "message": (
            f"{tool_name} does not accept the {noun} {listed}. "
            f"Accepted arguments: {', '.join(allowed)}."
        ),
        "unknown_arguments": [n[:_MAX_NAME_CHARS] for n in unknown[:_MAX_UNKNOWN_SHOWN]],
        "allowed_arguments": allowed,
        "suggestions": suggestions,
    }
