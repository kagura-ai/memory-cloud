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
the read that shows whether it took effect. A few writes are safe to repeat
(``_REPEAT_SAFE_WRITES``) and are advised like reads.
"""

from __future__ import annotations

import functools
import secrets
import socket
from dataclasses import dataclass
from typing import Any

import httpx
import redis.exceptions
from qdrant_client.http.exceptions import ResponseHandlingException
from sqlalchemy import exc as sa_exc

from mcp_server.tools._helpers import ToolErrorContent, _ContextNotFoundError, _error_response
from utils.exceptions import (
    AdminProtectionError,
    AuthorizationError,
    DatabaseConnectionError,
    ExternalServiceError,
    FeatureNotAvailableError,
    MemoryCloudException,
    QuotaExceededError,
    RateLimitError,
)
from utils.logger import get_logger

logger = get_logger(__name__)

CAUSE_TIMEOUT = "timeout"
CAUSE_SERVICE_UNAVAILABLE = "service_unavailable"
CAUSE_INTERNAL_ERROR = "internal_error"

# Same wait the REST database-unavailable 503 advertises in ``Retry-After``.
RETRY_AFTER_SECONDS = 5

# ``asyncio.TimeoutError`` is the builtin on 3.11+, so this covers
# ``execute_with_timeout`` as well as socket-level timeouts.
_TIMEOUT_TYPES: tuple[type[BaseException], ...] = (
    TimeoutError,
    httpx.TimeoutException,
    redis.exceptions.TimeoutError,
)

# A backing service could not be reached or used.
_DEPENDENCY_TYPES: tuple[type[BaseException], ...] = (
    ConnectionError,
    socket.gaierror,
    DatabaseConnectionError,
    ExternalServiceError,
    sa_exc.OperationalError,
    sa_exc.InterfaceError,
    sa_exc.DisconnectionError,
    sa_exc.TimeoutError,  # connection-pool checkout timeout
    httpx.TransportError,
    ResponseHandlingException,
    redis.exceptions.ConnectionError,
)

# Envelope keys a forwarded ``details`` block must not overwrite.
_RESERVED_KEYS = frozenset({"status", "error", "message"})

# The read that shows whether a write took effect, named in ``help`` when a
# write fails with an unknown outcome. ``test_tool_errors.py`` keeps every name
# registered and requires every write to appear here, in
# ``_REPEAT_SAFE_WRITES`` or in ``_UNVERIFIABLE_WRITES``.
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
    "setup_connector": "list_resource_tokens",
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

# Writes a repeat cannot turn into a duplicate: they get read-style advice
# (``retryable: true``) and say why. ``recall`` / ``get_agent_bootstrap`` only
# write ranking-learning state (#1683 marks them not read-only), which any
# repeated query updates the same way; a pubkey already registered is refused.
_REPEAT_SAFE_WRITES: dict[str, str] = {
    "recall": "This tool searches; its only writes are ranking updates, which a repeat applies "
    "like any other query.",
    "get_agent_bootstrap": "This tool reads the agent's starting context; its only writes are "
    "the ranking updates of its recall.",
    "secret_register_pubkey": "Registering the same key again is refused as a duplicate, never "
    "stored twice.",
}

# Writes with no read that shows their outcome: ``help`` says what a repeat does.
_UNVERIFIABLE_WRITES: dict[str, str] = {
    "feedback": (
        "Feedback is append-only and no tool reads it back, so calling it again may record the "
        "rating twice. Skip the retry unless the rating matters."
    ),
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

_READ_ONLY = "This tool only reads, so"
_RETRY_TRANSIENT = (
    "calling it again is safe. Wait a few seconds and retry; "
    "if it keeps failing, report the correlation_id."
)
_RETRY_INTERNAL = (
    "one more attempt is safe. If it fails again, stop retrying and report the correlation_id."
)


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
    # Lazy: ``_definitions`` is loaded by the tools package that imports us.
    from mcp_server.tools._definitions import get_tool_definitions

    return {d["name"]: read_only_hint(d) for d in get_tool_definitions()}


def is_known_tool(tool_name: object) -> bool:
    return isinstance(tool_name, str) and tool_name in _tool_hints()


def is_read_only_tool(tool_name: object) -> bool:
    """True only for a registered tool whose definition says it is read-only."""
    return isinstance(tool_name, str) and _tool_hints().get(tool_name, False)


def _is_repeat_safe(tool_name: object) -> bool:
    return is_read_only_tool(tool_name) or (
        isinstance(tool_name, str) and tool_name in _REPEAT_SAFE_WRITES
    )


# ============================================================================
# Classification
# ============================================================================


def classify_cause(exc: BaseException) -> str:
    """Map a server-side exception to ``timeout`` / ``service_unavailable`` /
    ``internal_error``."""
    if isinstance(exc, _TIMEOUT_TYPES):
        return CAUSE_TIMEOUT
    if isinstance(exc, _DEPENDENCY_TYPES):
        return CAUSE_SERVICE_UNAVAILABLE
    return CAUSE_INTERNAL_ERROR


def _is_refusal(exc: BaseException, *, echo_value_error: bool) -> bool:
    if isinstance(exc, MemoryCloudException):
        return exc.status_code < 500
    if isinstance(exc, PermissionError):
        return True
    # Exact type: a ValueError *subclass* (UnicodeDecodeError, JSONDecodeError,
    # pydantic's ValidationError …) is library detail, not a message for the caller.
    return echo_value_error and type(exc) is ValueError


def _refusal(exc: BaseException) -> tuple[str, str, dict[str, Any]]:
    """``(vocabulary code, message, extra fields)`` for a refusal."""
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
    name = str(tool_name)[:100]
    return name if name.isprintable() else repr(name)


def _server_failure_message(tool_name: object, cause: str) -> str:
    subject = tool_name if is_known_tool(tool_name) else "The tool call"
    if cause == CAUSE_TIMEOUT:
        return f"{subject} did not finish within the server's time limit."
    if cause == CAUSE_SERVICE_UNAVAILABLE:
        return (
            f"{subject} could not reach a service it depends on "
            "(database, search index, file storage or model provider)."
        )
    return f"{subject} failed because of an unexpected server error."


def _retry_help(tool_name: object, cause: str) -> str:
    """``help`` for a tool whose repeat is safe (a read, or a repeat-safe write)."""
    retry = _RETRY_INTERNAL if cause == CAUSE_INTERNAL_ERROR else _RETRY_TRANSIENT
    why = _REPEAT_SAFE_WRITES.get(tool_name) if isinstance(tool_name, str) else None
    if why is None:
        return f"{_READ_ONLY} {retry}"
    return f"{why} So {retry}"


def _write_help(tool_name: object) -> str:
    name = tool_name if isinstance(tool_name, str) else ""
    if name in _UNVERIFIABLE_WRITES:
        return f"{_UNVERIFIABLE_WRITES[name]} If it keeps failing, report the correlation_id."
    verify = _VERIFY_WITH.get(name)
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
    echo_value_error: bool = True,
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
        echo_value_error: ``False`` treats a plain ``ValueError`` as a server
            failure instead of a refusal, so its text is logged, not returned.
            For the handlers whose catch-all always returned a fixed message
            (#1247): a ``ValueError`` reaching them is not a designed refusal.

    Returns:
        The failure, for ``.response()`` (tool result) or ``.jsonrpc_data()``.
    """
    if _is_refusal(exc, echo_value_error=echo_value_error):
        code, message, details = _refusal(exc)
        if isinstance(exc, MemoryCloudException):
            # A designed refusal: its message is the whole story.
            logger.warning(
                "mcp_tool_refused",
                tool=_shown(tool_name),
                error=error or code,
                exc_type=type(exc).__name__,
            )
        else:
            # A builtin ValueError / PermissionError can also be a server bug
            # (bad stored data, a path the process cannot read): keep its text
            # and traceback in the log, since the caller gets a fixed message
            # (PermissionError) or only the text.
            logger.warning(
                "mcp_tool_refused",
                tool=_shown(tool_name),
                error=error or code,
                exc_type=type(exc).__name__,
                exc=str(exc),
                exc_info=exc,
            )
        return ToolFailure(error or code, message, {"help": _REFUSAL_HELP[code], **details})

    cause = classify_cause(exc)
    correlation_id = new_correlation_id()
    repeat_safe = _is_repeat_safe(tool_name)
    # The only place the exception itself is recorded: text + traceback in the
    # server log, keyed by the correlation_id the caller receives.
    logger.error(
        "mcp_tool_failed",
        tool=_shown(tool_name),
        error=error or cause,
        cause=cause,
        correlation_id=correlation_id,
        exc_type=type(exc).__name__,
        exc=str(exc),
        exc_info=exc,
    )
    fields: dict[str, Any] = {
        "cause": cause,
        "help": _retry_help(tool_name, cause) if repeat_safe else _write_help(tool_name),
        "correlation_id": correlation_id,
        "retryable": repeat_safe,
    }
    if repeat_safe and cause != CAUSE_INTERNAL_ERROR:
        fields["retry_after_seconds"] = RETRY_AFTER_SECONDS
    if not repeat_safe:
        fields["outcome"] = "unknown"
    return ToolFailure(error or cause, _server_failure_message(tool_name, cause), fields)


