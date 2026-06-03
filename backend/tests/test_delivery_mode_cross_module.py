"""Cross-module delivery_mode invariants (#886).

The canonical source of truth is ``models.memory._ALL_DELIVERY_MODES``. Every
surface that enumerates the modes — the API request schemas' ``Literal`` and the
MCP tool ``enum`` lists — must equal that set, or a value accepted at one layer
is rejected at another. These cannot be expressed at the type-checker layer
(``Literal`` needs string literals, the MCP defs are plain dicts), so this
runtime test is the safety net (mirrors test_edge_type_constants.py).
"""

import typing

from models.memory import _ALL_DELIVERY_MODES
from models.schemas import RememberRequest, UpdateMemoryRequest

EXPECTED = frozenset(_ALL_DELIVERY_MODES)


def _literal_values(model, field_name):
    """Extract the set of literal values from a (possibly Optional) Literal field."""
    annotation = model.model_fields[field_name].annotation
    values: set[str] = set()
    # Optional[Literal[...]] is Union[Literal[...], None]; walk the args.
    for arg in typing.get_args(annotation) or (annotation,):
        if typing.get_origin(arg) is typing.Literal:
            values.update(typing.get_args(arg))
    # Bare Literal[...] (non-optional) case.
    if typing.get_origin(annotation) is typing.Literal:
        values.update(typing.get_args(annotation))
    return values


def test_remember_request_literal_matches_canonical_set():
    assert _literal_values(RememberRequest, "delivery_mode") == EXPECTED


def test_update_memory_request_literal_matches_canonical_set():
    assert _literal_values(UpdateMemoryRequest, "delivery_mode") == EXPECTED


def _tool(defs, name):
    return next(d for d in defs if d["name"] == name)


def test_mcp_remember_and_update_enums_match_canonical_set():
    from mcp_server.tools._definitions import get_tool_definitions

    defs = get_tool_definitions()
    for tool_name in ("remember", "update_memory"):
        props = _tool(defs, tool_name)["inputSchema"]["properties"]
        assert "delivery_mode" in props, f"{tool_name} missing delivery_mode property"
        assert frozenset(props["delivery_mode"]["enum"]) == EXPECTED


def test_mcp_load_pinned_tool_is_registered_and_read_only():
    from mcp_server.tools import _RATE_LIMIT_EXEMPT_TOOLS
    from mcp_server.tools._definitions import get_tool_definitions

    load_pinned = _tool(get_tool_definitions(), "load_pinned")
    assert load_pinned.get("readOnly") is True
    # Read-only + deterministic-every-turn ⇒ must be rate-limit exempt.
    assert "load_pinned" in _RATE_LIMIT_EXEMPT_TOOLS
