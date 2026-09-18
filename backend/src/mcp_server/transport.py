"""MCP over Streamable HTTP transport.

Implements MCP Streamable HTTP transport (spec 2025-03-26) for remote client connections.
Issue #248: SSE transport removed (deprecated in MCP spec 2025-03-26).
Issue #1544: dual-era — requests carrying MCP 2026-07-28 per-request ``_meta``
are handed to ``mcp_server.transport_stateless`` before any session handling.
"""

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

from starlette.responses import Response
from starlette.types import Receive, Scope, Send

from config.constants import APP_VERSION
from config.settings import get_settings
from mcp_server.auth import authenticate_mcp_request
from mcp_server.session import get_session_manager

if TYPE_CHECKING:
    from mcp_server.session import MCPSession

logger = logging.getLogger(__name__)


def _sanitize_challenge_attr_value(value: str) -> str:
    """Sanitize a string before interpolating it into an RFC 6750
    ``WWW-Authenticate: Bearer ...`` quoted-attribute value.

    The ``error_description`` attribute can echo back malformed-token bytes,
    and a stray ``"`` or CR/LF would close the quoted attribute early or
    split the response (CWE-93 / response splitting). Strip the three
    characters that would break the header — leaving the JSON body to carry
    the unsanitized description for client logging.
    """
    return value.replace("\r", " ").replace("\n", " ").replace('"', "'")


async def _send_json_error(
    send: Send,
    status: int,
    payload: dict[str, Any],
    extra_headers: list[list[bytes]] | None = None,
) -> None:
    """Send a JSON error response on the raw ASGI ``send`` channel (#1456).

    ``mcp_asgi_app`` and its POST handler had nine copies of the same
    ``json.dumps(...).encode()`` → ``http.response.start`` →
    ``http.response.body`` sequence, each one a place a status code or a
    ``content-type`` could quietly drift from its siblings.

    Args:
        send: ASGI send callable.
        status: HTTP status code.
        payload: JSON-serializable body.
        extra_headers: Appended after ``content-type`` — the 401 path needs
            ``www-authenticate``.
    """
    headers: list[list[bytes]] = [[b"content-type", b"application/json"]]
    if extra_headers:
        headers.extend(extra_headers)
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": json.dumps(payload).encode("utf-8")})


def _extract_session_id(
    method: str, path: str, headers: dict[bytes, bytes], query_string: bytes
) -> str | None:
    """Resolve the MCP session id from the request (#1456 extract).

    Priority is unchanged: URL path, then the ``mcp-session-id`` header, then a
    ``session_id`` query parameter. The path form only applies to the legacy
    ``POST /mcp/messages/{session_id}/`` shape.

    Returns:
        The session id, or ``None`` when the request carries none.
    """
    if method == "POST" and path.startswith("/mcp/messages/"):
        # /mcp/messages/mcp-xxx/ -> mcp-xxx
        path_parts = path.strip("/").split("/")
        if len(path_parts) >= 3:  # ['mcp', 'messages', 'session_id']
            logger.info(f"MCP session_id from path: {path_parts[2]}")
            return path_parts[2]

    session_id_header = headers.get(b"mcp-session-id")
    if session_id_header:
        session_id = session_id_header.decode("utf-8")
        logger.info(f"MCP session_id from header: {session_id}")
        return session_id

    query = query_string.decode("utf-8")
    if "session_id=" in query:
        for param in query.split("&"):
            if param.startswith("session_id="):
                session_id = param.split("=", 1)[1]
                logger.info(f"MCP session_id from query: {session_id}")
                return session_id

    return None


def _normalize_mcp_path(path: str) -> str:
    """Strip the ``/mcp`` mount prefix for the downstream handlers (#1456)."""
    if path.startswith("/mcp/"):
        return path[4:]
    if path == "/mcp":
        return "/"
    return path


async def _get_user_workspace_id(user_id: str) -> "UUID | None":
    """Get user's current workspace ID.

    Issue #146: Helper to fetch workspace_id for workspace-scoped API keys.

    Args:
        user_id: User ID (varchar, e.g., Google OAuth2 ID)

    Returns:
        Workspace UUID or None if user has no current workspace
    """
    from sqlalchemy import select

    from db.base import get_db
    from models.auth import User

    try:
        async for db in get_db():
            result = await db.execute(
                select(User.current_workspace_id).where(User.user_id == user_id)
            )
            workspace_id = result.scalar_one_or_none()
            return workspace_id
    except Exception as e:
        logger.warning(f"Failed to get user workspace_id: {e}")
        return None


