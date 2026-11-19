"""The connector key rule on the MCP endpoint (#2929).

A connector key calls the tools in ``auth.connector_key_scope.CONNECTOR_KEY_MCP_TOOLS``
and no other. The decision itself is ``check_connector_key_scope`` (one place for
REST and MCP); this module applies it to the three MCP surfaces that depend on the
credential:

- ``tools/call``: ``connector_key_tool_refusal`` runs first in the tool
  dispatcher, before the rate limit, the default context, the slot and capacity
  gates and the handler. A refused call is a tool result (``isError``), the
  ``permission_denied`` envelope a role refusal has, with ``error_code``
  ``AUTH-204``: the session stays usable for the listed tools.
- ``tools/list``: ``tools_for_credential`` keeps only the listed tools of the
  URL's view.
- ``initialize`` / ``server/discover``: ``connector_key_narrowed`` tells the
  instructions builder to serve the static base text, without the guardrail
  digest or the default context's name.

``CONNECTOR_KEY_SCOPE=log`` leaves ``tools/list`` and the instructions as they
were and dispatches every call, logging ``connector_key_scope_would_refuse``
for each call outside the list.
"""

from __future__ import annotations

from typing import Any

from auth.connector_key_scope import (
    check_connector_key_scope,
    connector_key_mcp_tool_allowed,
    connector_key_scope_enforced,
)
from mcp_server.auth import get_mcp_connector_scope
from mcp_server.tools._helpers import ToolErrorContent, _error_response
from utils.exceptions import ConnectorKeyScopeError

CONNECTOR_KEY_TOOL_HELP = (
    "This connection authenticates with a connector key, which calls only the tools a "
    "connector worker uses (see tools/list). Use an ordinary API key or sign in for "
    "other tools."
)


def connector_key_narrowed() -> bool:
    """Whether this request's credential is a connector key under ``enforce``."""
    return get_mcp_connector_scope() is not None and connector_key_scope_enforced()


def connector_key_tool_refusal(tool_name: object) -> ToolErrorContent | None:
    """The refusal for a ``tools/call`` a connector key may not make, else ``None``.

    ``None`` for every other credential, for a listed tool, and in ``log``
    mode (after its one log event).
    """
    scoped = get_mcp_connector_scope()
    if scoped is None:
        return None
    try:
        check_connector_key_scope(
            surface="mcp",
            target=str(tool_name)[:100],
            allowed=connector_key_mcp_tool_allowed(tool_name),
            key_prefix=scoped.key_prefix,
            connector_id=scoped.connector_id,
        )
    except ConnectorKeyScopeError as exc:
        return _error_response(
            "permission_denied",
            exc.message,
            error_code=exc.error_code,
            help=CONNECTOR_KEY_TOOL_HELP,
        )
    return None


def tools_for_credential(tools: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    """The ``tools/list`` entries this request's credential may call.

    Returns:
        ``(tools, narrowed)``: for a connector key under ``enforce`` only the
        listed tools of the view, and ``narrowed=True`` (the list then depends
        on the credential); otherwise ``tools`` unchanged and ``False``.
    """
    if not connector_key_narrowed():
        return tools, False
    return [tool for tool in tools if connector_key_mcp_tool_allowed(tool.get("name"))], True
