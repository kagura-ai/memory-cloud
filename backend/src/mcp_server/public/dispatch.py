"""``tools/call`` on the public MCP endpoint (#2675).

One pipeline for both protocol eras, in this order:

1. the name must be in the URL's view of ``PUBLIC_TOOLS`` — anything else
   (hidden, write, admin, secret, unknown, narrowed away) is ``unknown_tool``
   with one message shape, before any database or rate-limit access;
2. ``arguments`` must be an object (or absent);
3. every argument name must be in the tool's public schema
   (``version`` on ``reference`` gets its own message);
4. the per-minute buckets (fail closed when Redis cannot be read);
5. ``context_id``, when given, must be the bound context;
6. the public handler, under the tool's timeout.

``record_public_call`` runs once for every call that reaches step 1,
whatever the outcome.
"""

from __future__ import annotations

import time
from typing import Any, Literal
from uuid import UUID

from mcp.types import TextContent

from mcp_server.public.definitions import public_argument_names
from mcp_server.public.limits import (
    PUBLIC_RETRY_AFTER_SECONDS,
    PublicRateLimitUnavailable,
    check_public_rate_limit,
)
from mcp_server.public.publication import ServingEndpoint
from mcp_server.public.telemetry import PublicCallRecord, record_public_call
from mcp_server.public.tools import PUBLIC_HANDLERS, PublicArgumentError, PublicToolResult
from mcp_server.tools._errors import _tool_exception_response
from mcp_server.tools._helpers import _error_response, execute_with_timeout
from utils.exceptions import MemoryCloudException
from utils.logger import get_logger

logger = get_logger(__name__)

_NAME_CHARS = 64

UNAVAILABLE_MESSAGE = "The public endpoint is briefly unavailable. Try again shortly."


def _unknown_tool(name: object) -> list[TextContent]:
    shown = name[:_NAME_CHARS] if isinstance(name, str) else str(name)[:_NAME_CHARS]
    return _error_response("unknown_tool", f"Unknown tool: {shown}")


def _unavailable() -> list[TextContent]:
    return _error_response(
        "temporarily_unavailable",
        UNAVAILABLE_MESSAGE,
        retry_after_seconds=PUBLIC_RETRY_AFTER_SECONDS,
    )


def _invalid(message: str) -> list[TextContent]:
    return _error_response("invalid_argument", message)


def _check_argument_names(name: str, arguments: dict[str, Any]) -> list[TextContent] | None:
    accepted = public_argument_names(name)
    unknown = sorted(str(key) for key in arguments if key not in accepted)
    if not unknown:
        return None
    if name == "reference" and "version" in unknown:
        return _invalid("historical versions are not available on this endpoint")
    shown = ", ".join(key[:_NAME_CHARS] for key in unknown[:10])
    return _invalid(
        f"unknown arguments: {shown}. This tool accepts: {', '.join(sorted(accepted))}."
    )


def _context_matches(value: object, endpoint: ServingEndpoint) -> bool:
    if isinstance(value, UUID):
        return value == endpoint.context_id
    if not isinstance(value, str):
        return False
    try:
        return UUID(value) == endpoint.context_id
    except ValueError:
        return False


async def _run(
    endpoint: ServingEndpoint,
    view: tuple[str, ...],
    name: object,
    arguments: object,
    session_id: str | None,
) -> PublicToolResult:
    if not isinstance(name, str) or name not in view:
        return PublicToolResult(_unknown_tool(name), "denied")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return PublicToolResult(_invalid("arguments must be an object."), "invalid_request")
    refused = _check_argument_names(name, arguments)
    if refused is not None:
        return PublicToolResult(refused, "invalid_request")
    try:
        retry_after = await check_public_rate_limit(endpoint, session_id=session_id, bucket="call")
    except PublicRateLimitUnavailable:
        return PublicToolResult(_unavailable(), "error")
    if retry_after is not None:
        return PublicToolResult(
            _error_response(
                "rate_limit_exceeded",
                "Too many calls to this public endpoint. Wait before calling again.",
                retry_after_seconds=retry_after,
            ),
            "rate_limited",
        )
    if "context_id" in arguments and not _context_matches(arguments["context_id"], endpoint):
        return PublicToolResult(
            _error_response(
                "context_not_available",
                "This endpoint serves one published dataset; omit context_id.",
            ),
            "denied",
        )
    handler = PUBLIC_HANDLERS[name]
    try:
        return await execute_with_timeout(handler(endpoint, arguments), operation_name=name)
    except PublicArgumentError as exc:
        return PublicToolResult(_invalid(exc.message), "invalid_request")
    except MemoryCloudException as exc:
        if exc.status_code < 500:
            # A plan, quota or capacity refusal from a shared service: its
            # message describes the workspace, which an anonymous caller
            # must not see.
            logger.warning("public_mcp_tool_refused", tool=name, error_type=type(exc).__name__)
            return PublicToolResult(_unavailable(), "error")
        return PublicToolResult(
            _tool_exception_response(name, exc, echo_value_error=False), "error"
        )
    except PermissionError as exc:
        logger.warning("public_mcp_tool_refused", tool=name, error_type=type(exc).__name__)
        return PublicToolResult(_unavailable(), "error")
    except Exception as exc:  # noqa: BLE001 - logged with a correlation id, never echoed
        return PublicToolResult(
            _tool_exception_response(name, exc, echo_value_error=False), "error"
        )


async def call_public_tool(
    endpoint: ServingEndpoint,
    view: tuple[str, ...],
    name: object,
    arguments: object,
    *,
    era: Literal["session", "stateless"],
    session_id: str | None,
    protocol_version: str | None = None,
) -> list[TextContent]:
    """Run one public ``tools/call`` and record it once.

    Args:
        endpoint: The serving endpoint (revalidated by the transport).
        view: The URL's narrowed tool view (``public_tool_view``).
        name: ``params.name`` as the client sent it.
        arguments: ``params.arguments`` as the client sent it.
        era: ``"session"`` or ``"stateless"``.
        session_id: The session id (session era), counted in its bucket.
        protocol_version: The negotiated / declared protocol version.

    Returns:
        The tool result content; an error result is marked ``is_error``.
    """
    started = time.perf_counter()
    result = PublicToolResult(_unavailable(), "error")
    try:
        result = await _run(endpoint, view, name, arguments, session_id)
    finally:
        tool = name[:_NAME_CHARS] if isinstance(name, str) else str(name)[:_NAME_CHARS]
        record_public_call(
            PublicCallRecord(
                endpoint_id=endpoint.id,
                public_id=endpoint.public_id,
                workspace_id=endpoint.workspace_id,
                context_id=endpoint.context_id,
                resource_pk=endpoint.resource_pk,
                resource_id=endpoint.resource_id,
                tool=tool,
                outcome=result.outcome,
                duration_ms=int((time.perf_counter() - started) * 1000),
                era=era,
                protocol_version=protocol_version,
                result_count=result.result_count,
                continuation=result.continuation,
            )
        )
    return result.content
