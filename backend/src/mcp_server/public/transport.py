"""Anonymous Streamable HTTP transport at ``/mcp/public/{public_id}`` (#2675).

Serves both protocol eras of the dual-era MCP server — ``initialize``
sessions and stateless 2026-07-28 requests — without authentication: it never
reads ``Authorization`` or ``Cookie`` and never challenges. A client that
sends an owner key gets the same public answers as anyone else.

Per request, in this order: Origin check (the same allow-list as ``/mcp``),
kill switch, address shape, then ``load_serving_endpoint`` (one query, no
cache) before any session or dispatch logic. Every "not served" case — switch
off, unknown or malformed id, unpublished, context no longer public — is one
404 body. A workspace whose plan pauses public serving, or that is over its
capacity, gets a bare 503. Then the method (GET 405, DELETE ends the session),
a bounded body, no batches, and the era split.

The JSON-RPC helpers are the main server's; tool calls go through
``dispatch.call_public_tool`` only.
"""

from __future__ import annotations

import contextlib
import json
import re
from typing import Any

from starlette.types import Receive, Scope, Send

from config.constants import APP_VERSION
from mcp_server.public.definitions import public_tool_definitions, public_tool_view
from mcp_server.public.dispatch import call_public_tool
from mcp_server.public.limits import (
    PUBLIC_MAX_REQUEST_BYTES,
    check_public_rate_limit,
)
from mcp_server.public.publication import (
    ServingEndpoint,
    load_serving_endpoint,
    public_endpoints_enabled,
    serving_paused,
)
from mcp_server.session import get_session_manager, is_server_minted_session_id
from mcp_server.tools._profiles import ToolProfileError
from mcp_server.transport import (
    DEFAULT_PROTOCOL_VERSION,
    LEGACY_PROTOCOL_VERSIONS,
    PROTOCOL_VERSION_META_KEY,
    SERVER_CAPABILITIES,
    SERVER_INFO_META_KEY,
    TOOLS_LIST_TTL_MS,
    _discover_result,
    _end_session,
    _extract_session_id,
    _is_modern_request,
    _reject_disallowed_origin,
    _send_invalid_request,
    _send_json_error,
    _send_jsonrpc_result,
    _send_method_not_allowed,
    _send_session_not_found,
    _send_transport_failure,
    _StartTrackingSend,
    _tool_call_result,
)
from mcp_server.transport_stateless import _complete, _Rejected, validate_modern_envelope
from services.guardrail_digest import cut_summary, flatten_summary
from utils.logger import get_logger
from utils.public_id import PublicIdPrefix, public_id_pattern

logger = get_logger(__name__)

PUBLIC_SERVER_INFO = {"name": "kagura-memory-cloud-public", "version": APP_VERSION}

PUBLIC_INSTRUCTIONS_NAME_CAP = 40
_PUBLIC_INSTRUCTIONS = (
    "Read-only public data: {name}. recall searches it; reference reads an entry in full; "
    "explore finds related entries; list_tags lists categories; get_context_info "
    "describes it. Nothing you send is stored."
)

NOT_PUBLISHED_BODY = {
    "error": "not_found",
    "error_description": "No public MCP endpoint is published at this address.",
}
_PUBLIC_ID_RE = re.compile(public_id_pattern(PublicIdPrefix.PUBLIC_MCP))
_PAUSED_RETRY_AFTER_SECONDS = 300
_CONTROL_METHODS = frozenset({"initialize", "tools/list", "ping", "server/discover"})


def public_instructions(endpoint: ServingEndpoint) -> str:
    """The server ``instructions`` for one endpoint (at most 240 characters).

    The owner's public name is flattened and cut, so it cannot forge the
    instruction boundary; the description and usage guide are data, returned
    by ``get_context_info``.
    """
    name = cut_summary(flatten_summary(endpoint.public_name), PUBLIC_INSTRUCTIONS_NAME_CAP)
    return _PUBLIC_INSTRUCTIONS.format(name=name or "this dataset")


def _public_complete(result: dict[str, Any]) -> dict[str, Any]:
    """The 2026-07-28 result envelope, naming the public server."""
    return {**_complete(result), "_meta": {SERVER_INFO_META_KEY: PUBLIC_SERVER_INFO}}


