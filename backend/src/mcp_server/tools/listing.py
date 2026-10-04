"""``list`` and ``changes_since`` — the deterministic read lanes (#1852).

Both resolve the context like every read tool, scope a private context to the
caller's identity-link set, and return bounded envelopes; neither ranks, embeds
or writes (no Hebbian side effect). The SQL lives in ``services.memory_listing``.
"""

from __future__ import annotations

import time
from typing import Any
from uuid import UUID

from mcp.types import TextContent

from mcp_server.tools._errors import _tool_exception_response, is_caller_value_error
from mcp_server.tools._helpers import (
    _context_response_fields,
    _ContextNotFoundError,
    _dumps,
    _error_response,
    _log_tool_usage,
    _resolve_context_for_read,
    _resolve_context_id,
    execute_with_timeout,
)
from services.memory_listing import (
    CHANGE_CURSOR_RESERVE,
    DEFAULT_LIMIT,
    DIRECTIONS,
    KINDS,
    MAX_LIMIT,
    ORDER_COLUMNS,
    change_item,
    changes_since,
    compile_memory_filters,
    encode_change_cursor,
    list_memories,
    to_naive_utc,
)
from utils.datetime import parse_iso8601_to_aware, to_utc_iso
from utils.response_budget import (
    BudgetArgumentError,
    fit_items,
    parse_limit,
    parse_max_chars,
    parse_offset_cursor,
)


def _memory_item(m: Any, *, include_details: bool) -> dict[str, Any]:
    item: dict[str, Any] = {
        "memory_id": str(m.id),
        "summary": m.summary,
        "type": m.type,
        "importance": m.importance,
        "scope": m.scope,
        "tags": m.tags or [],
        "delivery_mode": m.delivery_mode,
        "source_type": m.source_type,
        "created_at": to_utc_iso(m.created_at),
        "updated_at": to_utc_iso(m.updated_at),
    }
    if include_details:
        item["details"] = m.details
    return item


# list's offset cursor is shorter than the change log's keyset token; one reserve serves both.
CURSOR_RESERVE = CHANGE_CURSOR_RESERVE


def _bound(envelope: dict[str, Any], lane: str, max_chars: int) -> tuple[dict[str, Any], int]:
    """Keep the ``lane`` prefix that fits ``max_chars``; drop ``details`` first.

    Returns the envelope and how many items were kept, so the caller can point
    ``next_cursor`` at the first item that was cut — a truncated page must not
    skip rows. At least one item is kept even when it alone exceeds the budget.
    """
    items = envelope[lane]
    # Measure against the shell the caller may still grow: the two flags and a
    # continuation cursor are appended after the cut, so reserve their room —
    # a page that fit by a few characters must not overshoot once flagged.
    shell = {**envelope, lane: [], "truncated": True, "details_omitted": True}
    shell["next_cursor"] = "x" * max(len(str(envelope.get("next_cursor") or "")), CURSOR_RESERVE)
    budget = max_chars - len(_dumps(shell))
    if fit_items(items, budget) < len(items) and any("details" in it for it in items):
        envelope[lane] = items = [{k: v for k, v in it.items() if k != "details"} for it in items]
        envelope["details_omitted"] = True
    kept = max(1, fit_items(items, budget)) if items else 0
    if kept < len(items):
        envelope[lane] = items[:kept]
        envelope["truncated"] = True
    return envelope, kept


