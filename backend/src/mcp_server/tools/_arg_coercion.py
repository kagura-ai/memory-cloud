"""JSON-string argument coercion for MCP tool calls.

Issue #197 / #196: some MCP clients serialize complex arguments (arrays,
objects, booleans, or arbitrary JSON values) as JSON strings before sending
them over the wire:

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


def _coerce_to_any(value: Any) -> Any:
    """Decode JSON strings for schema fields that accept any JSON value."""
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return value


_COERCERS = {
    "array": _coerce_to_array,
    "object": _coerce_to_object,
    "boolean": _coerce_to_boolean,
}


def coerce_mcp_arguments(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Coerce MCP tool arguments to their declared JSON-schema types.

    Array / object / boolean fields are coerced to their declared type. Fields
    without a declared type accept any JSON value, so their strings are decoded
    without restricting the resulting type. Other declared types (string,
    number, integer) are left to pydantic to validate, since pydantic already
    coerces numeric strings when strict mode is off.

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
        if declared_type is None:
            coercer = _coerce_to_any
        elif isinstance(declared_type, str):
            coercer = _COERCERS.get(declared_type)
        else:
            continue
        if coercer is None:
            continue
        coerced[arg_name] = coercer(value)
    return coerced