def _public_discover(endpoint: ServingEndpoint) -> dict[str, Any]:
    # The tools/list TTL instead of an hour: an unpublish must reach clients.
    return {
        **_discover_result(public_instructions(endpoint)),
        "ttlMs": TOOLS_LIST_TTL_MS,
        "_meta": {SERVER_INFO_META_KEY: PUBLIC_SERVER_INFO},
    }


async def _send_not_published(send: Send) -> None:
    await _send_json_error(send, 404, NOT_PUBLISHED_BODY)


async def _send_paused(send: Send) -> None:
    await _send_json_error(
        send,
        503,
        {"error": "temporarily_unavailable"},
        [[b"retry-after", str(_PAUSED_RETRY_AFTER_SECONDS).encode()]],
    )


async def _send_rate_limited(send: Send, retry_after: int) -> None:
    await _send_json_error(
        send, 429, {"error": "rate_limited"}, [[b"retry-after", str(retry_after).encode()]]
    )


async def _control_allowed(send: Send, endpoint: ServingEndpoint) -> bool:
    """Count one control request; answer 429 and return False when over."""
    retry_after = await check_public_rate_limit(endpoint, session_id=None, bucket="control")
    if retry_after is None:
        return True
    await _send_rate_limited(send, retry_after)
    return False


async def _load(public_id: str) -> tuple[ServingEndpoint | None, bool]:
    """``(endpoint, paused)`` from one short-lived session."""
    from db.base import get_db

    async with contextlib.aclosing(get_db()) as sessions:
        async for db in sessions:
            endpoint = await load_serving_endpoint(db, public_id)
            if endpoint is None:
                return None, False
            return endpoint, await serving_paused(db, endpoint)
    return None, False  # pragma: no cover - get_db always yields


async def _read_bounded_body(scope: Scope, receive: Receive) -> bytes | None:
    """The request body, or ``None`` when it exceeds ``PUBLIC_MAX_REQUEST_BYTES``."""
    for name, value in scope.get("headers", []):
        if name.lower() == b"content-length":
            try:
                if int(value) > PUBLIC_MAX_REQUEST_BYTES:
                    return None
            except ValueError:
                return None
    chunks: list[bytes] = []
    size = 0
    while True:
        message = await receive()
        if message["type"] == "http.request":
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > PUBLIC_MAX_REQUEST_BYTES:
                return None
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        elif message["type"] == "http.disconnect":
            break
    return b"".join(chunks)


async def public_mcp_asgi_app(scope: Scope, receive: Receive, send: Send) -> None:
    """ASGI entry for ``/mcp/public/{public_id}``.

    ``scope["public_endpoint_public_id"]`` carries the address segment;
    ``scope["path"]`` is ``/mcp/public/`` for the endpoint itself.
    """
    if scope.get("type") != "http":
        return
    headers: dict[bytes, bytes] = {k.lower(): v for k, v in scope.get("headers", [])}
    if await _reject_disallowed_origin(headers, send):
        return
    public_id = scope.get("public_endpoint_public_id")
    if (
        not public_endpoints_enabled()
        or not isinstance(public_id, str)
        or _PUBLIC_ID_RE.fullmatch(public_id) is None
        or scope.get("path") != "/mcp/public/"
    ):
        await _send_not_published(send)
        return

    try:
        endpoint, paused = await _load(public_id)
    except Exception as exc:
        logger.error("public_mcp_endpoint_lookup_failed", error_type=type(exc).__name__)
        await _send_transport_failure(send, exc, body=None, era="legacy")
        return
    if endpoint is None:
        await _send_not_published(send)
        return
    if paused:
        logger.info("public_mcp_serving_paused", endpoint_id=str(endpoint.id))
        await _send_paused(send)
        return

    method = scope.get("method", "")
    user_id = f"public:{endpoint.id}"
    query_string: bytes = scope.get("query_string", b"")
    if method == "DELETE":
        session_id = _extract_session_id("DELETE", "/mcp/", headers, query_string)
        await _end_session(send, get_session_manager(), session_id, user_id, endpoint.workspace_id)
        return
    if method != "POST":
        await _send_method_not_allowed(send)
        return

    raw = await _read_bounded_body(scope, receive)
    if raw is None:
        await _send_invalid_request(send, "Invalid Request: request too large")
        return
    try:
        body = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        await _send_json_error(
            send,
            400,
            {"jsonrpc": "2.0", "error": {"code": -32700, "message": "Parse error"}, "id": None},
        )
        return
    if isinstance(body, list):
        await _send_invalid_request(
            send, "Invalid Request: batch requests are not accepted on this endpoint"
        )
        return

    tracked = _StartTrackingSend(send)
    era = "stateless" if _is_modern_request(body) else "legacy"
    try:
        if era == "stateless":
            await _serve_stateless(tracked, body, headers, endpoint, query_string)
        else:
            await _serve_session(tracked, body, headers, endpoint, query_string)
    except Exception as exc:
        if tracked.started:
            logger.error("public_mcp_handler_failed", error_type=type(exc).__name__)
            raise
        await _send_transport_failure(send, exc, body=body, era=era)