async def handle_list(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Every live memory of a context matching exact filters, ordered and paged (#1852)."""
    from db.base import get_db

    if "context_id" not in args:
        return _error_response("missing_fields", "Missing required field: context_id")
    order_by = args.get("order_by", "updated_at")
    direction = args.get("direction", "desc")
    if (
        not isinstance(order_by, str)
        or order_by not in ORDER_COLUMNS
        or not isinstance(direction, str)
        or direction not in DIRECTIONS
    ):
        return _error_response(
            "validation_error",
            f"order_by must be one of {', '.join(ORDER_COLUMNS)} and direction 'asc' or 'desc'.",
        )
    include_details = args.get("include_details", False)
    if not isinstance(include_details, bool):
        return _error_response("validation_error", "include_details must be a boolean.")
    try:
        limit = parse_limit(args.get("limit"), default=DEFAULT_LIMIT, maximum=MAX_LIMIT)
        offset = parse_offset_cursor(args.get("cursor"))
        max_chars = parse_max_chars(args.get("max_chars"))
    except BudgetArgumentError as e:
        return _error_response("validation_error", e.message, received=e.received)
    try:
        # Pure: a bad filter is refused before any database round trip.
        predicates = compile_memory_filters(args.get("filters"))
    except ValueError as e:
        return _error_response("validation_error", str(e))
    start = time.time()
    async for db in get_db():
        context_id: UUID | None = None
        try:
            context_id = _resolve_context_id(args["context_id"])
            context = await _resolve_context_for_read(db, user_id, context_id, operation="list")
            owner = user_id if context.is_private else None
            page = await execute_with_timeout(
                list_memories(
                    db,
                    context_id=context_id,
                    owner_user_id=owner,
                    predicates=predicates,
                    order_by=order_by,
                    direction=direction,
                    offset=offset,
                    limit=limit,
                ),
                operation_name="list",
            )
            await _log_tool_usage(db, user_id, "list", start, 200, context_id, workspace_id)
            envelope: dict[str, Any] = {
                "status": "success",
                "memories": [_memory_item(m, include_details=include_details) for m in page.rows],
                "count": len(page.rows),
                "total": page.total,
                "has_more": page.has_more,
                "next_cursor": None,
                "order_by": order_by,
                "direction": direction,
                **_context_response_fields(context),
            }
            envelope, kept = _bound(envelope, "memories", max_chars)
            # A cut page continues from the first row that was cut, never from offset+limit.
            envelope["count"] = kept
            if kept < len(page.rows) or page.has_more:
                envelope["has_more"] = True
                envelope["next_cursor"] = str(offset + kept)
            return [TextContent(type="text", text=_dumps(envelope))]
        except _ContextNotFoundError as e:
            await _log_tool_usage(db, user_id, "list", start, 404, context_id, workspace_id)
            return e.to_response()
        except ValueError as e:
            if not is_caller_value_error(e):
                await _log_tool_usage(db, user_id, "list", start, 500, context_id, workspace_id)
                return _tool_exception_response("list", e)
            await _log_tool_usage(db, user_id, "list", start, 422, context_id, workspace_id)
            return _error_response("validation_error", str(e))
    return _error_response("internal_error", "Database session unavailable")


async def handle_changes_since(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """A context's memory change log since a time, oldest first, keyset-paged (#1852)."""
    from db.base import get_db

    if "context_id" not in args:
        return _error_response("missing_fields", "Missing required field: context_id")
    if not isinstance(args.get("since"), str):
        return _error_response("missing_fields", "Missing required field: since (ISO 8601)")
    until_arg = args.get("until")
    if until_arg is not None and not isinstance(until_arg, str):
        return _error_response("validation_error", "until must be an ISO 8601 string.")
    try:
        since = parse_iso8601_to_aware(args["since"], "since")
        until = parse_iso8601_to_aware(until_arg, "until") if until_arg else None
    except ValueError as e:
        return _error_response("validation_error", str(e))
    if until is not None and until <= since:
        return _error_response("validation_error", "until must be later than since.")
    kinds_arg = args.get("kinds")
    if kinds_arg is None:
        kinds: tuple[str, ...] = KINDS
    elif isinstance(kinds_arg, list) and kinds_arg and all(k in KINDS for k in kinds_arg):
        kinds = tuple(dict.fromkeys(kinds_arg))
    else:
        return _error_response(
            "validation_error", f"kinds must be a non-empty subset of {', '.join(KINDS)}."
        )
    cursor = args.get("cursor")
    if cursor is not None and not isinstance(cursor, str):
        return _error_response(
            "validation_error", "cursor must be the next_cursor of a previous response."
        )
    try:
        limit = parse_limit(args.get("limit"), default=DEFAULT_LIMIT, maximum=MAX_LIMIT)
        max_chars = parse_max_chars(args.get("max_chars"))
    except BudgetArgumentError as e:
        return _error_response("validation_error", e.message, received=e.received)
    since_naive = to_naive_utc(since)
    until_naive = to_naive_utc(until) if until else None
    start = time.time()
    async for db in get_db():
        context_id: UUID | None = None
        try:
            context_id = _resolve_context_id(args["context_id"])
            context = await _resolve_context_for_read(
                db, user_id, context_id, operation="changes_since"
            )
            owner = user_id if context.is_private else None
            page = await execute_with_timeout(
                changes_since(
                    db,
                    context_id=context_id,
                    owner_user_id=owner,
                    since=since_naive,
                    until=until_naive,
                    kinds=kinds,
                    cursor=cursor or None,
                    limit=limit,
                ),
                operation_name="changes_since",
            )
            await _log_tool_usage(
                db, user_id, "changes_since", start, 200, context_id, workspace_id
            )
            envelope: dict[str, Any] = {
                "status": "success",
                "changes": [change_item(c) for c in page.changes],
                "count": len(page.changes),
                "has_more": page.next_cursor is not None,
                "next_cursor": page.next_cursor,
                "since": to_utc_iso(since_naive),
                "until": to_utc_iso(until_naive),
                **_context_response_fields(context),
            }
            envelope, kept = _bound(envelope, "changes", max_chars)
            if kept < len(page.changes):
                # Continue from the last change that was kept, not from the page's end.
                envelope["count"] = kept
                envelope["has_more"] = True
                envelope["next_cursor"] = encode_change_cursor(page.changes[kept - 1])
            return [TextContent(type="text", text=_dumps(envelope))]
        except _ContextNotFoundError as e:
            await _log_tool_usage(
                db, user_id, "changes_since", start, 404, context_id, workspace_id
            )
            return e.to_response()
        except ValueError as e:
            if not is_caller_value_error(e):
                await _log_tool_usage(
                    db, user_id, "changes_since", start, 500, context_id, workspace_id
                )
                return _tool_exception_response("changes_since", e)
            await _log_tool_usage(
                db, user_id, "changes_since", start, 422, context_id, workspace_id
            )
            return _error_response("validation_error", str(e))
    return _error_response("internal_error", "Database session unavailable")
