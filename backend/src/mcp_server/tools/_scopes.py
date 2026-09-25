"""The OAuth scope each MCP tool needs on ``tools/call`` (#1686).

One rule, derived from the annotations table (``_annotations.TOOL_ANNOTATIONS``):

* ``memory:read`` — every tool whose ``readOnlyHint`` is true, plus the
  ``READ_SCOPE_TOOLS`` exceptions: searches whose only side effect is learned
  ranking state (Hebbian edge weights, working → persistent promotion), which
  is why their hint is false.
* ``memory:write`` — every other tool, and any name the table does not list
  (fail closed).

Applies to OAuth access tokens only: API keys, agent-bound keys and session
cookies carry no OAuth scope and are not gated here. ``memory:delete`` and
``memory:admin`` are not separately required by any tool; deletes need
``memory:write`` like other writes, and every tool keeps its role checks.
``initialize``, ``tools/list``, ``ping`` and ``server/discover`` are not
scope-gated.
"""

from __future__ import annotations

from auth.mcp_scopes import ALL_ADVERTISED_SCOPES
from mcp_server.tools._annotations import TOOL_ANNOTATIONS

READ_SCOPE = "memory:read"
WRITE_SCOPE = "memory:write"

# Not read-only by annotation, but a read for authorization: their only writes
# are the ranking updates any query makes (``_errors._REPEAT_SAFE_WRITES``
# gives them read-style retry advice for the same reason).
READ_SCOPE_TOOLS: frozenset[str] = frozenset({"recall", "get_agent_bootstrap"})

TOOL_SCOPES: dict[str, str] = {
    name: READ_SCOPE if hints["readOnlyHint"] or name in READ_SCOPE_TOOLS else WRITE_SCOPE
    for name, hints in TOOL_ANNOTATIONS.items()
}


def required_scope_for_tool(tool_name: object) -> str:
    """The scope a ``tools/call`` for ``tool_name`` needs (client-supplied, may be anything)."""
    if isinstance(tool_name, str):
        return TOOL_SCOPES.get(tool_name, WRITE_SCOPE)
    return WRITE_SCOPE


def challenge_scope(granted: frozenset[str], required: str) -> str:
    """The ``scope`` of an ``insufficient_scope`` challenge: granted plus required.

    A client re-authorizes with exactly this value, so it keeps what the token
    already grants (MCP authorization, scope challenge handling): naming only
    the missing scope would trade ``memory:read`` for ``memory:write``. Scopes
    the server does not define are left out; the order is the advertised one.
    """
    wanted = (granted & set(ALL_ADVERTISED_SCOPES)) | {required}
    return " ".join(s for s in ALL_ADVERTISED_SCOPES if s in wanted)
