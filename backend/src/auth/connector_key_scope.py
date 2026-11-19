"""Where a connector key is accepted (#2929).

A connector key is the workspace API key a chat or ingestion connector's worker
holds: an ``api_keys`` row linked to its connector by ``connector_id``, or one
that carries the reserved ``connector:`` name without that link
(``auth.connector_keys``). Either is ``VerifiedKey.connector_scoped``.

A connector key authenticates exactly what a connector worker calls:

- the MCP tools in ``CONNECTOR_KEY_MCP_TOOLS`` (the MCP protocol methods
  around them, ``initialize``, ``ping``, notifications and a ``tools/list``
  that names only these tools, are available);
- the REST routes in ``CONNECTOR_KEY_REST_ROUTES``: none. No REST route that
  authenticates an API key accepts a connector key.

This module is the one source of both lists and of the decision. REST applies
it where a verified API key becomes a principal (``auth.dependencies``), on the
route template FastAPI matched; MCP applies it in the tool dispatcher before
anything else runs for the call (``mcp_server.tools``). Everything else about
the key (its owner, its workspace, the pause while its connector has no seat)
is decided in ``auth.api_keys``, not here.

``CONNECTOR_KEY_SCOPE`` (``Settings.connector_key_scope``) picks what happens
outside the lists: ``enforce`` (default) refuses with ``ConnectorKeyScopeError``
(403 ``AUTH-204``) and logs ``connector_key_scope_refused``; ``log`` serves the
request as if the rule did not exist and logs
``connector_key_scope_would_refuse``. Both events carry the surface, the target
(``"<METHOD> <route template>"`` or the tool name), the key's non-secret prefix
and its connector id, never the key.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final, Literal
from uuid import UUID

from config.settings import get_settings
from utils.exceptions import ConnectorKeyScopeError
from utils.logger import get_logger

logger = get_logger(__name__)

# (HTTP method, route template exactly as FastAPI registers it, prefixes
# included). Empty: no REST route accepts a connector key. An entry is a
# one-line change pinned by ``tests/auth/test_connector_key_scope.py`` and
# checked against the app's route table by the sweep test.
CONNECTOR_KEY_REST_ROUTES: Final[frozenset[tuple[str, str]]] = frozenset()

# The MCP tools the connector workers call (ingest, thread continuity, edits
# and deletions mirrored from the chat, answer citations, corpus steering).
CONNECTOR_KEY_MCP_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "recall",
        "remember",
        "create_edge",
        "reference",
        "update_memory",
        "list_edges",
        "delete_edge",
        "forget",
        "get_context_info",
    }
)

REFUSED_EVENT: Final = "connector_key_scope_refused"
WOULD_REFUSE_EVENT: Final = "connector_key_scope_would_refuse"

# What the target reads when a request matched no route.
UNMATCHED_ROUTE: Final = "(no matched route)"

_MESSAGES: Final[dict[str, str]] = {
    "rest": "This credential is not accepted on this route",
    "mcp": "This credential is not accepted for this tool",
}

ConnectorKeyScopeMode = Literal["enforce", "log"]
Surface = Literal["rest", "mcp"]


def connector_key_scope_mode() -> ConnectorKeyScopeMode:
    """The deployment's ``CONNECTOR_KEY_SCOPE`` (validated at startup)."""
    return get_settings().connector_key_scope


def connector_key_scope_enforced() -> bool:
    """Whether a connector key is refused outside the lists (``enforce``)."""
    return connector_key_scope_mode() == "enforce"


def connector_key_rest_allowed(method: str, route_path: str | None) -> bool:
    """Whether a connector key is accepted on ``method`` + ``route_path``.

    ``route_path`` is the template of the matched route (``matched_route_template``),
    never the request URL; ``None`` (no matched route) is never accepted, and
    no method is special-cased: only a listed (method, template) pair passes.
    """
    if route_path is None:
        return False
    return (method.upper(), route_path) in CONNECTOR_KEY_REST_ROUTES


def connector_key_mcp_tool_allowed(tool_name: object) -> bool:
    """Whether a connector key may call the MCP tool ``tool_name``."""
    return isinstance(tool_name, str) and tool_name in CONNECTOR_KEY_MCP_TOOLS


def matched_route_template(scope: Mapping[str, Any]) -> str | None:
    """The path template of the route this request matched, prefixes included.

    FastAPI includes routers lazily: ``scope["route"]`` is the route object as
    its own router declared it (``/memory/remember``), and the template with
    every include prefix (``/api/v1/memory/remember``) is the ``path`` of the
    effective route context FastAPI records for the request (with path
    converters as declared: ``{name:path}``). A route registered on the
    application itself has no such context; its own ``path`` is already the
    full template. A route of a mounted sub-application resolves to its path
    inside the mount, which no list entry names. ``None`` when no route
    matched. The sweep test pins the result for every route of the app.
    """
    fastapi_scope = scope.get("fastapi")
    if isinstance(fastapi_scope, Mapping):
        context = fastapi_scope.get("effective_route_context")
        template = getattr(context, "path", None)
        if isinstance(template, str) and template:
            return template
    route = scope.get("route")
    template = getattr(route, "path", None)
    return template if isinstance(template, str) and template else None


def rest_target(method: str, route_path: str | None) -> str:
    """The log target of a REST request: ``"<METHOD> <route template>"``."""
    return f"{method.upper()} {route_path or UNMATCHED_ROUTE}"


def check_connector_key_scope(
    *,
    surface: Surface,
    target: str,
    allowed: bool,
    key_prefix: str | None,
    connector_id: UUID | None,
) -> None:
    """Apply the connector key rule to one request of a connector key.

    The one place REST and MCP decide, so the two cannot drift: an allowed
    target returns silently; otherwise ``log`` mode writes one
    ``connector_key_scope_would_refuse`` event and returns, and ``enforce``
    writes one ``connector_key_scope_refused`` event and raises.

    Args:
        surface: ``rest`` or ``mcp``.
        target: ``"<METHOD> <route template>"`` (``rest_target``) or the tool name.
        allowed: The list decision for ``target``.
        key_prefix: The key's non-secret prefix (``VerifiedKey.key_prefix``).
        connector_id: The key's connector, when the key is linked to one.

    Raises:
        ConnectorKeyScopeError: 403 ``AUTH-204`` in ``enforce`` mode.
    """
    if allowed:
        return
    enforced = connector_key_scope_enforced()
    logger.warning(
        REFUSED_EVENT if enforced else WOULD_REFUSE_EVENT,
        surface=surface,
        target=target[:200],
        key_prefix=key_prefix,
        connector_id=str(connector_id) if connector_id is not None else None,
    )
    if enforced:
        raise ConnectorKeyScopeError(_MESSAGES[surface])
