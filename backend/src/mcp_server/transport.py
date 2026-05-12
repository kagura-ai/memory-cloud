"""MCP over Streamable HTTP transport.

Implements MCP Streamable HTTP transport (spec 2025-03-26) for remote client connections.
Issue #248: SSE transport removed (deprecated in MCP spec 2025-03-26).
"""

import asyncio
import json
import logging
from typing import TYPE_CHECKING
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
    # Read request body
    body_bytes = b""
    while True:
        message = await receive()
        if message["type"] == "http.request":
            body_bytes += message.get("body", b"")
            if not message.get("more_body", False):
                break

    try:
        body = json.loads(body_bytes.decode("utf-8"))
    except json.JSONDecodeError as e:
        logger.error(f"MCP POST invalid JSON: {e}")
        error_response = json.dumps(
            {
                "jsonrpc": "2.0",
                "error": {"code": -32700, "message": "Parse error"},
                "id": None,
            }
        ).encode()

        await send(
            {
                "type": "http.response.start",
                "status": 400,
                "headers": [[b"content-type", b"application/json"]],
            }
        )
        await send({"type": "http.response.body", "body": error_response})
        return

    # Check if this is a notification (no "id" field)
    is_notification = "id" not in body

    if is_notification:
        # Notifications don't expect a response - return 202 Accepted
        logger.info(f"MCP notification: method={body.get('method')}, session={session.session_id}")
        await send({"type": "http.response.start", "status": 202, "headers": []})
        await send({"type": "http.response.body", "body": b""})
        return

    # Check request method
    method = body.get("method")
    request_id = body.get("id")

    # Handle initialize request
    if method == "initialize":
        logger.info(f"MCP initialize (Streamable HTTP): session={session.session_id}")

        response = {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {
                    "name": "kagura-memory-cloud",
                    "version": APP_VERSION,
                },
            },
        }

        response_body = json.dumps(response).encode()

        # Return session ID in header
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
        await send({"type": "http.response.body", "body": response_body})
        return

    # Handle tools/list request
    elif method == "tools/list":
        logger.info(f"MCP tools/list (Streamable HTTP): session={session.session_id}")

        from mcp_server.tools import get_tool_definitions

        tools = get_tool_definitions()
        response = {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"tools": tools},
        }

        response_body = json.dumps(response).encode()

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
        await send({"type": "http.response.body", "body": response_body})
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

            # Format response
            response = {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"content": [{"type": item.type, "text": item.text} for item in result]},
            }

            response_body = json.dumps(response).encode()

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
            await send({"type": "http.response.body", "body": response_body})
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

    # For other requests, use existing transport mechanism
    # Reuse the legacy POST message handler
    logger.info(f"MCP request (Streamable HTTP): method={method}, session={session.session_id}")

    # Create modified scope for legacy handler
    modified_scope = dict(scope)
    modified_scope["path"] = f"/messages/{session.session_id}/"
    modified_scope["raw_path"] = f"/messages/{session.session_id}/".encode()

    # Create a new receive callable that replays the body
    body_sent = False

    async def replay_receive():
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {
                "type": "http.request",
                "body": body_bytes,
                "more_body": False,
            }
        # After body is sent, wait for disconnect
        return await receive()

    # Use existing transport handler with replayed body
    await session.transport.handle_post_message(modified_scope, replay_receive, send)


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
                    error_response = json.dumps(
                        {
                            "error": "workspace_mismatch",
                            "error_description": "API key workspace does not match URL workspace. "
                            "Use an API key scoped to this workspace.",
                        }
                    ).encode("utf-8")

                    await send(
                        {
                            "type": "http.response.start",
                            "status": 403,
                            "headers": [[b"content-type", b"application/json"]],
                        }
                    )
                    await send({"type": "http.response.body", "body": error_response})
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
                            error_response = json.dumps(
                                {
                                    "error": "access_denied",
                                    "error_description": "You are not a member of this workspace.",
                                }
                            ).encode("utf-8")

                            await send(
                                {
                                    "type": "http.response.start",
                                    "status": 403,
                                    "headers": [[b"content-type", b"application/json"]],
                                }
                            )
                            await send({"type": "http.response.body", "body": error_response})
                            return
                        break

                except ValueError:
                    logger.warning(f"MCP invalid workspace UUID in URL: {workspace_id_from_url}")
                    error_response = json.dumps(
                        {
                            "error": "invalid_request",
                            "error_description": "Invalid workspace ID in URL.",
                        }
                    ).encode("utf-8")

                    await send(
                        {
                            "type": "http.response.start",
                            "status": 400,
                            "headers": [[b"content-type", b"application/json"]],
                        }
                    )
                    await send({"type": "http.response.body", "body": error_response})
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

    # Get or create session
    # Priority order: 1) URL path, 2) Header, 3) Query parameter
    session_id = None

    # For POST requests, extract session_id from path (/messages/{session_id}/)
    if method == "POST" and path.startswith("/mcp/messages/"):
        # Extract session_id from path: /mcp/messages/mcp-xxx/ -> mcp-xxx
        path_parts = path.strip("/").split("/")
        if len(path_parts) >= 3:  # ['mcp', 'messages', 'session_id']
            session_id = path_parts[2]
            logger.info(f"MCP session_id from path: {session_id}")

    # If not found in path, try header
    if not session_id:
        session_id_header = headers.get(b"mcp-session-id")
        if session_id_header:
            session_id = session_id_header.decode("utf-8")
            logger.info(f"MCP session_id from header: {session_id}")

    # If still not found, try query parameter
    if not session_id:
        query_string = scope.get("query_string", b"").decode("utf-8")
        if "session_id=" in query_string:
            for param in query_string.split("&"):
                if param.startswith("session_id="):
                    session_id = param.split("=", 1)[1]
                    logger.info(f"MCP session_id from query: {session_id}")
                    break

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
                error_response = json.dumps(
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
                    }
                ).encode()

                await send(
                    {
                        "type": "http.response.start",
                        "status": 404,
                        "headers": [[b"content-type", b"application/json"]],
                    }
                )
                await send({"type": "http.response.body", "body": error_response})
                return
            logger.info(f"MCP POST /mcp: using existing session: {session.session_id}")

    elif method == "POST" and path.startswith("/mcp/messages/"):
        if session_id:
            session = await session_manager.get_session(session_id)
            if session is None:
                # Session not found - return helpful error (Issue #50)
                logger.warning(f"MCP POST to non-existent session: {session_id}")

                error_response = json.dumps(
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
                    }
                ).encode("utf-8")

                await send(
                    {
                        "type": "http.response.start",
                        "status": 404,
                        "headers": [[b"content-type", b"application/json"]],
                    }
                )
                await send({"type": "http.response.body", "body": error_response})
                return
        else:
            # No session_id provided for POST
            error_response = json.dumps(
                {
                    "error": "Missing session_id",
                    "message": "POST requests require a valid session_id. Please connect using /mcp first.",
                }
            ).encode("utf-8")

            await send(
                {
                    "type": "http.response.start",
                    "status": 400,
                    "headers": [[b"content-type", b"application/json"]],
                }
            )
            await send({"type": "http.response.body", "body": error_response})
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
            error_response = json.dumps(
                {"error": "Internal error", "message": f"Failed to create session: {str(e)}"}
            ).encode()

            await send(
                {
                    "type": "http.response.start",
                    "status": 500,
                    "headers": [[b"content-type", b"application/json"]],
                }
            )
            await send({"type": "http.response.body", "body": error_response})
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
            error_response = json.dumps(
                {"error": "Internal error", "message": f"Failed to create session: {str(e)}"}
            ).encode()

            await send(
                {
                    "type": "http.response.start",
                    "status": 500,
                    "headers": [[b"content-type", b"application/json"]],
                }
            )
            await send({"type": "http.response.body", "body": error_response})
            return

    # Normalize path (remove /mcp prefix if present)
    normalized_path = path
    if path.startswith("/mcp/"):
        normalized_path = path[4:]
    elif path == "/mcp":
        normalized_path = "/"

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
