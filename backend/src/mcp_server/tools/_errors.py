"""Safe, actionable error envelopes for MCP tool failures (#1684).

One vocabulary for every path that used to return raw exception text or a bare
"An internal error occurred.": the dispatch catch-alls in
``execute_tool_call``, the per-tool catch-alls in the handler modules (which
keep their ``<tool>_error`` codes) and the JSON-RPC fallbacks of both
transports. An exception is one of two kinds:

* A **refusal** — a ``MemoryCloudException`` below 500, a plain
  ``ValueError`` (the service layer's bad-request signal) or a
  ``PermissionError``. Its message was written for the caller (REST returns
  the same text), so it is kept, with a ``help`` line for the next step.
* A **server failure** — everything else. The caller gets a stable ``cause``
  (``timeout`` / ``service_unavailable`` / ``internal_error``), a fixed
  message, ``help``, a ``correlation_id`` and retry advice. The exception's
  text, type and traceback go to the server log under the same
  ``correlation_id`` — never into the response.

Retry advice follows the tool's read-only hint: repeating a read is safe; a
write whose outcome is unknown is never marked retryable, and ``help`` names
the read that shows whether it took effect.
"""

from __future__ import annotations

import functools
import logging
import secrets
import socket
from dataclasses import dataclass
from typing import Any

from mcp_server.tools._helpers import ToolErrorContent, _ContextNotFoundError, _error_response

logger = logging.getLogger(__name__)

CAUSE_TIMEOUT = "timeout"
CAUSE_SERVICE_UNAVAILABLE = "service_unavailable"
CAUSE_INTERNAL_ERROR = "internal_error"

# Same wait the REST database-unavailable 503 advertises in ``Retry-After``.
RETRY_AFTER_SECONDS = 5

# Envelope keys a forwarded ``details`` block must not overwrite.
_RESERVED_KEYS = frozenset({"status", "error", "message"})

# The read that shows whether a write took effect, named in ``help`` when a
# write fails with an unknown outcome. Writes without an entry get the generic
# advice; ``test_tool_errors.py`` keeps every name registered.
_VERIFY_WITH: dict[str, str] = {
    "remember": "recall",
    "update_memory": "reference",
    "forget": "reference",
    "create_edge": "list_edges",
    "update_edge": "list_edges",
    "delete_edge": "list_edges",
    "create_context": "list_contexts",
    "delete_context": "list_contexts",
    "merge_contexts": "list_contexts",
    "update_context": "get_context_info",
    "update_search_config": "get_context_info",
    "rollback_sleep_run": "get_sleep_report",
    "setup_resource": "list_resource_tokens",
    "ingest_events": "get_resource_impact",
    "analyze_context": "list_analyses",
    "init_file_upload": "list_files",
    "complete_file_upload": "list_files",
    "delete_file": "list_files",
    "set_state": "get_state",
    "record_measurement": "recall_series",
    "register_agent": "list_agents",
    "update_agent": "get_agent",
    "delete_agent": "list_agents",
    "bind_agent_context": "list_agent_bindings",
    "update_agent_binding": "list_agent_bindings",
    "unbind_agent_context": "list_agent_bindings",
    "secret_put": "secret_list",
    "secret_revoke_grant": "secret_list",
}

_REFUSAL_HELP: dict[str, str] = {
    "validation_error": "Correct the arguments the message describes, then call the tool again.",
    "permission_denied": (
        "Your role does not allow this operation. Check your workspace and context role; "
        "a workspace owner can grant access."
    ),
    "not_found": (
        "Check the id. list_contexts, recall or the matching list_* tool returns valid ids."
    ),
    "conflict": "Read the target's current state before deciding whether to call again.",
    "quota_exceeded": (
        "Call get_usage to see your limits. Wait for the quota to reset or free up capacity."
    ),
    "rate_limit_exceeded": "Wait before calling again. get_usage shows your remaining quota.",
    "feature_not_available": (
        "This feature is not enabled for the workspace; the response says which plan or "
        "switch it needs."
    ),
}

_READ_HELP: dict[str, str] = {
    CAUSE_TIMEOUT: (
        "This tool only reads, so calling it again is safe. Wait a few seconds and retry; "
        "if it keeps failing, report the correlation_id."
    ),
    CAUSE_SERVICE_UNAVAILABLE: (
        "This tool only reads, so calling it again is safe. Wait a few seconds and retry; "
        "if it keeps failing, report the correlation_id."
    ),
    CAUSE_INTERNAL_ERROR: (
        "This tool only reads, so one more attempt is safe. If it fails again, stop "
        "retrying and report the correlation_id."
    ),
}


