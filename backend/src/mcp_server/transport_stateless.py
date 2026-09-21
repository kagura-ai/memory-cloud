"""Stateless MCP 2026-07-28 request handling — the modern half of the dual-era server.

Issue #1544. MCP 2026-07-28 removed the ``initialize`` handshake and
protocol-level sessions: every request is self-describing (protocol version and
client capabilities travel in ``params._meta``, mirrored into HTTP headers) and
is answered on its own. ``mcp_server.transport.mcp_asgi_app`` authenticates the
request, picks the era (``_is_modern_request``) and hands modern requests here;
``initialize`` clients keep the session-scoped handler in ``transport``.

Served: ``server/discover``, ``ping``, ``tools/list``, ``tools/call``.
Not implemented (answered ``404`` + ``-32601``): ``subscriptions/listen``,
multi round-trip input requests, resources and prompts — capabilities stay
``{"tools": {}}``.
"""

import asyncio
import base64
import binascii
import logging
from typing import Any
from uuid import UUID

from starlette.types import Send

from mcp_server.transport import (
    MODERN_PROTOCOL_VERSIONS,
    PROTOCOL_VERSION_META_KEY,
    SERVER_INFO,
    SERVER_INFO_META_KEY,
    SUPPORTED_PROTOCOL_VERSIONS,
    TOOLS_LIST_TTL_MS,
    _discover_result,
    _send_json_error,
    _send_jsonrpc_result,
)

logger = logging.getLogger(__name__)

CLIENT_CAPABILITIES_META_KEY = "io.modelcontextprotocol/clientCapabilities"
CLIENT_INFO_META_KEY = "io.modelcontextprotocol/clientInfo"

# Protocol-defined error codes (MCP 2026-07-28 reserves -32020..-32099).
HEADER_MISMATCH = -32020
UNSUPPORTED_PROTOCOL_VERSION = -32022

# Methods whose ``params.name`` is mirrored into the ``Mcp-Name`` header. Only
# ``tools/call`` is listed: ``resources/read`` and ``prompts/get`` are not
# implemented and fall through to -32601.
_NAMED_METHODS = frozenset({"tools/call"})

_BASE64_PREFIX = "=?base64?"
_BASE64_SUFFIX = "?="

# Client-supplied strings are echoed into log lines and error messages; the cap
# plus repr() keeps a crafted value from forging log lines or bloating replies.
_SHOWN_CHARS = 100