# This is a dual-era server (#1544). MCP 2026-07-28 splits revisions in two:
#
# - *legacy* — an ``initialize`` handshake opens a session (``Mcp-Session-Id``).
#   2024-11-05 is what ``initialize`` negotiates by default, 2025-03-26 is the
#   Streamable HTTP revision ``handle_streamable_http_post`` implements.
# - *modern* — no handshake and no session: every request carries its protocol
#   version in ``params._meta`` and is served statelessly by
#   ``mcp_server.transport_stateless``.
#
# The two sets stay separate because they are reachable through different
# doors: ``initialize`` must never echo a modern revision (there is no
# session-scoped 2026-07-28), and per-request ``_meta`` must never select a
# legacy one. Only ``server/discover`` and the ``-32022`` error list both.
#
# Listing legacy revisions alone was the #1544 bug: a ``DiscoverResult`` is
# itself a modern signal, so a dual-era client stopped falling back to
# ``initialize`` and a modern-only client (ChatGPT) found no version to speak.
MODERN_PROTOCOL_VERSIONS: tuple[str, ...] = ("2026-07-28",)
LEGACY_PROTOCOL_VERSIONS: tuple[str, ...] = ("2025-03-26", "2024-11-05")
SUPPORTED_PROTOCOL_VERSIONS: tuple[str, ...] = MODERN_PROTOCOL_VERSIONS + LEGACY_PROTOCOL_VERSIONS

# What ``initialize`` answers when the client asks for a revision we do not
# list (or for none): the spec lets the server reply with another version it
# supports, and this is what every existing client has been negotiating.
DEFAULT_PROTOCOL_VERSION = "2024-11-05"

# ``params._meta`` key that carries the per-request protocol version. Its
# presence is what marks a request as modern (see ``_is_modern_request``).
PROTOCOL_VERSION_META_KEY = "io.modelcontextprotocol/protocolVersion"

# ``server/discover`` caching hint (MCP 2026-07-28 caching utility): the result
# is static per deployment, identical for every user → public, one hour.
DISCOVER_TTL_MS = 60 * 60 * 1000

SERVER_INFO = {"name": "kagura-memory-cloud", "version": APP_VERSION}
SERVER_CAPABILITIES: dict[str, dict] = {"tools": {}}
SERVER_INSTRUCTIONS = (
    "Kagura Memory Cloud: persistent memory for AI agents. Call list_contexts "
    "first to discover context IDs, then remember / recall / explore within a "
    "context. All tools take context_id explicitly."
)


def _discover_result() -> dict:
    """Build the ``server/discover`` result (MCP 2026-07-28 DiscoverResult).

    Issue #1541: ChatGPT sends this before anything else. The result carries the
    same identity and capabilities as ``initialize`` plus the versions we
    support and the MUST caching hints (``ttlMs`` >= 0, ``cacheScope``).

    Issue #1544: ``supportedVersions`` lists both eras — the modern revision a
    per-request client continues with, and the legacy ones an ``initialize``
    client negotiates.
    """
    return {
        "resultType": "complete",
        "supportedVersions": list(SUPPORTED_PROTOCOL_VERSIONS),
        "capabilities": SERVER_CAPABILITIES,
        "_meta": {"io.modelcontextprotocol/serverInfo": SERVER_INFO},
        "instructions": SERVER_INSTRUCTIONS,
        "ttlMs": DISCOVER_TTL_MS,
        "cacheScope": "public",
    }


def _is_modern_request(body: Any, headers: dict[bytes, bytes]) -> bool:
    """Decide which era serves a ``POST /mcp`` body (#1544).

    Per MCP 2026-07-28 a dual-era server "selects its behavior from how the
    client opens": per-request ``_meta`` → stateless, ``initialize`` → legacy.

    The ``MCP-Protocol-Version`` header alone counts only when it names a
    *modern* revision. Legacy clients (2025-06-18 and later) send that header
    too, with their negotiated legacy value, and must keep their session. A
    body that did not parse to a JSON object stays legacy as well: that path
    already answers it with ``-32700`` / ``-32600``.

    Args:
        body: The decoded JSON body, or ``None`` when it did not parse.
        headers: Request headers (lower-cased byte names, as ASGI delivers).

    Returns:
        True when the stateless 2026-07-28 handler must serve the request —
        including a request for a modern-looking version we do not support,
        which has to reach that handler to be answered with ``-32022``.
    """
    if not isinstance(body, dict) or body.get("method") == "initialize":
        return False

    params = body.get("params")
    meta = params.get("_meta") if isinstance(params, dict) else None
    if isinstance(meta, dict) and PROTOCOL_VERSION_META_KEY in meta:
        return True

    header = headers.get(b"mcp-protocol-version")
    return header is not None and header.decode("latin-1").strip() in MODERN_PROTOCOL_VERSIONS