# ============================================================================
# Stateless era (MCP 2026-07-28)
# ============================================================================


async def _serve_stateless(
    send: Send,
    body: dict[str, Any],
    headers: dict[bytes, bytes],
    endpoint: ServingEndpoint,
    query_string: bytes,
) -> None:
    request_id = body.get("id")
    method = body.get("method")
    valid_id = isinstance(request_id, (str, int)) and not isinstance(request_id, bool)

    async def error(status: int, code: int, message: str, data: dict | None = None) -> None:
        payload: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            payload["data"] = data
        await _send_json_error(
            send,
            status,
            {"jsonrpc": "2.0", "id": request_id if valid_id else None, "error": payload},
        )

    if not isinstance(method, str) or not method:
        await error(400, -32600, "Invalid Request: missing method")
        return
    is_notification = "id" not in body
    if not is_notification and not valid_id:
        await error(400, -32600, "Invalid Request: id must be a string or an integer")
        return
    try:
        method, params, _missing = validate_modern_envelope(body, headers)
    except _Rejected as rejected:
        await error(rejected.status, rejected.code, rejected.message, rejected.data)
        return
    if is_notification:
        await send({"type": "http.response.start", "status": 202, "headers": []})
        await send({"type": "http.response.body", "body": b""})
        return

    if method in _CONTROL_METHODS and not await _control_allowed(send, endpoint):
        return
    if method == "server/discover":
        await _send_jsonrpc_result(send, None, request_id, _public_discover(endpoint))
    elif method == "ping":
        await _send_jsonrpc_result(send, None, request_id, _public_complete({}))
    elif method == "tools/list":
        try:
            tools = public_tool_definitions(query_string)
        except ToolProfileError as exc:
            await error(400, -32602, exc.message)
            return
        await _send_jsonrpc_result(
            send,
            None,
            request_id,
            _public_complete({"tools": tools, "ttlMs": TOOLS_LIST_TTL_MS, "cacheScope": "public"}),
        )
    elif method == "tools/call":
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        content = await call_public_tool(
            endpoint,
            _call_view(query_string),
            params.get("name"),
            params.get("arguments"),
            era="stateless",
            session_id=None,
            protocol_version=_shown_version(meta.get(PROTOCOL_VERSION_META_KEY)),
        )
        await _send_jsonrpc_result(
            send, None, request_id, _public_complete(_tool_call_result(content))
        )
    else:
        await error(404, -32601, f"Method not found: {method[:100]}")


def _call_view(query_string: bytes) -> tuple[str, ...]:
    """The view ``tools/call`` enforces; a ``tools=`` matching nothing allows nothing."""
    try:
        return public_tool_view(query_string)
    except ToolProfileError:
        return ()


def _shown_version(value: object) -> str | None:
    return value[:32] if isinstance(value, str) else None


# ============================================================================
# Session era (initialize handshake)
# ============================================================================