@dataclass(frozen=True)
class ToolFailure:
    """A classified tool failure, rendered as a tool result or a JSON-RPC error."""

    error: str
    message: str
    fields: dict[str, Any]

    def response(self) -> ToolErrorContent:
        """The ``isError`` tool result every handler returns."""
        return _error_response(self.error, self.message, **self.fields)

    def jsonrpc_data(self) -> dict[str, Any]:
        """``error.data`` for a transport-level JSON-RPC error."""
        return {"error": self.error, **self.fields}


# ============================================================================
# Read-only classification
# ============================================================================


def read_only_hint(definition: dict[str, Any]) -> bool:
    """Whether a tool definition declares the tool read-only.

    ``annotations.readOnlyHint`` (the MCP standard hint) wins; the legacy
    top-level ``readOnly`` flag is the fallback. Anything else — no hint at
    all — counts as a write, the conservative reading for retry advice.
    """
    annotations = definition.get("annotations")
    hint = annotations.get("readOnlyHint") if isinstance(annotations, dict) else None
    if hint is None:
        hint = definition.get("readOnly")
    return hint is True


@functools.cache
def _tool_hints() -> dict[str, bool]:
    from mcp_server.tools._definitions import get_tool_definitions

    return {d["name"]: read_only_hint(d) for d in get_tool_definitions()}


def is_known_tool(tool_name: object) -> bool:
    return isinstance(tool_name, str) and tool_name in _tool_hints()


def is_read_only_tool(tool_name: object) -> bool:
    """True only for a registered tool whose definition says it is read-only."""
    return isinstance(tool_name, str) and _tool_hints().get(tool_name, False)


# ============================================================================
# Classification
# ============================================================================


@functools.cache
def _timeout_types() -> tuple[type[BaseException], ...]:
    # ``asyncio.TimeoutError`` is the builtin on 3.11+, so this covers
    # ``execute_with_timeout`` as well as socket-level timeouts.
    types: list[type[BaseException]] = [TimeoutError]
    try:
        import httpx

        types.append(httpx.TimeoutException)
    except ImportError:  # pragma: no cover - dependency is always installed
        pass
    try:
        import redis.exceptions

        types.append(redis.exceptions.TimeoutError)
    except ImportError:  # pragma: no cover
        pass
    return tuple(types)


@functools.cache
def _dependency_types() -> tuple[type[BaseException], ...]:
    """Exceptions meaning a backing service could not be reached or used."""
    from sqlalchemy import exc as sa_exc

    from utils.exceptions import DatabaseConnectionError, ExternalServiceError

    types: list[type[BaseException]] = [
        ConnectionError,
        socket.gaierror,
        DatabaseConnectionError,
        ExternalServiceError,
        sa_exc.OperationalError,
        sa_exc.InterfaceError,
        sa_exc.DisconnectionError,
        sa_exc.TimeoutError,  # connection-pool checkout timeout
    ]
    try:
        import httpx

        types.append(httpx.TransportError)
    except ImportError:  # pragma: no cover
        pass
    try:
        from qdrant_client.http.exceptions import ResponseHandlingException

        types.append(ResponseHandlingException)
    except ImportError:  # pragma: no cover
        pass
    try:
        import redis.exceptions

        types.append(redis.exceptions.ConnectionError)
    except ImportError:  # pragma: no cover
        pass
    return tuple(types)


def classify_cause(exc: BaseException) -> str:
    """Map a server-side exception to ``timeout`` / ``service_unavailable`` /
    ``internal_error``."""
    if isinstance(exc, _timeout_types()):
        return CAUSE_TIMEOUT
    if isinstance(exc, _dependency_types()):
        return CAUSE_SERVICE_UNAVAILABLE
    return CAUSE_INTERNAL_ERROR


def _is_refusal(exc: BaseException) -> bool:
    from utils.exceptions import MemoryCloudException

    if isinstance(exc, MemoryCloudException):
        return exc.status_code < 500
    # Exact type: a ValueError *subclass* (UnicodeDecodeError, JSONDecodeError,
    # pydantic's ValidationError …) is library detail, not a message for the caller.
    return type(exc) is ValueError or isinstance(exc, PermissionError)


def _refusal(exc: BaseException) -> tuple[str, str, dict[str, Any]]:
    """``(vocabulary code, message, extra fields)`` for a refusal."""
    from utils.exceptions import (
        AdminProtectionError,
        AuthorizationError,
        FeatureNotAvailableError,
        MemoryCloudException,
        QuotaExceededError,
        RateLimitError,
    )

    if not isinstance(exc, MemoryCloudException):
        if isinstance(exc, PermissionError):
            # An OSError: its text can name a server path, so it is never echoed.
            return "permission_denied", "Permission denied for this operation.", {}
        return "validation_error", str(exc), {}

    if isinstance(exc, QuotaExceededError):
        code = "quota_exceeded"
    elif isinstance(exc, RateLimitError):
        code = "rate_limit_exceeded"
    elif isinstance(exc, FeatureNotAvailableError):
        code = "feature_not_available"
    elif exc.status_code in (401, 403):
        code = "permission_denied"
    elif exc.status_code in (404, 410):
        code = "not_found"
    elif exc.status_code == 409:
        code = "conflict"
    else:
        code = "validation_error"
    # CWE-639: deny-class exceptions never forward ``details`` (same strip as
    # the REST handler), so a deny sub-reason cannot leak.
    if isinstance(exc, (AuthorizationError, AdminProtectionError)):
        details: dict[str, Any] = {}
    else:
        details = {
            k: v for k, v in exc.details.items() if v is not None and k not in _RESERVED_KEYS
        }
    return code, exc.message, details