async def _read_body(receive: Receive) -> bytes:
    """Drain the ASGI request body."""
    body_bytes = b""
    while True:
        message = await receive()
        if message["type"] == "http.request":
            body_bytes += message.get("body", b"")
            if not message.get("more_body", False):
                break
        elif message["type"] == "http.disconnect":
            break
    return body_bytes


def _replay_receive(body_bytes: bytes) -> Receive:
    """Build a ``receive`` that replays an already-drained body (#1544).

    ``mcp_asgi_app`` has to read the body to pick the era before it decides
    whether a session is involved at all; the legacy handler still expects to
    read it from ``receive``.
    """
    delivered = False

    async def receive() -> dict[str, Any]:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body_bytes, "more_body": False}
        return {"type": "http.disconnect"}

    return receive


async def _send_jsonrpc_result(
    send: Send, session: "MCPSession", request_id: Any, result: dict
) -> None:
    """Send a JSON-RPC success response for the Streamable HTTP session."""
    response = {"jsonrpc": "2.0", "id": request_id, "result": result}
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [
                [b"content-type", b"application/json"],
                [b"mcp-session-id", session.session_id.encode()],
            ],
        }
    )
    await send({"type": "http.response.body", "body": json.dumps(response).encode()})


async def handle_streamable_http_post(
    scope: Scope,
    receive: Receive,
    send: Send,
    session: "MCPSession",
    headers: dict,
) -> None:
    """Handle POST /mcp request (Streamable HTTP Transport).

    Implements MCP Specification 2025-03-26 unified endpoint.

    Args:
        scope: ASGI scope
        receive: ASGI receive
        send: ASGI send
        session: MCP session
        headers: Request headers dict
    """
    body_bytes = await _read_body(receive)

    try:
        body = json.loads(body_bytes.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.error(f"MCP POST invalid JSON: {e}")
        await _send_json_error(
            send,
            400,
            {
                "jsonrpc": "2.0",
                "error": {"code": -32700, "message": "Parse error"},
                "id": None,
            },
        )
        return

    # A JSON-RPC message is a single object. A scalar body used to raise
    # TypeError on the membership test below (HTTP 500), and a batch array
    # passed it by list membership and was 202'd as a "notification", leaving
    # the client waiting for a response that never came (#1541 review).
    # Batching is not supported by this transport.
    if not isinstance(body, dict):
        await _send_json_error(
            send,
            400,
            {
                "jsonrpc": "2.0",
                "error": {
                    "code": -32600,
                    "message": (
                        "Invalid Request: expected a single JSON-RPC object "
                        "(batch arrays are not supported)"
                    ),
                },
                "id": None,
            },
        )
        return

    method = body.get("method")
    request_id = body.get("id")  # None for a notification

    # Every JSON-RPC message from the client — request or notification — must
    # name its method. This runs BEFORE the notification short-circuit: checked
    # after it, a malformed envelope with no ``id`` (``{}``, ``{"method": 5}``)
    # was silently 202'd as a "notification" and never reached the guard
    # (#1541 review). Without it, -32601 "Method not found: None" would also
    # blame a method the client never sent.
    if not isinstance(method, str) or not method:
        await _send_json_error(
            send,
            400,
            {
                "jsonrpc": "2.0",
                "error": {"code": -32600, "message": "Invalid Request: missing method"},
                "id": request_id,
            },
        )
        return

    # A valid notification (string method, no "id") expects no response.
    if "id" not in body:
        shown = method[:100]
        logger.info(f"MCP notification: method={shown!r}, session={session.session_id}")
        await send({"type": "http.response.start", "status": 202, "headers": []})
        await send({"type": "http.response.body", "body": b""})
        return

    # Handle initialize request
    if method == "initialize":
        logger.info(f"MCP initialize (Streamable HTTP): session={session.session_id}")

        # Echo the requested revision when it is a legacy one we advertise
        # through server/discover, so the two surfaces cannot contradict each
        # other; anything else negotiates down to the default, as before
        # (#1541). A modern revision is never echoed: 2026-07-28 has no
        # handshake, so there is no session-scoped form of it to agree on
        # (#1544).
        params = body.get("params")
        requested = params.get("protocolVersion") if isinstance(params, dict) else None
        protocol_version = (
            requested if requested in LEGACY_PROTOCOL_VERSIONS else DEFAULT_PROTOCOL_VERSION
        )

        # Session ID is returned in the mcp-session-id header
        await _send_jsonrpc_result(
            send,
            session,
            request_id,
            {
                "protocolVersion": protocol_version,
                "capabilities": SERVER_CAPABILITIES,
                "serverInfo": SERVER_INFO,
            },
        )
        return

    # Handle tools/list request
    elif method == "tools/list":
        logger.info(f"MCP tools/list (Streamable HTTP): session={session.session_id}")

        from mcp_server.tools import get_tool_definitions

        tools = get_tool_definitions()
        await _send_jsonrpc_result(send, session, request_id, {"tools": tools})
        return

    # Handle tools/call request
    elif method == "tools/call":
        logger.info(f"MCP tools/call (Streamable HTTP): session={session.session_id}")

        try:
            # Extract tool parameters
            params = body.get("params", {})
            tool_name = params.get("name")
            arguments = params.get("arguments", {})

            logger.info(f"MCP calling tool: {tool_name}")

            # Call the tool through extracted helper function
            from mcp_server.tools import execute_tool_call

            # Issue #245: context_id is now in arguments, not session
            result = await execute_tool_call(
                tool_name=tool_name,
                arguments=arguments,
                user_id=session.user_id,
                workspace_id=session.workspace_id,  # Issue #204: Pass workspace_id for workspace info
            )

            await _send_jsonrpc_result(
                send,
                session,
                request_id,
                {"content": [{"type": item.type, "text": item.text} for item in result]},
            )
            return

        except Exception as e:
            # Issue #163: Improved error response with custom error codes
            error_type = type(e).__name__
            logger.error(f"MCP tools/call failed: {error_type}: {e}", exc_info=True)

            # Determine appropriate JSON-RPC error code
            # Standard codes: https://www.jsonrpc.workspace/specification#error_object
            # Custom codes: -32001 to -32099 (reserved for implementation)
            if isinstance(e, asyncio.TimeoutError):
                error_code = -32001  # Custom: Tool execution timeout
                error_message = "Tool execution timeout"
            elif isinstance(e, PermissionError):
                error_code = -32002  # Custom: Permission denied
                error_message = str(e)
            elif isinstance(e, ValueError):
                error_code = -32602  # Standard: Invalid params
                error_message = str(e)
            else:
                error_code = -32603  # Standard: Internal error
                error_message = f"Internal error: {str(e)}"

            error_response = {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": error_code,
                    "message": error_message,
                    "data": {
                        "exception_type": error_type,
                        "details": str(e)[:500],  # Truncate for safety
                    },
                },
            }
            error_body = json.dumps(error_response).encode()

            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [[b"content-type", b"application/json"]],
                }
            )
            await send({"type": "http.response.body", "body": error_body})
            return

    # Issue #1541: everything below used to fall through to a "legacy
    # transport" path that dereferenced ``session.transport`` — an attribute
    # MCPSession has not had since #248 (SSE removal) — so ANY other
    # id-bearing request was a guaranteed AttributeError → HTTP 500. ChatGPT
    # opens every session with ``server/discover`` (MCP 2026-07-28) and hit
    # it on each connector registration. Notifications never reach here (202
    # above); requests are answered explicitly, and the unknown case is a
    # JSON-RPC ``-32601`` instead of a crash.

    # Handle ping request (spec utility; MUST answer with an empty result)
    elif method == "ping":
        logger.info(f"MCP ping (Streamable HTTP): session={session.session_id}")
        await _send_jsonrpc_result(send, session, request_id, {})
        return

    # Handle server/discover request (MCP 2026-07-28 discovery). A conforming
    # client sends it with per-request ``_meta`` and is served by the stateless
    # handler; only a bare probe without that metadata lands here (#1544).
    elif method == "server/discover":
        logger.info(f"MCP server/discover (Streamable HTTP): session={session.session_id}")
        await _send_jsonrpc_result(send, session, request_id, _discover_result())
        return

    # Unknown / unimplemented request method → -32601 Method not found.
    # HTTP 200 + JSON-RPC error, like the tools/call error path above: this
    # handler is the legacy (initialize-handshake) half of the server, and the
    # 404 + -32601 shape belongs to the *modern* (2026-07-28) contract, which
    # ``transport_stateless`` answers for requests carrying modern ``_meta``.
    # ``method`` is client-supplied and unbounded: repr() + a length cap keep a
    # crafted value from forging log lines or bloating the echoed message.
    shown_method = method[:100]
    logger.info(
        f"MCP unknown method (Streamable HTTP): method={shown_method!r}, "
        f"session={session.session_id}"
    )
    await _send_json_error(
        send,
        200,
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"Method not found: {shown_method}"},
        },
        [[b"mcp-session-id", session.session_id.encode()]],
    )


