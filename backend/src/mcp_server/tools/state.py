"""MCP tools for the agent session-state lane (Issue #889).

``set_state`` / ``get_state`` over the dedicated ``agent_states`` table — a
TTL-bounded run-state store that is structurally excluded from ``recall()``.

Access control composes two proven helpers (same posture as the memory write
path): ``_resolve_context_for_read`` verifies the caller can reach the context
at all (uniform ``context_not_found`` on deny — CWE-639), and, for writes,
``_check_viewer_permission`` blocks read-only viewers from mutating state.
"""

from typing import Any
from uuid import UUID

from mcp.types import TextContent

from mcp_server.tools._helpers import (
    _check_viewer_permission,
    _ContextNotFoundError,
    _error_response,
    _resolve_context_for_read,
    _resolve_context_id,
    _success_response,
)

# Matches the agent_states.key column length (VARCHAR(255)). Enforced in the
# handlers so an overlong key returns a structured error, not a DB-layer 500.
_STATE_KEY_MAX_LEN = 255


async def handle_set_state(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Upsert an agent-state value at ``(context_id, key)`` with an optional TTL."""
    if "key" not in args or "value" not in args:
        return _error_response("missing_fields", "Missing required fields: key, value")
    # These handlers have no pydantic request model, so validate the loosely
    # typed args explicitly rather than letting bad values reach SQL.
    key = args["key"]
    if not isinstance(key, str) or not key:
        return _error_response("validation_error", "'key' must be a non-empty string")
    if len(key) > _STATE_KEY_MAX_LEN:
        return _error_response(
            "validation_error", f"'key' must be at most {_STATE_KEY_MAX_LEN} characters"
        )
    value = args["value"]
    if value is None:
        # JSONB column is NOT NULL — reject up front instead of a generic 500.
        return _error_response("validation_error", "'value' must not be null")
    ttl_seconds = args.get("ttl_seconds")
    if ttl_seconds is not None and (
        isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds <= 0
    ):
        return _error_response("validation_error", "'ttl_seconds' must be a positive integer")

    from db.base import get_db
    from services.agent_state_service import AgentStateService

    async for db in get_db():
        # Defense-in-depth: the dispatcher already validates context_id for
        # non-exempt tools, but guard here too so a direct handler call returns
        # a structured error instead of raising KeyError/ValueError.
        raw_ctx = args.get("context_id")
        if not raw_ctx:
            return _error_response("missing_fields", "Missing required field: context_id")
        try:
            context_id = _resolve_context_id(str(raw_ctx))
        except ValueError as exc:
            return _error_response("invalid_context_id_format", str(exc))
        try:
            # Verify the caller can reach the context (IDOR guard) ...
            context = await _resolve_context_for_read(db, user_id, context_id)
        except _ContextNotFoundError as exc:
            return exc.to_response()
        # ... and is not a read-only viewer (write gate, mirrors remember).
        # workspace_id is often None under OAuth2 / session-cookie MCP auth,
        # which would make _check_viewer_permission short-circuit and skip the
        # write gate. Fall back to the resolved context's workspace so the gate
        # always fires against the authoritative workspace.
        effective_workspace_id = workspace_id or context.workspace_id
        perm_error = await _check_viewer_permission(
            db, user_id, effective_workspace_id, "set agent state"
        )
        if perm_error:
            return perm_error

        await AgentStateService(db).set_state(context_id, key, value, ttl_seconds=ttl_seconds)
        # Use the helper's standard {"status":"success"} envelope (consistent
        # with get_state and the rest of the MCP tools / tests).
        return _success_response(key=key)

    return _error_response("internal_error", "Database session unavailable")


async def handle_get_state(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Read one key's live value, or list all live entries when ``key`` is omitted."""
    from db.base import get_db
    from services.agent_state_service import AgentStateService

    async for db in get_db():
        # Defense-in-depth context_id guard (see handle_set_state).
        raw_ctx = args.get("context_id")
        if not raw_ctx:
            return _error_response("missing_fields", "Missing required field: context_id")
        try:
            context_id = _resolve_context_id(str(raw_ctx))
        except ValueError as exc:
            return _error_response("invalid_context_id_format", str(exc))
        try:
            await _resolve_context_for_read(db, user_id, context_id)
        except _ContextNotFoundError as exc:
            return exc.to_response()

        service = AgentStateService(db)
        # ``key`` is optional: a non-empty string reads one entry; omitting it
        # lists all. Reject a present-but-invalid key (non-string / empty) so an
        # empty string doesn't silently fall through to list-all.
        key = args.get("key")
        if key is not None and (
            not isinstance(key, str) or not key or len(key) > _STATE_KEY_MAX_LEN
        ):
            return _error_response(
                "validation_error",
                f"'key' must be a non-empty string of at most {_STATE_KEY_MAX_LEN} "
                "characters when provided",
            )
        if key:
            value = await service.get_state(context_id, key)
            return _success_response(key=key, value=value, found=value is not None)

        states = await service.list_state(context_id)
        return _success_response(states=states, count=len(states))

    return _error_response("internal_error", "Database session unavailable")