def insufficient_scope_failure(tool_name: object, required_scope: str) -> ToolFailure:
    """A ``tools/call`` refused before dispatch: the OAuth token lacks ``required_scope``.

    The transports send it as HTTP 403 with an ``insufficient_scope``
    challenge (#1686); nothing ran, so there is no outcome to verify.
    """
    subject = tool_name if is_known_tool(tool_name) else "This tool call"
    return ToolFailure(
        "insufficient_scope",
        f"{subject} needs the OAuth scope {required_scope}, which this connection's "
        "authorization does not grant.",
        {
            "required_scope": required_scope,
            "help": (
                f"Reconnect this server in your MCP client and approve access that includes "
                f"{required_scope}, then call the tool again. Tools the current authorization "
                "covers keep working."
            ),
        },
    )


def _tool_exception_response(
    tool_name: str,
    exc: BaseException,
    *,
    error: str | None = None,
    echo_value_error: bool = True,
) -> ToolErrorContent:
    """The ``isError`` envelope for an exception a tool handler raised.

    See :func:`describe_tool_exception` for ``error`` and ``echo_value_error``.
    """
    if isinstance(exc, _ContextNotFoundError):
        return exc.to_response()
    return describe_tool_exception(
        tool_name, exc, error=error, echo_value_error=echo_value_error
    ).response()