class _Rejected(Exception):
    """A request the stateless handler refuses before dispatch."""

    def __init__(self, status: int, code: int, message: str, data: dict | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.data = data


def _shown(value: Any) -> str:
    return str(value)[:_SHOWN_CHARS]


def _decode_header_value(raw: bytes | None) -> str | None:
    """Decode a mirrored request-metadata header, or ``None`` if absent/invalid.

    Values that are not header-safe arrive in the spec's Base64 sentinel form
    (``=?base64?<b64 of utf-8>?=``) and MUST be decoded before they are compared
    with the body.
    """
    if raw is None:
        return None
    try:
        value = raw.decode("ascii").strip()
    except UnicodeDecodeError:
        return None
    if value.startswith(_BASE64_PREFIX) and value.endswith(_BASE64_SUFFIX):
        encoded = value[len(_BASE64_PREFIX) : -len(_BASE64_SUFFIX)]
        try:
            return base64.b64decode(encoded, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return None
    return value


def _check_mirror(
    headers: dict[bytes, bytes], name: str, expected: str, missing: list[str]
) -> None:
    """Reject a mirrored header that disagrees with the body; note an absent one.

    Header *names* are case-insensitive (ASGI lower-cases them); header *values*
    are compared case-sensitively, as the spec requires.
    """
    raw = headers.get(name.lower().encode("ascii"))
    if raw is None:
        missing.append(name)
        return
    actual = _decode_header_value(raw)
    if actual != expected:
        shown_actual = "<undecodable>" if actual is None else _shown(actual)
        raise _Rejected(
            400,
            HEADER_MISMATCH,
            f"Header mismatch: {name} header value {shown_actual!r} does not match "
            f"body value {_shown(expected)!r}",
        )


def _validate(body: dict, headers: dict[bytes, bytes]) -> tuple[str, dict, list[str]]:
    """Validate a modern request envelope.

    The protocol version is settled first: a client on another revision must
    hear ``-32022`` (and the versions to retry with), not be blamed for a
    2026-07-28 rule its revision may not define.

    Mirrored metadata is then checked for *disagreement*, which is always
    rejected — a header that contradicts the body is the case the spec's
    validation rule exists for (an intermediary acting on one value while we
    execute the other). Mere *absence* of a mirrored header, or of
    ``clientCapabilities``, is tolerated and reported back to the caller for
    logging. That is a deliberate robustness deviation from two spec MUSTs:
    nothing here routes on those headers or relies on a client capability, the
    clients that matter can only be exercised in production, and a rejection
    would cost them the connection while protecting nothing. Tighten it once
    real clients are observed to send them.

    ``server/discover`` is the pre-negotiation probe, so it is answerable even
    with no ``_meta`` at all.

    Returns:
        ``(method, params, missing)`` — ``missing`` names the tolerated gaps.

    Raises:
        _Rejected: with the HTTP status and JSON-RPC error to send.
    """
    method = body["method"]
    is_discover = method == "server/discover"
    missing: list[str] = []

    params = body.get("params")
    meta = params.get("_meta") if isinstance(params, dict) else None
    if not isinstance(params, dict) or not isinstance(meta, dict):
        if not is_discover:
            raise _Rejected(400, -32602, "Invalid params: params._meta is required")
        # A bare probe: nothing in the body to validate, but a mirrored header
        # it does send must still not contradict it — absence is tolerated,
        # contradiction never is.
        missing.append("params._meta")
        _check_mirror(headers, "Mcp-Method", method, missing)
        return method, {}, missing

    requested = meta.get(PROTOCOL_VERSION_META_KEY)
    if requested is None and is_discover:
        missing.append(PROTOCOL_VERSION_META_KEY)
    elif not isinstance(requested, str) or not requested:
        raise _Rejected(
            400, -32602, f"Invalid params: _meta['{PROTOCOL_VERSION_META_KEY}'] must be a string"
        )
    elif requested not in MODERN_PROTOCOL_VERSIONS:
        # Legacy revisions are listed too (they ARE supported, through
        # ``initialize``), which is what lets a dual-era client fall back.
        raise _Rejected(
            400,
            UNSUPPORTED_PROTOCOL_VERSION,
            "Unsupported protocol version",
            {"supported": list(SUPPORTED_PROTOCOL_VERSIONS), "requested": _shown(requested)},
        )
    else:
        _check_mirror(headers, "MCP-Protocol-Version", requested, missing)

    capabilities = meta.get(CLIENT_CAPABILITIES_META_KEY)
    if capabilities is None:
        missing.append(CLIENT_CAPABILITIES_META_KEY)
    elif not isinstance(capabilities, dict):
        raise _Rejected(
            400,
            -32602,
            f"Invalid params: _meta['{CLIENT_CAPABILITIES_META_KEY}'] must be an object",
        )

    _check_mirror(headers, "Mcp-Method", method, missing)

    if method in _NAMED_METHODS:
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise _Rejected(400, -32602, "Invalid params: 'name' must be a non-empty string")
        _check_mirror(headers, "Mcp-Name", name, missing)
        # An explicit ``null`` is treated like an omitted field, as the
        # reference SDK does (``arguments: dict[str, Any] | None = None``):
        # serializers with nullable fields emit it for a no-argument call.
        arguments = params.get("arguments")
        if arguments is not None and not isinstance(arguments, dict):
            raise _Rejected(400, -32602, "Invalid params: 'arguments' must be an object")

    return method, params, missing


def _complete(result: dict) -> dict:
    """Wrap a result in the 2026-07-28 envelope: ``resultType`` + server identity."""
    return {
        "resultType": "complete",
        **result,
        "_meta": {SERVER_INFO_META_KEY: SERVER_INFO},
    }


async def _send_result(send: Send, request_id: Any, result: dict) -> None:
    """Send a JSON-RPC result. No ``Mcp-Session-Id``: this revision has none."""
    await _send_jsonrpc_result(send, None, request_id, result)


async def _send_error(
    send: Send, status: int, request_id: Any, code: int, message: str, data: dict | None = None
) -> None:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    await _send_json_error(send, status, {"jsonrpc": "2.0", "id": request_id, "error": error})


async def _call_tool(
    send: Send, request_id: Any, params: dict, user_id: str, workspace_id: "UUID | None"
) -> None:
    from mcp_server.tools import execute_tool_call

    tool_name = params["name"]
    logger.info(f"MCP calling tool (stateless): {_shown(tool_name)!r}")

    try:
        # ``_meta`` is protocol metadata: only ``arguments`` reaches the tool.
        result = await execute_tool_call(
            tool_name=tool_name,
            arguments=params.get("arguments") or {},
            user_id=user_id,
            workspace_id=workspace_id,
        )
    except Exception as e:
        error_type = type(e).__name__
        logger.error(f"MCP tools/call failed (stateless): {error_type}: {e}", exc_info=True)

        # Standard JSON-RPC codes only. The legacy handler's -32001 / -32002
        # sit in -32000..-32019, which 2026-07-28 marks as a legacy sub-range
        # new implementations SHOULD NOT use.
        data: dict[str, Any] = {"exception_type": error_type}
        if isinstance(e, ValueError):
            code, message = -32602, str(e)
            data["details"] = str(e)[:500]
        elif isinstance(e, PermissionError):
            code, message = -32603, str(e)
            data["details"] = str(e)[:500]
        elif isinstance(e, asyncio.TimeoutError):
            code, message = -32603, "Tool execution timeout"
        else:
            # The exception text stays in the log line above: it can carry
            # driver / DSN / path detail a client has no business seeing.
            code, message = -32603, "Internal error"
        await _send_error(send, 200, request_id, code, message, data)
        return

    await _send_result(
        send,
        request_id,
        _complete({"content": [{"type": item.type, "text": item.text} for item in result]}),
    )


async def handle_stateless_post(
    send: Send,
    body: dict,
    headers: dict[bytes, bytes],
    *,
    user_id: str,
    workspace_id: "UUID | None",
    query_string: bytes = b"",
) -> None:
    """Serve one modern (MCP 2026-07-28) ``POST /mcp`` request, statelessly.

    The caller has already authenticated the request and applied the workspace
    checks; identity comes from those per-request credentials, never from a
    session.

    Args:
        send: ASGI send callable.
        body: The decoded JSON-RPC message (a JSON object).
        headers: Request headers (lower-cased byte names, as ASGI delivers).
        user_id: The authenticated user.
        workspace_id: The workspace the credentials resolve to, if any.
        query_string: The raw ASGI query string; only ``tools/list`` reads it,
            for the tool profile (#1601).
    """
    request_id = body.get("id")
    method = body.get("method")

    # Unlike base JSON-RPC, an MCP request id MUST be a string or an integer
    # (bool is an int subclass in Python, hence the explicit exclusion).
    valid_id = isinstance(request_id, (str, int)) and not isinstance(request_id, bool)

    if not isinstance(method, str) or not method:
        await _send_error(
            send, 400, request_id if valid_id else None, -32600, "Invalid Request: missing method"
        )
        return

    # This revision defines no client-to-server notification over HTTP, but
    # the transport rule for one is unchanged: accept, no body.
    if "id" not in body:
        logger.info(f"MCP notification (stateless): method={_shown(method)!r}")
        await send({"type": "http.response.start", "status": 202, "headers": []})
        await send({"type": "http.response.body", "body": b""})
        return

    if not valid_id:
        await _send_error(
            send, 400, None, -32600, "Invalid Request: id must be a string or an integer"
        )
        return

    try:
        method, params, missing = _validate(body, headers)
    except _Rejected as rejected:
        # ChatGPT-class clients can only be exercised in production, so the
        # reason — and which mirrored headers the client did send (names
        # only) — has to be readable from the log alone.
        mcp_headers = sorted(
            name.decode("latin-1")[:_SHOWN_CHARS] for name in headers if name.startswith(b"mcp-")
        )
        logger.warning(
            f"MCP stateless request rejected: method={_shown(method)!r}, "
            f"code={rejected.code}, reason={rejected.message!r}, "
            f"mcp_headers={mcp_headers}, user={user_id}"
        )
        await _send_error(
            send, rejected.status, request_id, rejected.code, rejected.message, rejected.data
        )
        return

    meta = params.get("_meta") or {}
    client_info = meta.get(CLIENT_INFO_META_KEY)
    client_name = client_info.get("name") if isinstance(client_info, dict) else None
    logger.info(
        f"MCP {_shown(method)!r} (stateless, {_shown(meta.get(PROTOCOL_VERSION_META_KEY))}): "
        f"client={_shown(client_name)!r}, user={user_id}"
    )
    if missing:
        # Tolerated, not rejected (see ``_validate``) — but worth knowing
        # before the leniency is ever tightened.
        logger.warning(
            f"MCP stateless request served without required metadata: "
            f"method={_shown(method)!r}, missing={missing}, client={_shown(client_name)!r}"
        )

    if method == "server/discover":
        await _send_result(send, request_id, _discover_result())

    elif method == "ping":
        await _send_result(send, request_id, _complete({}))

    elif method == "tools/list":
        from mcp_server.tools._profiles import ToolProfileError, select_tool_definitions

        # #1601: the endpoint URL (``?profile=`` / ``?tools=``) picks what is
        # listed. A view, not an authorization boundary: ``tools/call`` below
        # never reads it.
        try:
            tools = select_tool_definitions(query_string)
        except ToolProfileError as e:
            logger.warning(
                f"MCP tools/list rejected (stateless): reason={e.message!r}, user={user_id}"
            )
            await _send_error(send, 400, request_id, -32602, e.message)
            return

        # Caching hints are MUST on a complete tools/list result. The list
        # varies by URL (the tool profile) and never by caller identity, so it
        # is still shareable between callers of the same URL: "public" holds.
        # Switch to "private" if it ever varies by plan, role or workspace.
        await _send_result(
            send,
            request_id,
            _complete(
                {
                    "tools": tools,
                    "ttlMs": TOOLS_LIST_TTL_MS,
                    "cacheScope": "public",
                }
            ),
        )

    elif method == "tools/call":
        await _call_tool(send, request_id, params, user_id, workspace_id)

    else:
        # The modern contract is 404 AND a -32601 body: the body is what tells
        # a client this is a modern server rather than a legacy one that does
        # not host the endpoint.
        await _send_error(send, 404, request_id, -32601, f"Method not found: {_shown(method)}")
