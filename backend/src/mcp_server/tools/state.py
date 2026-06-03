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


async def handle_set_state(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Upsert an agent-state value at ``(context_id, key)`` with an optional TTL."""
    if "key" not in args or "value" not in args:
        return _error_response("missing_fields", "Missing required fields: key, value")

    from db.base import get_db
    from services.agent_state_service import AgentStateService

    async for db in get_db():
        context_id = _resolve_context_id(args["context_id"])
        try:
            # Verify the caller can reach the context (IDOR guard) ...
            await _resolve_context_for_read(db, user_id, context_id)
        except _ContextNotFoundError as exc:
            return exc.to_response()
        # ... and is not a read-only viewer (write gate, mirrors remember).
        perm_error = await _check_viewer_permission(db, user_id, workspace_id, "set agent state")
        if perm_error:
            return perm_error

        await AgentStateService(db).set_state(
            context_id,
            args["key"],
            args["value"],
            ttl_seconds=args.get("ttl_seconds"),
        )
        # Use the helper's standard {"status":"success"} envelope (consistent
        # with get_state and the rest of the MCP tools / tests).
        return _success_response(key=args["key"])

    return _error_response("internal_error", "Database session unavailable")


async def handle_get_state(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Read one key's live value, or list all live entries when ``key`` is omitted."""
    from db.base import get_db
    from services.agent_state_service import AgentStateService

    async for db in get_db():
        context_id = _resolve_context_id(args["context_id"])
        try:
            await _resolve_context_for_read(db, user_id, context_id)
        except _ContextNotFoundError as exc:
            return exc.to_response()

        service = AgentStateService(db)
        key = args.get("key")
        if key:
            value = await service.get_state(context_id, key)
            return _success_response(key=key, value=value, found=value is not None)

        states = await service.list_state(context_id)
        return _success_response(states=states, count=len(states))

    return _error_response("internal_error", "Database session unavailable")
