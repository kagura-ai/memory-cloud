"""``list_revisions`` — a memory's revision log (#2043).

One item per write event, newest first: ``created``, ``updated`` (with the
fields it changed), ``forgotten``, ``restored``, each with the version after
it. ``reference(memory_id, version=N)`` reads an earlier version's text. The
gate is ``reference``'s: the context resolves like every read tool and the
memory-level access check runs in ``MemoryService.list_revisions``; a
forgotten memory, or one outside the named context, answers
``memory_not_found``, so its log is unreadable too.
"""

from __future__ import annotations

import time
from typing import Any
from uuid import UUID

from mcp.types import TextContent

from mcp_server.tools._helpers import (
    _context_response_fields,
    _ContextNotFoundError,
    _dumps,
    _error_response,
    _log_tool_usage,
    _resolve_context_for_read,
    _resolve_context_id,
    _validate_memory_id,
    execute_with_timeout,
    get_mcp_key_workspace_scope,
)
from utils.datetime import to_utc_iso
from utils.exceptions import NotFoundException
from utils.response_budget import (
    BudgetArgumentError,
    fit_items,
    parse_limit,
    parse_max_chars,
    parse_offset_cursor,
)

DEFAULT_LIMIT = 20
MAX_LIMIT = 100
# The keyset cursor is a revision id (BIGSERIAL); 18 digits keeps it inside
# BIGINT, so a forged cursor is refused here, not by the database.
_MAX_CURSOR_DIGITS = 18


def _parse_cursor(raw: Any) -> int | None:
    """The ``next_cursor`` of a previous page (a revision id), or None."""
    if raw is None or raw == "":
        return None
    value = parse_offset_cursor(raw)  # the same digits-only token as list's
    if len(raw) > _MAX_CURSOR_DIGITS:
        raise BudgetArgumentError("cursor must be the next_cursor of a previous response.", raw)
    return value


def _item(revision: Any) -> dict[str, Any]:
    item = revision.model_dump(exclude_none=True)
    item["at"] = to_utc_iso(revision.at)
    return item


async def handle_list_revisions(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """A memory's revision log, newest first, keyset-paged and bounded (#2043)."""
    memory_uuid, error = _validate_memory_id(args, "list_revisions")
    if error or memory_uuid is None:
        return error or _error_response("invalid_memory_id_format", "Invalid memory_id")
    try:
        limit = parse_limit(args.get("limit"), default=DEFAULT_LIMIT, maximum=MAX_LIMIT)
        before_id = _parse_cursor(args.get("cursor"))
        max_chars = parse_max_chars(args.get("max_chars"))
    except BudgetArgumentError as e:
        return _error_response("validation_error", e.message, received=e.received)

    from db.base import get_db
    from services.memory_service import MemoryService

    start = time.time()
    async for db in get_db():
        context_id: UUID | None = None
        try:
            context_id = _resolve_context_id(args["context_id"])
            # The revision log is metadata of one memory: it audits as a
            # reference read (no memory_access_events operation of its own).
            context = await _resolve_context_for_read(
                db, user_id, context_id, operation="reference"
            )
            try:
                page = await execute_with_timeout(
                    MemoryService(db).list_revisions(
                        memory_uuid,
                        user_id=user_id,
                        before_id=before_id,
                        limit=limit,
                        context_id=context_id,
                        key_workspace_id=get_mcp_key_workspace_scope(),
                    ),
                    operation_name="list_revisions",
                )
            except NotFoundException:
                await db.rollback()
                await _log_tool_usage(
                    db, user_id, "list_revisions", start, 404, context_id, workspace_id
                )
                return _error_response(
                    "memory_not_found",
                    f"Memory not found or you don't have access: {memory_uuid}",
                    help="Use recall() to find memories you have access to.",
                )
            await _log_tool_usage(
                db, user_id, "list_revisions", start, 200, context_id, workspace_id
            )
            await db.commit()

            items = [_item(r) for r in page.revisions]
            envelope: dict[str, Any] = {
                "status": "success",
                "memory_id": str(page.memory_id),
                "current_version": page.current_version,
                "revisions": items,
                "count": len(items),
                "has_more": page.has_more,
                "next_cursor": page.next_cursor,
                **_context_response_fields(context),
            }
            # Items are small (no text), so the budget only bites at a large
            # limit with a small max_chars: keep the prefix that fits (at least
            # one item) and continue from the last one kept.
            shell = {
                **envelope,
                "revisions": [],
                "truncated": True,
                "next_cursor": "9" * _MAX_CURSOR_DIGITS,
            }
            kept = max(1, fit_items(items, max_chars - len(_dumps(shell)))) if items else 0
            if kept < len(items):
                envelope["revisions"] = items[:kept]
                envelope["count"] = kept
                envelope["truncated"] = True
                envelope["has_more"] = True
                envelope["next_cursor"] = page.revisions[kept - 1]._cursor
            return [TextContent(type="text", text=_dumps(envelope))]
        except _ContextNotFoundError as e:
            await db.rollback()
            await _log_tool_usage(
                db, user_id, "list_revisions", start, 404, context_id, workspace_id
            )
            return e.to_response()
        except Exception:
            await db.rollback()
            await _log_tool_usage(
                db, user_id, "list_revisions", start, 500, context_id, workspace_id
            )
            raise
    return _error_response("internal_error", "Database session unavailable")