def _shown(tool_name: object) -> str:
    """Log-safe rendering of a (possibly client-supplied) tool name."""
    return repr(str(tool_name)[:100])


def _server_failure_message(tool_name: object, cause: str) -> str:
    subject = tool_name if is_known_tool(tool_name) else "The tool call"
    if cause == CAUSE_TIMEOUT:
        return f"{subject} did not finish within the server's time limit."
    if cause == CAUSE_SERVICE_UNAVAILABLE:
        return (
            f"{subject} could not reach a service it depends on "
            "(database, search index or model provider)."
        )
    return f"{subject} failed because of an unexpected server error."


def _write_help(tool_name: object) -> str:
    verify = _VERIFY_WITH.get(tool_name) if isinstance(tool_name, str) else None
    check = f"Check with {verify}" if verify else "Check the current state"
    return (
        "This tool changes data, and the change may or may not have been applied. "
        f"{check} before calling it again, so it is not applied twice. "
        "If it keeps failing, report the correlation_id."
    )


def new_correlation_id() -> str:
    """The request's trace id when the transport set one, else a random id.

    Reusing the W3C trace id (#1277) joins the error with the request's audit
    rows and traces; the fallback covers direct handler calls.
    """
    try:
        from api.correlation import get_correlation

        correlation = get_correlation()
    except Exception:  # pragma: no cover - correlation is advisory
        correlation = None
    if correlation is not None and correlation.trace_id:
        return correlation.trace_id
    return secrets.token_hex(8)


# ============================================================================
# Public entry points
# ============================================================================


def describe_tool_exception(
    tool_name: object,
    exc: BaseException,
    *,
    error: str | None = None,
) -> ToolFailure:
    """Classify ``exc`` raised while running ``tool_name`` and log it.

    Args:
        tool_name: The tool being called. Client-supplied on the transport
            path, so it may be missing, unknown or not a string.
        exc: The exception.
        error: The handler's legacy per-tool code (``secret_put_error``,
            ``merge_contexts_error`` …), kept as the envelope's ``error`` so
            existing client branches still match. ``None`` (dispatch and
            transports) uses the vocabulary code.

    Returns:
        The failure, for ``.response()`` (tool result) or ``.jsonrpc_data()``.
    """
    if _is_refusal(exc):
        code, message, details = _refusal(exc)
        # Expected: the caller's request was refused. No traceback.
        logger.warning(
            "mcp_tool_refused: tool=%s error=%s exc_type=%s",
            _shown(tool_name),
            error or code,
            type(exc).__name__,
        )
        return ToolFailure(error or code, message, {"help": _REFUSAL_HELP[code], **details})

    cause = classify_cause(exc)
    correlation_id = new_correlation_id()
    read_only = is_read_only_tool(tool_name)
    # The only place the exception itself is recorded: text + traceback in the
    # server log, keyed by the correlation_id the caller receives.
    logger.error(
        "mcp_tool_failed: tool=%s error=%s cause=%s correlation_id=%s exc_type=%s exc=%s",
        _shown(tool_name),
        error or cause,
        cause,
        correlation_id,
        type(exc).__name__,
        exc,
        exc_info=exc,
    )
    fields: dict[str, Any] = {
        "cause": cause,
        "help": _READ_HELP[cause] if read_only else _write_help(tool_name),
        "correlation_id": correlation_id,
        "retryable": read_only,
    }
    if read_only and cause != CAUSE_INTERNAL_ERROR:
        fields["retry_after_seconds"] = RETRY_AFTER_SECONDS
    if not read_only:
        fields["outcome"] = "unknown"
    return ToolFailure(error or cause, _server_failure_message(tool_name, cause), fields)


def _tool_exception_response(
    tool_name: str,
    exc: BaseException,
    *,
    error: str | None = None,
) -> ToolErrorContent:
    """The ``isError`` envelope for an exception a tool handler raised.

    See :func:`describe_tool_exception` for ``error``.
    """
    if isinstance(exc, _ContextNotFoundError):
        return exc.to_response()
    return describe_tool_exception(tool_name, exc, error=error).response()
