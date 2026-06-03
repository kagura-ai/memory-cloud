"""MCP tool for the retrieval feedback signal (Issue #888, epic #885).

``feedback`` records an append-only "was this recalled memory useful for this
query" event. The signal lives in its own table and is never embedded, so it
cannot pollute ``recall()``.

Access mirrors the read path: ``_resolve_context_for_read`` verifies the caller
can reach the context (uniform ``context_not_found`` on deny — CWE-639). Recording
feedback is read-adjacent (anyone who can read the context consumes recall and may
rate it), so no viewer write-gate is applied.
"""

from typing import Any
from uuid import UUID

from mcp.types import TextContent

from mcp_server.tools._helpers import (
    _ContextNotFoundError,
    _error_response,
    _resolve_context_for_read,
    _resolve_context_id,
    _success_response,
)
from utils.exceptions import NotFoundException

# Matches retrieval_feedback.query VARCHAR(1024). Enforced here so an overlong
# query returns a structured error instead of a DB-layer 500.
_QUERY_MAX_LEN = 1024


async def handle_feedback(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Record retrieval feedback for a recalled memory (append-only)."""
    if "memory_id" not in args or "helpful" not in args:
        return _error_response("missing_fields", "Missing required fields: memory_id, helpful")

    helpful = args["helpful"]
    if not isinstance(helpful, bool):
        return _error_response("validation_error", "'helpful' must be a boolean")

    raw_memory_id = args["memory_id"]
    try:
        memory_id = UUID(str(raw_memory_id))
    except (ValueError, TypeError):
        return _error_response("validation_error", "'memory_id' must be a valid UUID")

    query = args.get("query")
    if query is not None and (not isinstance(query, str) or len(query) > _QUERY_MAX_LEN):
        return _error_response(
            "validation_error",
            f"'query' must be a string of at most {_QUERY_MAX_LEN} characters",
        )
    note = args.get("note")
    if note is not None and not isinstance(note, str):
        return _error_response("validation_error", "'note' must be a string")

    from db.base import get_db
    from services.feedback_service import FeedbackService

    async for db in get_db():
        raw_ctx = args.get("context_id")
        if not raw_ctx:
            return _error_response("missing_fields", "Missing required field: context_id")
        try:
            context_id = _resolve_context_id(str(raw_ctx))
        except ValueError as exc:
            return _error_response("invalid_context_id_format", str(exc))
        try:
            # Read-adjacent: verify the caller can reach the context (IDOR guard).
            await _resolve_context_for_read(db, user_id, context_id)
        except _ContextNotFoundError as exc:
            return exc.to_response()

        try:
            row = await FeedbackService(db).record_feedback(
                context_id=context_id,
                memory_id=memory_id,
                helpful=helpful,
                user_id=user_id,
                query=query,
                note=note,
            )
        except NotFoundException:
            # memory_id does not belong to this context (uniform, IDOR-safe).
            return _error_response("not_found", "Memory not found in this context")
        return _success_response(feedback_id=str(row.id), memory_id=str(memory_id), helpful=helpful)

    return _error_response("internal_error", "Database session unavailable")
