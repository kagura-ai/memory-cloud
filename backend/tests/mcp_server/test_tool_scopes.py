"""The OAuth scope every MCP tool needs on ``tools/call`` (#1686).

The rule lives in ``mcp_server.tools._scopes``: ``memory:read`` for tools whose
``readOnlyHint`` is true plus a pinned list of searches, ``memory:write`` for
everything else. The transport half (HTTP 403 + challenge on both eras) is in
``test_transport_oauth``.
"""

from __future__ import annotations

import pytest

from mcp_server.tools import get_tool_definitions
from mcp_server.tools._scopes import (
    READ_SCOPE,
    READ_SCOPE_TOOLS,
    TOOL_SCOPES,
    WRITE_SCOPE,
    challenge_scope,
    required_scope_for_tool,
)


def _definitions() -> dict[str, dict]:
    return {tool["name"]: tool for tool in get_tool_definitions()}


def test_every_listed_tool_resolves_to_read_or_write():
    definitions = _definitions()
    assert set(TOOL_SCOPES) == set(definitions)
    for name in definitions:
        assert required_scope_for_tool(name) in (READ_SCOPE, WRITE_SCOPE), name


def test_read_scope_is_the_read_only_hint_plus_the_pinned_exceptions():
    definitions = _definitions()
    read_only = {n for n, d in definitions.items() if d["annotations"]["readOnlyHint"] is True}
    reads = {n for n in definitions if required_scope_for_tool(n) == READ_SCOPE}
    assert reads == read_only | READ_SCOPE_TOOLS


def test_the_exception_list_is_pinned():
    """Searches whose only writes are ranking updates; each must still be a
    registered tool that is NOT read-only by annotation (else it is no exception)."""
    assert READ_SCOPE_TOOLS == {"recall", "get_agent_bootstrap"}
    definitions = _definitions()
    for name in READ_SCOPE_TOOLS:
        assert definitions[name]["annotations"]["readOnlyHint"] is False


@pytest.mark.parametrize(
    ("name", "scope"),
    [
        ("list_contexts", READ_SCOPE),
        ("get_context_info", READ_SCOPE),
        ("reference", READ_SCOPE),
        ("recall", READ_SCOPE),
        ("get_agent_bootstrap", READ_SCOPE),
        ("remember", WRITE_SCOPE),
        ("forget", WRITE_SCOPE),
        ("delete_context", WRITE_SCOPE),
        ("feedback", WRITE_SCOPE),
        ("secret_put", WRITE_SCOPE),
    ],
)
def test_classifications_a_reviewer_would_check(name, scope):
    assert required_scope_for_tool(name) == scope


@pytest.mark.parametrize("name", ["no_such_tool", "", None, 7, ["recall"]])
def test_unknown_or_malformed_names_need_write(name):
    """Fail closed: a name the table does not list needs the broader scope."""
    assert required_scope_for_tool(name) == WRITE_SCOPE


@pytest.mark.parametrize(
    ("granted", "required", "expected"),
    [
        ({"memory:read"}, "memory:write", "memory:read memory:write"),
        (
            {"offline_access", "memory:read", "verification:unknown"},
            "memory:write",
            "memory:read memory:write offline_access",
        ),
        (set(), "memory:read", "memory:read"),
        ({"memory:write"}, "memory:read", "memory:read memory:write"),
    ],
)
def test_challenge_scope_keeps_what_is_granted(granted, required, expected):
    """A client re-authorizes with exactly this value: naming only the missing
    scope would trade one scope for the other."""
    assert challenge_scope(frozenset(granted), required) == expected