async def _serve_session(
    send: Send,
    body: Any,
    headers: dict[bytes, bytes],
    endpoint: ServingEndpoint,
    query_string: bytes,
) -> None:
    """Open or resume the session under the endpoint's pseudo principal, then dispatch.

    The principal ``public:<endpoint id>`` is never an account: a session of an
    authenticated user, or of another endpoint, is "foreign" here (404).
    """
    manager = get_session_manager()
    user_id = f"public:{endpoint.id}"
    workspace_id = endpoint.workspace_id
    session_id = _extract_session_id("POST", "/mcp/", headers, query_string)
    counted = False
    if not session_id:
        if not await _control_allowed(send, endpoint):
            return
        counted = True
        session = await manager.get_or_create_session(user_id=user_id, workspace_id=workspace_id)
    else:
        lookup, session = await manager.get_owned_session(session_id, user_id, workspace_id)
        if lookup == "missing" and is_server_minted_session_id(session_id):
            # Lost on a restart or the idle timeout: re-adopted under the same
            # pseudo principal, as on /mcp.
            if not await _control_allowed(send, endpoint):
                return
            counted = True
            try:
                session = await manager.get_or_create_session(
                    user_id=user_id, workspace_id=workspace_id, session_id=session_id
                )
            except PermissionError:
                session = None
        if session is None:
            await _send_session_not_found(send, session_id)
            return
    await _dispatch_public_message(send, session, body, endpoint, query_string, counted=counted)


async def _dispatch_public_message(
    send: Send,
    session: Any,
    body: Any,
    endpoint: ServingEndpoint,
    query_string: bytes,
    *,
    counted: bool,
) -> None:
    if not isinstance(body, dict):
        await _send_invalid_request(send, "Invalid Request: expected a JSON-RPC object")
        return
    method = body.get("method")
    request_id = body.get("id")
    session_header = [[b"mcp-session-id", session.session_id.encode()]]
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
    if "id" not in body:
        await send({"type": "http.response.start", "status": 202, "headers": []})
        await send({"type": "http.response.body", "body": b""})
        return

    async def rpc_error(code: int, message: str) -> None:
        await _send_json_error(
            send,
            200,
            {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}},
            session_header,
        )

    if method in _CONTROL_METHODS and not counted and not await _control_allowed(send, endpoint):
        return

    if method == "initialize":
        params = body.get("params")
        requested = params.get("protocolVersion") if isinstance(params, dict) else None
        protocol_version = (
            requested if requested in LEGACY_PROTOCOL_VERSIONS else DEFAULT_PROTOCOL_VERSION
        )
        session.protocol_version = protocol_version
        client_info = params.get("clientInfo") if isinstance(params, dict) else None
        client_name = client_info.get("name") if isinstance(client_info, dict) else None
        # Unverified and in memory only (#2678); stored reduced, if at all.
        session.client_name = client_name[:200] if isinstance(client_name, str) else None
        await _send_jsonrpc_result(
            send,
            session,
            request_id,
            {
                "protocolVersion": protocol_version,
                "capabilities": SERVER_CAPABILITIES,
                "serverInfo": PUBLIC_SERVER_INFO,
                "instructions": public_instructions(endpoint),
            },
        )
    elif method == "tools/list":
        try:
            tools = public_tool_definitions(query_string)
        except ToolProfileError as exc:
            await rpc_error(-32602, exc.message)
            return
        await _send_jsonrpc_result(send, session, request_id, {"tools": tools})
    elif method == "tools/call":
        params = body.get("params")
        name = params.get("name") if isinstance(params, dict) else None
        if not isinstance(name, str) or not name:
            await rpc_error(-32602, "Invalid params: 'name' must be a non-empty string")
            return
        arguments = params.get("arguments") if isinstance(params, dict) else None
        if arguments is not None and not isinstance(arguments, dict):
            await rpc_error(-32602, "Invalid params: 'arguments' must be an object")
            return
        content = await call_public_tool(
            endpoint,
            _call_view(query_string),
            name,
            arguments,
            era="session",
            session_id=session.session_id,
            protocol_version=getattr(session, "protocol_version", None),
            client_name=getattr(session, "client_name", None),
        )
        await _send_jsonrpc_result(send, session, request_id, _tool_call_result(content))
    elif method == "ping":
        await _send_jsonrpc_result(send, session, request_id, {})
    else:
        await rpc_error(-32601, f"Method not found: {method[:100]}")