async def handle_streamable_http_get(
    scope: Scope,
    receive: Receive,
    send: Send,
    session: "MCPSession",
    headers: dict,
) -> None:
    """Handle GET /mcp request (Optional SSE stream for notifications).

    Args:
        scope: ASGI scope
        receive: ASGI receive
        send: ASGI send
        session: MCP session
        headers: Request headers dict
    """
    logger.info(f"MCP GET stream (Streamable HTTP): session={session.session_id}")

    # Get Last-Event-ID for resumability
    last_event_id = headers.get(b"last-event-id")
    if last_event_id:
        last_event_id = last_event_id.decode()
        logger.info(f"MCP GET stream resuming from: {last_event_id}")

    # Start SSE stream
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [
                [b"content-type", b"text/event-stream"],
                [b"cache-control", b"no-cache"],
                [b"connection", b"keep-alive"],
                [b"mcp-session-id", session.session_id.encode()],
            ],
        }
    )

    # Send initial connection event
    await send(
        {
            "type": "http.response.body",
            "body": b"event: connected\ndata: {}\n\n",
        }
    )

    # TODO: Implement server-initiated notification streaming
    # For now, just keep the connection open

    # Close stream after timeout or client disconnect
    # (In production, this would listen for server notifications)


async def mcp_asgi_app(scope: Scope, receive: Receive, send: Send) -> None:
    """ASGI app for MCP over Streamable HTTP with multi-client support.

    Issue #248: SSE transport removed (deprecated in MCP spec 2025-03-26).

    Supports (Streamable HTTP spec 2025-03-26):
    - POST /mcp: MCP endpoint (tools, prompts, resources)
    - GET /mcp: Optional streaming endpoint

    Multi-Client Architecture:
        Each client gets an isolated MCP session with its own MCP Server instance.

    Authentication:
        - Authorization header: Bearer {api_key} (recommended)
        - If no auth: uses "default_user" (local development only)

    Session Management:
        - Session ID from mcp-session-id header (or auto-generated)
        - Sessions isolated per user_id + session_id
        - Inactive sessions cleaned up after timeout

    Args:
        scope: ASGI scope dict
        receive: ASGI receive callable
        send: ASGI send callable
    """
    method = scope.get("method", "UNKNOWN")
    path = scope.get("path", "")

    logger.info(f"MCP request: {method} {path}")

    # Extract headers
    headers = dict(scope.get("headers", []))

    # Debug: Log all headers
    logger.debug(f"MCP headers: {headers}")

    # Authenticate request (Issue #155: Support both Authorization header and session cookie)
    auth_header = headers.get(b"authorization")
    cookie_header = headers.get(b"cookie")

    # Debug: Log auth sources
    logger.debug(f"MCP auth_header: {auth_header}")
    logger.debug(f"MCP cookie_header: {cookie_header is not None}")

    try:
        # Try Authorization header first, then session cookie
        # Issue #245: context_id removed from auth (now required in tool args)
        user_id, _, api_key_workspace_id = await authenticate_mcp_request(
            authorization_header=auth_header,
            cookie_header=cookie_header,  # Issue #155: Add session cookie support
        )

        # Issue #963: record the PURE API-key workspace scope (None unless this
        # request is authenticated with a workspace-scoped API key) so the
        # context-resolution chokepoints can confine such a key to its workspace.
        # Must use api_key_workspace_id, NOT the conflated workspace_id below —
        # the latter becomes the user's *current* workspace for OAuth2/session/
        # global-key auth and would over-confine those (non-key-scoped) callers.
        from mcp_server.tools._helpers import set_mcp_key_workspace_scope

        set_mcp_key_workspace_scope(api_key_workspace_id)

        # RFC-0002 P0-4 (#1277): parse W3C traceparent + baggage into the
        # per-request correlation contextvar at the same auth seam (sibling of
        # the #963 key-workspace scope). Advisory-only — never fails the
        # request; missing/invalid headers → server-generated trace/span.
        from api.correlation import build_correlation_from_headers, set_correlation

        def _hdr(name: bytes) -> str | None:
            raw = headers.get(name)
            return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw

        try:
            set_correlation(
                build_correlation_from_headers(
                    traceparent=_hdr(b"traceparent"),
                    baggage=_hdr(b"baggage"),
                    surface="mcp",
                )
            )
        except Exception:  # pragma: no cover - defensive; correlation is advisory
            set_correlation(None)

        # Issue #169: For workspace-scoped API keys, use workspace_id from key; otherwise get from user
        if api_key_workspace_id:
            workspace_id = api_key_workspace_id
        else:
            workspace_id = await _get_user_workspace_id(user_id)

        logger.info(f"MCP authenticated: user={user_id}, workspace={workspace_id}")

        # Issue #XXX: Validate workspace_id from URL matches API key workspace
        # This enables workspace-scoped URLs for Claude Desktop multi-workspace support
        workspace_id_from_url = scope.get("workspace_id_from_url")
        if workspace_id_from_url:
            if api_key_workspace_id:
                # API Key authentication: Strict validation required
                if workspace_id is None or str(workspace_id) != workspace_id_from_url:
                    logger.warning(
                        f"MCP workspace mismatch (API Key): url={workspace_id_from_url}, key={workspace_id}"
                    )
                    await _send_json_error(
                        send,
                        403,
                        {
                            "error": "workspace_mismatch",
                            "error_description": "API key workspace does not match URL workspace. "
                            "Use an API key scoped to this workspace.",
                        },
                    )
                    return
            else:
                # OAuth2 authentication: Allow workspace switching if user is a member
                try:
                    from uuid import UUID

                    from db.base import get_db

                    workspace_uuid = UUID(workspace_id_from_url)

                    # Check if user is a member of the workspace
                    async for db in get_db():
                        from sqlalchemy import select

                        from models.auth import WorkspaceMember

                        result = await db.execute(
                            select(WorkspaceMember).where(
                                WorkspaceMember.workspace_id == workspace_uuid,
                                WorkspaceMember.user_id == user_id,
                            )
                        )
                        member = result.scalar_one_or_none()

                        if member:
                            workspace_id = workspace_uuid
                            logger.info(f"MCP OAuth2 workspace switch: {workspace_id_from_url}")
                        else:
                            logger.warning(
                                f"MCP OAuth2 not a member: url={workspace_id_from_url}, user={user_id}"
                            )
                            await _send_json_error(
                                send,
                                403,
                                {
                                    "error": "access_denied",
                                    "error_description": "You are not a member of this workspace.",
                                },
                            )
                            return
                        break

                except ValueError:
                    logger.warning(f"MCP invalid workspace UUID in URL: {workspace_id_from_url}")
                    await _send_json_error(
                        send,
                        400,
                        {
                            "error": "invalid_request",
                            "error_description": "Invalid workspace ID in URL.",
                        },
                    )
                    return

    except Exception as auth_error:
        from utils.exceptions import InvalidTokenError, TokenExpiredError, TokenRevokedError

        # Authentication failed - send 401 with RFC 6750 compliant headers
        logger.warning(f"MCP auth failed: {auth_error}")

        # Map exception type to RFC 6750 §3.1 error code.
        # RFC 6750 limits the error attribute to "invalid_request",
        # "invalid_token", and "insufficient_scope" — internal error codes
        # like AUTH-001/AUTH-003 must NOT leak into the challenge header.
        # All token-related auth failures map to invalid_token; everything
        # else (missing Bearer header, malformed request) is invalid_request.
        error_code = "invalid_request"
        error_description = str(auth_error)

        if isinstance(auth_error, (TokenExpiredError, TokenRevokedError, InvalidTokenError)):
            error_code = "invalid_token"
            error_description = getattr(auth_error, "error_description", str(auth_error))

        # Sanitize every interpolated value, not just error_description.
        # Settings.frontend_url is server-side config but a typo or stale
        # value with a stray quote / CR / LF would corrupt the header just
        # as effectively as user-controlled token bytes. Defense in depth.
        safe_code = _sanitize_challenge_attr_value(error_code)
        safe_description = _sanitize_challenge_attr_value(error_description)

        # RFC 6750 + RFC 9728 §5.1 challenge. resource_metadata is anchored
        # to frontend_url so a reverse-proxied deployment produces the same
        # absolute URL the well-known endpoints publish.
        base_url = get_settings().frontend_url.strip().rstrip("/")
        safe_resource_metadata_url = _sanitize_challenge_attr_value(
            f"{base_url}/.well-known/oauth-protected-resource"
        )
        www_authenticate = (
            f'Bearer realm="Kagura Memory Cloud", '
            f'error="{safe_code}", '
            f'error_description="{safe_description}", '
            f'resource_metadata="{safe_resource_metadata_url}"'
        )

        error_response = json.dumps(
            {"error": error_code, "error_description": error_description}
        ).encode("utf-8")

        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    [b"content-type", b"application/json"],
                    [b"www-authenticate", www_authenticate.encode("utf-8")],
                ],
            }
        )
        await send({"type": "http.response.body", "body": error_response})
        return

    # Era split (#1544). Runs after authentication and the workspace checks
    # above, so both eras inherit them, and BEFORE any session handling: a
    # modern (MCP 2026-07-28) request is stateless — it neither needs nor mints
    # an ``Mcp-Session-Id``, and a stale one on it is ignored rather than 404'd.
    # ``/mcp/`` is what the FastAPI routes normalize to; ``/mcp`` is the raw
    # ASGI mount.
    if method == "POST" and path in ("/mcp", "/mcp/"):
        body_bytes = await _read_body(receive)
        try:
            parsed_body = json.loads(body_bytes.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            parsed_body = None  # the legacy handler owns the -32700 answer

        if _is_modern_request(parsed_body, headers):
            from mcp_server.transport_stateless import handle_stateless_post

            try:
                await handle_stateless_post(
                    send, parsed_body, headers, user_id=user_id, workspace_id=workspace_id
                )
            except Exception as e:
                logger.error(f"MCP stateless handler exception: {e}", exc_info=True)
                raise
            return

        receive = _replay_receive(body_bytes)

    # Get or create session. Priority: URL path, header, query parameter.
    session_id = _extract_session_id(method, path, headers, scope.get("query_string", b""))

    session_manager = get_session_manager()

    # Session management logic
    # NEW: POST /mcp - create session for initialize, require for others
    # LEGACY: POST /messages/ - require existing session
    # LEGACY: GET /sse - create session if needed
    if method == "POST" and path == "/mcp":
        # NEW: Streamable HTTP POST endpoint
        # Need to peek at the body to determine if this is initialize request
        # For initialize, create new session. For others, require existing session.

        # If no session_id in header, this must be an initialize request
        if not session_id:
            # Create new session for initialize
            # Issue #245: context_id removed (now required in tool args)
            session = await session_manager.get_or_create_session(
                user_id=user_id,
                workspace_id=workspace_id,  # Issue #146
            )
            logger.info(f"MCP POST /mcp: created new session for initialize: {session.session_id}")
        else:
            # Session ID provided - validate it exists
            session = await session_manager.get_session(session_id)
            if session is None:
                # Issue #163: Improved session not found error with diagnostic info
                active_count = len(session_manager._sessions)
                logger.warning(
                    f"MCP POST /mcp with invalid session: {session_id}, "
                    f"active_sessions={active_count}"
                )
                await _send_json_error(
                    send,
                    404,
                    {
                        "jsonrpc": "2.0",
                        "error": {
                            "code": -32603,
                            "message": "MCP session not found or expired. Please re-initialize your connection.",
                            "data": {
                                "session_id": session_id,
                                "reason": "Session may have expired due to inactivity (1 hour timeout) or server restart",
                                "action": "Send a new 'initialize' request without Mcp-Session-Id header",
                            },
                        },
                        "id": None,
                    },
                )
                return
            logger.info(f"MCP POST /mcp: using existing session: {session.session_id}")

    elif method == "POST" and path.startswith("/mcp/messages/"):
        if session_id:
            session = await session_manager.get_session(session_id)
            if session is None:
                # Session not found - return helpful error (Issue #50)
                logger.warning(f"MCP POST to non-existent session: {session_id}")

                await _send_json_error(
                    send,
                    404,
                    {
                        "jsonrpc": "2.0",
                        "error": {
                            "code": -32603,
                            "message": "MCP session not found or expired. Please reconnect using /mcp command.",
                            "data": {
                                "session_id": session_id,
                                "reconnect_command": "/mcp",
                                "hint": "The session may have expired due to inactivity or server restart",
                            },
                        },
                        "id": None,
                    },
                )
                return
        else:
            # No session_id provided for POST
            await _send_json_error(
                send,
                400,
                {
                    "error": "Missing session_id",
                    "message": "POST requests require a valid session_id. Please connect using /mcp first.",
                },
            )
            return
    elif method == "GET" and path == "/mcp":
        # NEW: Streamable HTTP GET endpoint (optional SSE stream)
        # Create session if needed (session_id optional for GET)
        # Issue #245: context_id removed (now required in tool args)
        try:
            session = await session_manager.get_or_create_session(
                user_id=user_id,
                workspace_id=workspace_id,  # Issue #146
                session_id=session_id,
            )
            logger.info(f"MCP GET /mcp: session={session.session_id}")
        except Exception as e:
            logger.error(f"MCP GET /mcp session creation failed: {e}", exc_info=True)
            await _send_json_error(
                send,
                500,
                {"error": "Internal error", "message": "Failed to create session."},
                # #1456 review: the exception text stays in the log line
                # above (with exc_info) and out of the client body — it can
                # carry driver/DSN/path detail an MCP client has no business
                # seeing, and nothing actionable for it either way.
            )
            return
    else:
        # LEGACY: GET /sse - create session if needed
        # Issue #245: context_id removed (now required in tool args)
        try:
            session = await session_manager.get_or_create_session(
                user_id=user_id,
                workspace_id=workspace_id,  # Issue #146
                session_id=session_id,
            )
        except Exception as e:
            logger.error(f"MCP session creation failed: {e}", exc_info=True)
            await _send_json_error(
                send,
                500,
                {"error": "Internal error", "message": "Failed to create session."},
                # #1456 review: the exception text stays in the log line
                # above (with exc_info) and out of the client body — it can
                # carry driver/DSN/path detail an MCP client has no business
                # seeing, and nothing actionable for it either way.
            )
            return

    # Normalize path (remove /mcp prefix if present)
    normalized_path = _normalize_mcp_path(path)

    # Create modified scope with normalized path
    modified_scope = dict(scope)
    modified_scope["path"] = normalized_path
    modified_scope["raw_path"] = normalized_path.encode()

    logger.info(f"MCP handling: {method} {normalized_path} (session={session.session_id})")

    # Handle request based on method
    try:
        # =====================================================================
        # NEW: Streamable HTTP Transport (MCP Spec 2025-03-26)
        # =====================================================================
        if method == "POST" and (path == "/mcp" or path == "/mcp/"):
            # NEW: Unified POST /mcp endpoint
            await handle_streamable_http_post(scope, receive, send, session, headers)
            return

        elif method == "GET" and (path == "/mcp" or path == "/mcp/"):
            # NEW: Optional GET /mcp stream endpoint
            await handle_streamable_http_get(scope, receive, send, session, headers)
            return

        elif method == "GET" and normalized_path == "/sse":
            # Issue #248: SSE transport removed (deprecated in MCP spec 2025-03-26)
            logger.warning(f"MCP SSE endpoint removed: {method} {normalized_path}")
            error_response = json.dumps(
                {
                    "error": "SSE transport removed",
                    "message": "SSE transport was deprecated in MCP spec 2025-03-26. Please use Streamable HTTP (POST /mcp).",
                    "migration_guide": "https://modelcontextprotocol.io/specification/2025-03-26/basic/transports",
                }
            )
            await Response(
                error_response, status_code=410, headers={"Content-Type": "application/json"}
            )(modified_scope, receive, send)
            return

        elif method == "POST" and normalized_path.startswith("/messages/"):
            # Issue #248: Legacy POST /messages/ endpoint removed
            logger.warning(f"MCP legacy POST endpoint removed: {method} {normalized_path}")
            error_response = json.dumps(
                {
                    "error": "Legacy endpoint removed",
                    "message": "POST /messages/ was part of deprecated SSE transport. Please use POST /mcp.",
                    "migration_guide": "https://modelcontextprotocol.io/specification/2025-03-26/basic/transports",
                }
            )
            await Response(
                error_response, status_code=410, headers={"Content-Type": "application/json"}
            )(modified_scope, receive, send)
            return

        else:
            # Unsupported path/method
            logger.warning(f"MCP unsupported: {method} {normalized_path}")
            error_response = Response("Not Found", status_code=404)
            await error_response(modified_scope, receive, send)

    except Exception as e:
        logger.error(f"MCP handler exception: {e}", exc_info=True)
        raise
