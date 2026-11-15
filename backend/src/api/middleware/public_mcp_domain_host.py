"""Host guard for verified custom domains of public MCP endpoints (#2809).

A verified custom domain is an alias of one public MCP endpoint: under the
alias name the application answers ``/mcp`` (and ``/mcp/``) as the endpoint's
fixed address ``/mcp/public/{public_id}`` does, and nothing else
(``docs/public-mcp-domains.md``).

``PublicMcpDomainHostGuard`` is a pure ASGI middleware registered last in
``api.main`` so that it wraps every other application middleware. Per
``http`` / ``websocket`` scope:

1. ``MCP_PUBLIC_DOMAINS_ENABLED`` off: the inner application is called at
   once and no header is read. While the switch is off the application applies
   no Host rule at all: a request under an alias name that still reaches it is
   answered like a request under any other Host (on ``/mcp``, the authenticated
   transport's 401). Alias routing is therefore removed at the proxy before
   the switch goes off or the release is rolled back.
2. More than one ``Host`` header: 400. No ``Host`` header: an internal caller.
   ``X-Forwarded-Host`` and ``Forwarded`` are never read.
3. An application host (from the settings) or an internal caller's shape
   (address literal, single label, pass-through name): the inner application,
   scope untouched.
4. Any other Host (a foreign one):

   - a websocket is closed before it is accepted (1008);
   - ``/mcp`` and ``/mcp/`` with a claimable name go to the public transport
     with ``public_endpoint_alias_host`` set to the normalised name and the
     path ``/mcp/public/``; a malformed or reserved name is the transport's
     404, without a lookup;
   - every other path and method is the same 404, without a database access,
     a cookie or a CORS header.

The guard opens no database session, keeps no list of names and logs nothing
about the request. Which names are served is decided per request by the
alias statement (``load_serving_endpoint_for_alias``) in the transport.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Receive, Scope, Send

from mcp_server.public import transport as public_transport
from mcp_server.public.domains import (
    claimable_hostname,
    classify_request_host,
    public_domains_enabled,
)
from mcp_server.transport import _send_json_error
from utils.public_hostname import host_from_header

_ALIAS_PATHS = frozenset({"/mcp", "/mcp/"})
_PUBLIC_ENDPOINT_PATH = "/mcp/public/"
_WS_POLICY_VIOLATION = 1008


class PublicMcpDomainHostGuard:
    """Route a foreign Host to the public transport on ``/mcp``, refuse it elsewhere."""

    def __init__(self, app: ASGIApp, public_app: ASGIApp | None = None) -> None:
        """Wrap ``app``.

        Args:
            app: The application.
            public_app: The public MCP transport; ``None`` uses
                ``mcp_server.public.transport.public_mcp_asgi_app`` (looked up
                per request).
        """
        self.app = app
        self._public_app = public_app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        kind = scope["type"]
        if kind not in ("http", "websocket") or not public_domains_enabled():
            await self.app(scope, receive, send)
            return

        hosts = [value for name, value in scope["headers"] if name.lower() == b"host"]
        if len(hosts) > 1:
            await self._refuse(kind, send, bad_request=True)
            return
        host = host_from_header(hosts[0]) if hosts else ""
        if classify_request_host(host) != "foreign":
            await self.app(scope, receive, send)
            return

        if kind == "http" and scope["path"] in _ALIAS_PATHS:
            name = claimable_hostname(host)
            if name is not None:
                alias_scope = dict(scope)
                alias_scope.pop(public_transport.PUBLIC_ID_SCOPE_KEY, None)
                alias_scope["path"] = _PUBLIC_ENDPOINT_PATH
                alias_scope[public_transport.ALIAS_HOST_SCOPE_KEY] = name
                await self._public(alias_scope, receive, send)
                return
        await self._refuse(kind, send, bad_request=False)

    async def _public(self, scope: Scope, receive: Receive, send: Send) -> None:
        public_app = self._public_app or public_transport.public_mcp_asgi_app
        await public_app(scope, receive, send)

    @staticmethod
    async def _refuse(kind: str, send: Send, *, bad_request: bool) -> None:
        if kind == "websocket":
            await send({"type": "websocket.close", "code": _WS_POLICY_VIOLATION})
            return
        if bad_request:
            await _send_json_error(send, 400, {"error": "bad_request"})
            return
        await _send_json_error(send, 404, public_transport.NOT_PUBLISHED_BODY)
