"""MCP tool handlers: memory operations (remember, recall, forget, reference).

Extracted from tools.py for modularity (Issue #7).
"""

import logging
import time
from typing import Any
from uuid import UUID

from mcp.types import TextContent

from mcp_server.tools._helpers import (
    _check_viewer_permission,
    _context_response_fields,
    _ContextNotFoundError,
    _degraded_response_fields,
    _dumps,
    _error_response,
    _format_validation_error,
    _lint_response_field,
    _log_tool_usage,
    _persistence_response_field,
    _resolve_context,
    _resolve_context_for_read,
    _resolve_context_id,
    _touch_context_last_used,
    _validate_memory_id,
    execute_with_timeout,
)
from utils.datetime import to_utc_iso
from utils.exceptions import AuthorizationError, NotFoundException, QuotaExceededError

logger = logging.getLogger(__name__)

# #1228: server-side cap for cross-context recall — MUST stay in sync with
# the recall inputSchema's context_ids maxItems in _definitions.py.
MAX_CROSS_CONTEXT_IDS = 20


def _guardrail_author_denied(operation: str) -> list[TextContent]:
    """``permission_denied`` for a tool-guardrail write below context EDITOR.

    Same envelope shape as ``_check_viewer_permission``; the message is uniform
    (no deny sub-reason — the caller already proved the context exists via the
    resolution gate, so this adds no enumeration vector).
    """
    return _error_response(
        "permission_denied",
        f"Cannot {operation}: tool guardrails require context editor or above.",
        required_role="editor",
        help=(
            "A memory carrying details.tool_trigger is injected into every member's "
            "agent session by client hooks, so only a context editor/owner (or a "
            "workspace owner/admin) may add, change, remove or delete one."
        ),
    )


async def handle_remember(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Store a new memory."""
    if "summary" not in args or "content" not in args or "type" not in args:
        return _error_response(
            "missing_fields",
            "Missing required fields: summary, content, type",
        )

    from db.base import get_db
    from models.schemas import RememberRequest
    from services.memory_service import MemoryService

    request = RememberRequest(
        summary=args["summary"],
        context_summary=args.get("context_summary"),
        content=args["content"],
        details=args.get("details"),
        type=args["type"],
        importance=args.get("importance", 0.5),
        tags=args.get("tags", []),
        context=args.get("context"),
        delivery_mode=args.get("delivery_mode", "on_recall"),  # Issue #886
        source_uri=args.get("source_uri"),
        source_type=args.get("source_type"),
        linked_memory_ids=args.get("linked_memory_ids"),
        linked_source_uris=args.get("linked_source_uris"),
        supersedes=args.get("supersedes"),  # #1208
    )

    start_time = time.time()
    async for db in get_db():
        try:
            current_context_id = _resolve_context_id(args["context_id"])

            perm_error = await _check_viewer_permission(
                db, user_id, workspace_id, "create memories"
            )
            if perm_error:
                return perm_error

            current_context = await _resolve_context(
                db, user_id, current_context_id, operation="remember"
            )

            service = MemoryService(db)
            result = await execute_with_timeout(
                service.remember(
                    request,
                    user_id=user_id,
                    client="mcp",
                    current_context_id=current_context_id,
                    current_workspace_id=workspace_id,
                ),
                operation_name="remember",
            )

            await _log_tool_usage(
                db, user_id, "remember", start_time, 200, current_context_id, workspace_id
            )
            # #1257: memory ops mark the context as used (guarded UPDATE at
            # commit time — see the helper's contract).
            await _touch_context_last_used(db, current_context)
            await db.commit()

            return [
                TextContent(
                    type="text",
                    text=_dumps(
                        {
                            "status": "success",
                            "memory_id": str(result.memory_id),
                            "scope": result.scope,
                            # #1505: scope alone reads as "not saved yet" — say
                            # what it actually implies for durability.
                            **_persistence_response_field(result.persistence),
                            # #1502: advisory recall-ability hints; absent when
                            # the write looks fine.
                            **_lint_response_field(result.lint),
                            **_context_response_fields(current_context),
                        }
                    ),
                )
            ]
        except _ContextNotFoundError as e:
            await db.rollback()
            return e.to_response()
        except QuotaExceededError as e:
            # The quota family (total memory_limit, #1549 daily memories_per_day)
            # is a 429, not a crash: same ``quota_exceeded`` envelope as
            # analysis / files, with the structured details (quota_type, limit,
            # used_today, requested, resets_at) forwarded so clients can show
            # the reset time instead of parsing the message.
            await db.rollback()
            await _log_tool_usage(
                db, user_id, "remember", start_time, 429, args.get("context_id"), workspace_id
            )
            return _error_response(
                "quota_exceeded",
                e.message,
                **{k: v for k, v in e.details.items() if v is not None},
            )
        except ValueError as e:
            # MemoryService raises ValueError as its "bad request" signal (e.g.
            # an invalid type="time" details.trigger). Return a structured
            # validation_error rather than re-raising as an opaque tool crash.
            await db.rollback()
            await _log_tool_usage(
                db, user_id, "remember", start_time, 422, args.get("context_id"), workspace_id
            )
            return _error_response("validation_error", str(e))
        except AuthorizationError:
            # Tool guardrails: details.tool_trigger needs context EDITOR or
            # above — membership (already proven by the context resolution
            # above) is not enough. Uniform message: no deny sub-reason.
            await db.rollback()
            await _log_tool_usage(
                db, user_id, "remember", start_time, 403, args.get("context_id"), workspace_id
            )
            return _guardrail_author_denied("mark a memory as a tool guardrail")
        except Exception:
            await db.rollback()
            await _log_tool_usage(
                db,
                user_id,
                "remember",
                start_time,
                500,
                args.get("context_id"),
                workspace_id,
            )
            raise

    # Safety: should never reach here (get_db always yields)
    return _error_response("internal_error", "Database session unavailable")


async def handle_update_memory(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Update an existing memory or upsert by external ID."""
    from pydantic import ValidationError

    from db.base import get_db
    from models.schemas import UpdateMemoryRequest
    from services.memory_service import MemoryService

    memory_id = args.get("memory_id")
    external_id = args.get("external_id")

    try:
        request = UpdateMemoryRequest(
            memory_id=UUID(memory_id) if memory_id else None,
            external_id=external_id,
            summary=args.get("summary"),
            context_summary=args.get("context_summary"),
            content=args.get("content"),
            details=args.get("details"),
            type=args.get("type"),
            importance=args.get("importance"),
            tags=args.get("tags"),
            context=args.get("context"),
            delivery_mode=args.get("delivery_mode"),  # Issue #886 (pin/unpin)
            # #1504: rejection path for a supersede suggestion.
            dismiss_supersede_candidate=bool(args.get("dismiss_supersede_candidate", False)),
        )
    except ValidationError as e:
        # #1323: plain field/constraint summary — no pydantic internals.
        return _error_response("validation_error", _format_validation_error(e))
    except ValueError as e:
        return _error_response("validation_error", str(e))

    start_time = time.time()
    async for db in get_db():
        try:
            current_context_id = _resolve_context_id(args["context_id"])

            perm_error = await _check_viewer_permission(
                db, user_id, workspace_id, "update memories"
            )
            if perm_error:
                return perm_error

            current_context = await _resolve_context(
                db, user_id, current_context_id, operation="update"
            )

            service = MemoryService(db)
            result = await execute_with_timeout(
                service.update_memory(
                    request,
                    user_id=user_id,
                    client="mcp",
                    current_context_id=current_context_id,
                    current_workspace_id=workspace_id,
                ),
                operation_name="update_memory",
            )

            await _log_tool_usage(
                db, user_id, "update_memory", start_time, 200, current_context_id, workspace_id
            )
            # #1257 review: update_memory is a memory op too — a context kept
            # alive purely by external_id upserts must not sort as never-used.
            await _touch_context_last_used(db, current_context)
            await db.commit()

            return [
                TextContent(
                    type="text",
                    text=_dumps(
                        {
                            "status": "success",
                            "memory_id": str(result.memory_id),
                            "operation": result.operation,
                            "re_embedded": result.re_embedded,
                            "scope": result.scope,
                            **_persistence_response_field(result.persistence),  # #1505
                            # #1504: echo WHICH pairing was tombstoned, so the
                            # caller can confirm it rejected what it meant to.
                            **(
                                {
                                    "supersede_candidate_dismissed": str(
                                        result.supersede_candidate_dismissed
                                    )
                                }
                                if result.supersede_candidate_dismissed
                                else {}
                            ),
                            **_lint_response_field(result.lint),  # #1502
                            **_context_response_fields(current_context),
                        }
                    ),
                )
            ]
        except _ContextNotFoundError as e:
            await db.rollback()
            return e.to_response()
        except NotFoundException as e:
            # #1323: structured envelope for a missing/inaccessible memory,
            # mirroring handle_reference — previously this fell through to the
            # generic dispatch handler as a raw slug-less string.
            await db.rollback()
            await _log_tool_usage(
                db, user_id, "update_memory", start_time, 404, current_context_id, workspace_id
            )
            # The upsert path (external_id) can surface the service's
            # NotFoundException("Context", ...) from the isolation gate — the
            # same variant handle_recall re-casts. Keep the deny shape
            # byte-identical to a regular context deny (CWE-639 uniformity).
            if str(e).startswith("Context"):
                return _ContextNotFoundError(
                    current_context_id,
                    "Context not found or you don't have access to it.",
                ).to_response()
            target_id = args.get("memory_id") or args.get("external_id")
            return _error_response(
                "memory_not_found",
                f"Memory not found or you don't have access: {target_id}",
                help="Use recall() to find memories you have access to.",
            )
        except ValueError as e:
            # _apply_time_trigger raises ValueError for an invalid type="time"
            # details.trigger on the update path. Return a structured
            # validation_error rather than re-raising as an opaque tool crash
            # (mirrors handle_remember).
            await db.rollback()
            await _log_tool_usage(
                db, user_id, "update_memory", start_time, 422, args.get("context_id"), workspace_id
            )
            return _error_response("validation_error", str(e))
        except AuthorizationError:
            # Tool guardrails: editing a guardrail row, or adding / removing
            # details.tool_trigger, needs context EDITOR or above.
            await db.rollback()
            await _log_tool_usage(
                db, user_id, "update_memory", start_time, 403, args.get("context_id"), workspace_id
            )
            return _guardrail_author_denied("change a tool guardrail")
        except Exception:
            await db.rollback()
            await _log_tool_usage(
                db,
                user_id,
                "update_memory",
                start_time,
                500,
                args.get("context_id"),
                workspace_id,
            )
            raise

    # Safety: should never reach here (get_db always yields)
    return _error_response("internal_error", "Database session unavailable")


async def handle_recall_upcoming(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Pull Time Memories (#877) whose trigger window overlaps a time range.

    Deterministic filter+sort over type='time' memories — NOT semantic recall,
    so it has no Hebbian write side-effects (the recall()-vs-list design rule).
    Soonest-first by trigger_from. The trigger_from/trigger_until columns are
    TEXT fixed-width ISO, so string comparison == chronological comparison.
    """
    if "context_id" not in args:
        return _error_response("missing_fields", "Missing required field: context_id")

    from db.base import get_db
    from services.time_memory import clamp_upcoming_k, query_upcoming_time_memories
    from utils.time_trigger import TriggerValidationError, parse_query_bound

    # Validate inputs up front (before opening a DB session) so a malformed
    # argument is a structured validation_error, not an unhandled crash.
    # #1276: the coerce+clamp is hoisted into the shared time_memory helper so
    # the tool and get_agent_bootstrap share one implementation.
    try:
        k = clamp_upcoming_k(args.get("k", 20))
    except (TypeError, ValueError):
        return _error_response("validation_error", f"k must be an integer, got {args.get('k')!r}")

    try:
        # Re-normalize bounds to fixed-width ISO (and resolve 'now') so the
        # lexical comparison against the TEXT columns is correct. None => no
        # bound on that side.
        q_from = parse_query_bound(args.get("from"))
        q_until = parse_query_bound(args.get("until"))
    except TriggerValidationError as e:
        return _error_response("validation_error", str(e))

    # #1599: items carry `trigger` by default; the full `details` is opt-in.
    # Strictly True (string booleans are coerced at dispatch) so anything
    # unrecognized falls back to the lean shape.
    include_details = args.get("include_details") is True

    start_time = time.time()
    async for db in get_db():
        current_context_id: UUID | None = None
        try:
            current_context_id = _resolve_context_id(args["context_id"])
            # Read path: uniform context_not_found on any deny (CWE-639 / OWASP
            # A01), mirroring handle_recall.
            current_context = await _resolve_context_for_read(db, user_id, current_context_id)

            results = await query_upcoming_time_memories(
                db,
                current_context_id,
                q_from=q_from,
                q_until=q_until,
                k=k,
                include_details=include_details,
            )
            await _log_tool_usage(
                db, user_id, "recall_upcoming", start_time, 200, current_context_id, workspace_id
            )
            return [
                TextContent(
                    type="text",
                    text=_dumps(
                        {
                            "status": "success",
                            "results": results,
                            **_context_response_fields(current_context),
                        }
                    ),
                )
            ]
        except _ContextNotFoundError as e:
            await _log_tool_usage(
                db, user_id, "recall_upcoming", start_time, 404, current_context_id, workspace_id
            )
            return e.to_response()

    return _error_response("internal_error", "Database session unavailable")


async def handle_recall_nearby(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """List memories near a point (#1331) — recall_upcoming's spatial twin.

    Deterministic filter+sort over the generated location_lat/location_lon
    columns — NOT semantic recall, so no Hebbian write side-effects. Nearest
    first with distance_m.

    Privacy invariant (spec §7-6): the query point is precise user location —
    neither this handler nor the geo_memory service logs lat/lon, and the
    validation errors below never echo the coordinate values.
    """
    missing = [field for field in ("context_id", "lat", "lon") if field not in args]
    if missing:
        return _error_response("missing_fields", f"Missing required field(s): {', '.join(missing)}")

    from db.base import get_db
    from services.geo_memory import query_nearby_memories
    from utils.geo_location import (
        LocationValidationError,
        clamp_nearby_k,
        clamp_radius_m,
        validate_query_coords,
    )

    # Validate inputs up front (before opening a DB session) so a malformed
    # argument is a structured validation_error — the recall_upcoming pattern.
    # Query coordinates are held to the write-side standard (bool/string
    # numerics rejected; MCP arg coercion does not recurse into numbers here).
    try:
        lat, lon = validate_query_coords(args["lat"], args["lon"])
    except LocationValidationError as e:
        return _error_response("validation_error", str(e))
    try:
        radius_m = clamp_radius_m(args.get("radius_m"))
    except (LocationValidationError, TypeError, ValueError):
        return _error_response("validation_error", "radius_m must be a finite number")
    try:
        k = clamp_nearby_k(args.get("k", 20))
    except (TypeError, ValueError):
        return _error_response("validation_error", f"k must be an integer, got {args.get('k')!r}")

    start_time = time.time()
    async for db in get_db():
        current_context_id: UUID | None = None
        try:
            current_context_id = _resolve_context_id(args["context_id"])
            # Read path: uniform context_not_found on any deny (CWE-639 / OWASP
            # A01), mirroring handle_recall. Cross-context nearby deliberately
            # does not exist — it would be a location-disclosure oracle across
            # private contexts.
            current_context = await _resolve_context_for_read(db, user_id, current_context_id)

            results = await query_nearby_memories(
                db, current_context_id, lat=lat, lon=lon, radius_m=radius_m, k=k
            )
            await _log_tool_usage(
                db, user_id, "recall_nearby", start_time, 200, current_context_id, workspace_id
            )
            return [
                TextContent(
                    type="text",
                    text=_dumps(
                        {
                            "status": "success",
                            "results": results,
                            **_context_response_fields(current_context),
                        }
                    ),
                )
            ]
        except _ContextNotFoundError as e:
            await _log_tool_usage(
                db, user_id, "recall_nearby", start_time, 404, current_context_id, workspace_id
            )
            return e.to_response()

    return _error_response("internal_error", "Database session unavailable")


async def handle_load_pinned(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Deterministically load a context's always-delivery memories (#886).

    The deterministic counterpart to recall(): returns the complete, unranked,
    bounded delivery_mode='always' set for the context — no embeddings, no
    Hebbian write side-effects (the recall()-vs-list design rule). Items are
    L1+L2 only; fetch full content via reference(). On cap-exceeded the response
    carries truncated=true + total_available (never a silent truncation).
    """
    if "context_id" not in args:
        return _error_response("missing_fields", "Missing required field: context_id")

    from db.base import get_db
    from services.memory_service import MemoryService

    cap = args.get("cap")
    start_time = time.time()
    async for db in get_db():
        current_context_id: UUID | None = None
        try:
            current_context_id = _resolve_context_id(args["context_id"])
            # Read path: uniform context_not_found on any deny (CWE-639 / OWASP
            # A01), mirroring handle_recall / handle_recall_upcoming.
            current_context = await _resolve_context_for_read(
                db, user_id, current_context_id, operation="load_pinned"
            )

            service = MemoryService(db)
            result = await execute_with_timeout(
                service.load_pinned(
                    user_id=user_id,
                    current_context_id=current_context_id,
                    current_workspace_id=workspace_id,
                    cap=cap,
                ),
                operation_name="load_pinned",
            )

            await _log_tool_usage(
                db, user_id, "load_pinned", start_time, 200, current_context_id, workspace_id
            )
            return [
                TextContent(
                    type="text",
                    text=_dumps(
                        {
                            "status": "success",
                            "memories": [
                                {
                                    "memory_id": str(m.memory_id),
                                    "summary": m.summary,
                                    "context_summary": m.context_summary,
                                    "type": m.type,
                                    "importance": m.importance,
                                    "delivery_mode": m.delivery_mode,
                                }
                                for m in result.memories
                            ],
                            "total_available": result.total_available,
                            "truncated": result.truncated,
                            "cap": result.cap,
                            **_context_response_fields(current_context),
                        }
                    ),
                )
            ]
        except _ContextNotFoundError as e:
            await _log_tool_usage(
                db, user_id, "load_pinned", start_time, 404, current_context_id, workspace_id
            )
            return e.to_response()
        except ValueError as e:
            await _log_tool_usage(
                db, user_id, "load_pinned", start_time, 422, current_context_id, workspace_id
            )
            return _error_response("validation_error", str(e))

    return _error_response("internal_error", "Database session unavailable")


def _guardrail_item_payload(item: Any) -> dict[str, Any]:
    """Project one ``GuardrailItem`` onto the MCP envelope (timestamps as UTC ``Z``)."""
    return {
        "memory_id": str(item.memory_id),
        "summary": item.summary,
        "context_summary": item.context_summary,
        "type": item.type,
        "importance": item.importance,
        "delivery_mode": item.delivery_mode,
        "tool_trigger": item.tool_trigger,
        "source_type": item.source_type,
        "authored_by_caller": item.authored_by_caller,
        "created_at": to_utc_iso(item.created_at),
        "updated_at": to_utc_iso(item.updated_at),
    }


async def handle_load_guardrails(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Deterministically load a context's guardrail set for a client-side hook.

    ``handle_load_pinned``'s twin: pinned memories (``delivery_mode='always'``)
    plus memories marked with ``details.tool_trigger``, trusted tier only, each
    lane ordered ``importance DESC, created_at ASC, id ASC`` and capped on its
    own. No embeddings, no Hebbian write; the response carries the shared
    payload ``format`` and the served-set ``version`` (docs/mcp-tools.md §
    Tool guardrails). The stored patterns are returned as data — never
    compiled or run here.
    """
    if "context_id" not in args:
        return _error_response("missing_fields", "Missing required field: context_id")

    from db.base import get_db
    from services.memory_service import MemoryService

    cap = args.get("cap")
    start_time = time.time()
    async for db in get_db():
        current_context_id: UUID | None = None
        try:
            current_context_id = _resolve_context_id(args["context_id"])
            # Read path: uniform context_not_found on any deny (CWE-639),
            # mirroring handle_load_pinned.
            current_context = await _resolve_context_for_read(
                db, user_id, current_context_id, operation="load_guardrails"
            )

            service = MemoryService(db)
            result = await execute_with_timeout(
                service.load_guardrails(
                    user_id=user_id,
                    current_context_id=current_context_id,
                    current_workspace_id=workspace_id,
                    cap=cap,
                ),
                operation_name="load_guardrails",
            )

            await _log_tool_usage(
                db, user_id, "load_guardrails", start_time, 200, current_context_id, workspace_id
            )
            return [
                TextContent(
                    type="text",
                    text=_dumps(
                        {
                            "status": "success",
                            "format": result.format,
                            "version": result.version,
                            "pinned": [_guardrail_item_payload(i) for i in result.pinned],
                            "tool_triggered": [
                                _guardrail_item_payload(i) for i in result.tool_triggered
                            ],
                            "total_available": result.total_available,
                            "truncated": result.truncated,
                            "cap": result.cap,
                            "pinned_cap": result.pinned_cap,
                            "pinned_total_available": result.pinned_total_available,
                            "pinned_truncated": result.pinned_truncated,
                            "tool_triggered_total_available": (
                                result.tool_triggered_total_available
                            ),
                            "tool_triggered_truncated": result.tool_triggered_truncated,
                            **_context_response_fields(current_context),
                        }
                    ),
                )
            ]
        except _ContextNotFoundError as e:
            await _log_tool_usage(
                db, user_id, "load_guardrails", start_time, 404, current_context_id, workspace_id
            )
            return e.to_response()
        except ValueError as e:
            await _log_tool_usage(
                db, user_id, "load_guardrails", start_time, 422, current_context_id, workspace_id
            )
            return _error_response("validation_error", str(e))

    return _error_response("internal_error", "Database session unavailable")


def _recall_result_item(r: Any) -> dict[str, Any]:
    """Project one recall result onto the MCP envelope.

    #1599: the annotations that are empty on almost every result are omitted
    rather than serialized as ``null`` / ``[]`` — absence is the signal, as for
    ``persistence`` / ``lint`` on the write tools. A present value is rendered
    exactly as before.

    Args:
        r: A ``MemoryResponse`` from ``RecallResponse.results``.

    Returns:
        The result item, Layers 1-2 only.
    """
    item: dict[str, Any] = {"memory_id": str(r.memory_id), "summary": r.summary}
    # Truthiness, like the annotations below: a stored "" (remember() has no
    # min_length) says as little as None, so it is absent too.
    if r.context_summary:
        item["context_summary"] = r.context_summary
    item.update(
        {
            "type": r.type,
            "importance": r.importance,
            "scope": r.scope,
            # 4 decimals keep the ranking readable; the full float is 16+
            # digits of noise per result.
            "score": round(r.score, 4) if r.score is not None else None,
            "tags": r.tags,
            # Issue #1047: recency/staleness cues for the agent. created_at
            # is the always-present floor; updated_at is the last real change
            # (null if never edited) — an old value means the fact may be stale.
            "created_at": to_utc_iso(r.created_at),
            "updated_at": to_utc_iso(r.updated_at),
        }
    )
    # #1208: fact-succession annotations. superseded_by is only set under
    # include_superseded=true; contradicts lists opposing memories (never
    # hidden, both sides annotated).
    if r.superseded_by:
        item["superseded_by"] = str(r.superseded_by)
    if r.contradicts:
        item["contradicts"] = [str(c) for c in r.contradicts]
    # #1403: liveness-guarded near-duplicate this memory may supersede — a
    # client can offer confirm→create_edge.
    if r.supersede_candidate:
        item["supersede_candidate"] = r.supersede_candidate.model_dump(mode="json")
    return item


def _recall_envelope(result: Any, context: Any) -> dict[str, Any]:
    """Build the recall MCP envelope from a ``RecallResponse``.

    Kept out of the handler so the response-size guard
    (``tests/mcp_server/test_recall_envelope.py``) measures the text clients
    actually receive rather than a copy of this projection.

    Args:
        result: The ``RecallResponse`` from ``MemoryService.recall``.
        context: The resolved primary context.

    Returns:
        The envelope ``handle_recall`` serializes.
    """
    results_data = [_recall_result_item(r) for r in result.results]
    response_data: dict[str, Any] = {
        "status": "success",
        "results": results_data,
        "count": len(results_data),
        # #1599: tag + count only. ``RelatedTagItem.sample_summary`` (still
        # served over REST) repeats, in full, a summary that is already in
        # ``results`` — up to 10 times per response on this surface.
        "related_tags": [{"tag": tag.tag, "count": tag.count} for tag in result.related_tags],
        **_context_response_fields(context),
    }

    if result.explore_hints is not None:
        response_data["explore_hints"] = [
            {"memory_id": str(h.memory_id), "reason": h.reason} for h in result.explore_hints
        ]

    # Issue #1047: top-level relevance confidence (level=none → the agent
    # can stop probing early / go external instead of hallucinating).
    if result.confidence is not None:
        response_data["confidence"] = result.confidence.model_dump()

    # #1503: only present on an empty tag-filtered recall — it tells the
    # agent whether the topic is absent or just spelled differently.
    if result.tag_suggestions:
        response_data["tag_suggestions"] = result.tag_suggestions

    # #1515: this envelope is hand-built, so a new RecallResponse field
    # does NOT reach MCP clients on its own — it has to be copied.
    # The agent needs it: served without the semantic arm, ``confidence``
    # rests on a different basis, so a low level here means "the search
    # was impaired", not "nothing relevant is stored".
    response_data.update(_degraded_response_fields(result))
    return response_data


async def handle_recall(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Search memories with configurable mode (hybrid/semantic/keyword)."""
    if "query" not in args:
        return _error_response("missing_fields", "Missing required field: query")

    # Issue #81: Require either context_id or context_ids
    if "context_id" not in args and "context_ids" not in args:
        return _error_response(
            "missing_fields",
            "Missing required field: context_id or context_ids. Use list_contexts() to find available IDs.",
        )

    from db.base import get_db
    from models.schemas import RecallRequest
    from services.memory_service import MemoryService

    request = RecallRequest(
        query=args["query"],
        k=args.get("k", 5),
        # #1572: None when omitted → SearchService follows the context's config.
        use_rerank=args.get("use_rerank"),
        filters=args.get("filters"),
        # #1212: pass None through when the caller omitted search_mode so the
        # query router can act on contexts with routing_mode='active';
        # MemoryService resolves None to "hybrid" everywhere else.
        search_mode=args.get("search_mode"),
        include_explore_hints=args.get("include_explore_hints", False),
        include_superseded=args.get("include_superseded", False),  # #1208
    )

    start_time = time.time()
    async for db in get_db():
        # Bind defensively so the NotFoundException handler below can
        # always reference a concrete context_id. In normal flow this is
        # re-bound at lines below before any path that raises.
        current_context_id: UUID | None = None
        try:
            # Issue #81: Cross-context recall — context_ids overrides context_id
            context_ids_arg = args.get("context_ids")
            cross_context_ids: list[UUID] | None = None
            # #1257: every context read by this call gets its last_used_at
            # touched — but only at commit time on the success path (see the
            # helper's contract), so collect the resolved rows here.
            secondary_contexts: list[Any] = []

            if isinstance(context_ids_arg, list) and context_ids_arg:
                # #1228 review: enforce the inputSchema's maxItems server-side
                # (the schema is advisory for non-validating clients) — a
                # single billable call must not fan out into unbounded
                # permission resolves and attribution rows.
                if len(context_ids_arg) > MAX_CROSS_CONTEXT_IDS:
                    return _error_response(
                        "too_many_context_ids",
                        f"context_ids accepts at most {MAX_CROSS_CONTEXT_IDS} entries.",
                    )
                # Multi-context mode. Use _resolve_context_for_read so deny
                # reasons (private non-creator, not a workspace member, etc.)
                # surface as a uniform context_not_found (CWE-639 / OWASP A01).
                # Order-preserving dedup (#1228 review): duplicated ids would
                # each burn a permission resolve and write an attribution row
                # (including one for the primary itself), inflating the
                # per-context read counts the attribution table exists to fix.
                cross_context_ids = list(
                    dict.fromkeys(_resolve_context_id(cid) for cid in context_ids_arg)
                )
                current_context_id = cross_context_ids[0]
                current_context = await _resolve_context_for_read(
                    db, user_id, current_context_id, operation="recall"
                )
                # #708 H3: all contexts must belong to the same workspace —
                # Option A paid_by routing has a single source-of-truth
                # workspace. Check inline so a mismatch short-circuits
                # before paying further permission lookups.
                #
                # #708 loop 6 (Copilot): also reject MIXED privacy across
                # the list. ``SearchService`` derives a single
                # ``is_shared_context`` value from the primary and applies
                # it to every context's Qdrant filter. If primary is
                # shared and a secondary is private (which the handler
                # permits when the caller is that private context's
                # creator / ContextMember), dropping the ``user_id`` filter
                # would leak memories authored by other users in the
                # private secondary. Rejecting at API boundary mirrors
                # the same-embedding-model invariant pattern below.
                for cid in cross_context_ids[1:]:
                    cross_ctx = await _resolve_context_for_read(
                        db, user_id, cid, operation="recall"
                    )
                    if cross_ctx.workspace_id != current_context.workspace_id:
                        return _error_response(
                            "workspace_mismatch",
                            "All contexts in cross-context recall must belong to "
                            "the same workspace.",
                        )
                    if cross_ctx.is_private != current_context.is_private:
                        return _error_response(
                            "context_privacy_mismatch",
                            "All contexts in cross-context recall must share the "
                            "same privacy setting (all shared or all private).",
                        )
                    secondary_contexts.append(cross_ctx)

                # Validate all contexts use the same embedding model
                from repositories.config_repository import ContextSearchConfigRepository

                config_repo = ContextSearchConfigRepository(db)
                primary_config = await config_repo.create_or_get(current_context_id)
                for cid in cross_context_ids[1:]:
                    cid_config = await config_repo.create_or_get(cid)
                    if cid_config.embedding_model != primary_config.embedding_model:
                        return _error_response(
                            "embedding_model_mismatch",
                            f"All contexts must use the same embedding model. "
                            f"Context {cid} uses '{cid_config.embedding_model}' "
                            f"but primary uses '{primary_config.embedding_model}'.",
                        )
            else:
                # Single context mode (backward compatible)
                if "context_id" not in args:
                    return _error_response(
                        "missing_fields",
                        "Missing required field: context_id (or provide context_ids with 2+ UUIDs).",
                    )
                current_context_id = _resolve_context_id(args["context_id"])
                current_context = await _resolve_context_for_read(
                    db, user_id, current_context_id, operation="recall"
                )

            service = MemoryService(db)
            result = await execute_with_timeout(
                service.recall(
                    request,
                    user_id=user_id,
                    current_context_id=current_context_id,
                    current_workspace_id=workspace_id,
                    # #708 Option A: source workspace pays for shared-context reads.
                    context_workspace_id=current_context.workspace_id,
                    context_ids=cross_context_ids,
                ),
                operation_name="recall",
            )

            # Built before the usage row / commit, as the item projection always
            # was: a result that cannot be rendered fails the call as a whole.
            response_data = _recall_envelope(result, current_context)

            await _log_tool_usage(
                db,
                user_id,
                "recall",
                start_time,
                200,
                current_context_id,
                workspace_id,
                # #1228: the primary context carries the billable UsageStats
                # row; the other listed contexts get diagnostic attribution
                # rows so per-context read visibility sees them.
                attributed_context_ids=(cross_context_ids[1:] if cross_context_ids else None),
            )
            # #1257: mark every recalled context as used. Ascending-id order
            # keeps concurrent overlapping cross-context recalls deadlock-free
            # (see the helper's contract).
            for touch_ctx in sorted(
                [current_context, *secondary_contexts], key=lambda c: str(c.id)
            ):
                await _touch_context_last_used(db, touch_ctx)
            await db.commit()

            return [
                TextContent(
                    type="text",
                    text=_dumps(response_data),
                )
            ]
        except _ContextNotFoundError as e:
            await db.rollback()
            return e.to_response()
        except NotFoundException:
            # #708 H2: service raises NotFoundException("Context", ...) for
            # shared-context reads that cannot proceed (e.g. source has no
            # BYOK). Re-cast through _ContextNotFoundError so the response
            # body is byte-identical to a regular deny — callers cannot
            # distinguish "source missing BYOK" from "context does not
            # exist for you" (CWE-639 / OWASP A01).
            await db.rollback()
            await _log_tool_usage(
                db, user_id, "recall", start_time, 404, current_context_id, workspace_id
            )
            if current_context_id is None:
                # Unreachable in practice — current_context_id is bound
                # before any path that can raise NotFoundException — but
                # static analysis cannot prove the invariant.
                return _error_response(
                    "context_not_found",
                    "Context not found or you don't have access to it.",
                    context_id=args.get("context_id"),
                    help="Use list_contexts() to see contexts you have access to.",
                )
            return _ContextNotFoundError(
                current_context_id,
                "Context not found or you don't have access to it.",
            ).to_response()
        except ValueError as e:
            # Malformed free-form ``filters`` input — importance range,
            # score_threshold (#1229), near (#1332; LocationValidationError
            # subclasses ValueError) — is routine client input: return the
            # structured validation_error envelope (the REST route's 422
            # mirror), not an opaque 500-shaped tool crash.
            await db.rollback()
            await _log_tool_usage(
                db, user_id, "recall", start_time, 422, current_context_id, workspace_id
            )
            return _error_response("validation_error", str(e))
        except Exception:
            await db.rollback()
            await _log_tool_usage(
                db, user_id, "recall", start_time, 500, args.get("context_id"), workspace_id
            )
            raise

    # Safety: should never reach here (get_db always yields)
    return _error_response("internal_error", "Database session unavailable")


async def handle_forget(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Delete memories (soft delete)."""
    from db.base import get_db
    from models.schemas import ForgetRequest
    from services.memory_service import MemoryService

    memory_id = args.get("memory_id")
    request = ForgetRequest(
        memory_id=UUID(memory_id) if memory_id else None,
        query=args.get("query"),
        k=args.get("k", 10),
    )

    start_time = time.time()
    async for db in get_db():
        try:
            current_context_id = _resolve_context_id(args["context_id"])

            perm_error = await _check_viewer_permission(
                db, user_id, workspace_id, "delete memories"
            )
            if perm_error:
                return perm_error

            current_context = await _resolve_context(
                db, user_id, current_context_id, operation="forget"
            )

            service = MemoryService(db)
            result = await execute_with_timeout(
                service.forget(
                    request,
                    user_id=user_id,
                    current_context_id=current_context_id,
                ),
                operation_name="forget",
            )

            await _log_tool_usage(
                db, user_id, "forget", start_time, 200, current_context_id, workspace_id
            )
            # #1257 review: forget is a memory op too — deletion-only
            # maintenance still counts as using the context.
            await _touch_context_last_used(db, current_context)
            await db.commit()

            return [
                TextContent(
                    type="text",
                    text=_dumps(
                        {
                            "status": "success",
                            "deleted_count": result.deleted_count,
                            "memory_ids": [str(mid) for mid in result.memory_ids],
                            "context_id": str(current_context.id) if current_context else None,
                            "context_name": current_context.name if current_context else None,
                        }
                    ),
                )
            ]
        except _ContextNotFoundError as e:
            await db.rollback()
            return e.to_response()
        except Exception:
            await db.rollback()
            await _log_tool_usage(
                db, user_id, "forget", start_time, 500, args.get("context_id"), workspace_id
            )
            raise

    # Safety: should never reach here (get_db always yields)
    return _error_response("internal_error", "Database session unavailable")


# ============================================================================
# #1685: bounded reference() responses
# ============================================================================
#
# reference() returns one memory in full. Its "light" fields (ids, summary,
# context_summary, tags, timestamps, provenance, supersede_candidate) are
# bounded by the write-side schema and always come back. The heavy fields —
# content, details, context and the declared links — are selectable (`fields`)
# and share a per-call budget (`max_chars`, in characters of the serialized
# tool result). A field that does not fit is never cut silently:
#
# * content (plain text) comes back as a slice with content_offset /
#   content_total_chars / content_truncated / content_next_offset;
# * details, context and links (JSON) come back whole or are left out with
#   <field>_omitted + <field>_total_chars — never sliced mid-structure.
#   details and context can be paged as their compact JSON text
#   (<field>_offset -> <field>_json), so a caller can always rebuild 100% of
#   either; links are capped by the service (50 per direction), so
#   fields=["links"] with a raised max_chars returns them whole.
#
# The whole-or-omitted fields are placed first; the page the caller asked for,
# then content from the start, fill what is left. Offsets count Python str characters (code
# points). Every page goes through this same handler, so context resolution and
# the memory-level access check run again on each continuation.

_REFERENCE_FIELD_KEYS: dict[str, tuple[str, ...]] = {
    "content": ("content",),
    "details": ("details",),
    "context": ("context",),
    "links": ("outgoing_links", "outgoing_has_more", "incoming_links", "incoming_has_more"),
}
_REFERENCE_FIELDS = tuple(_REFERENCE_FIELD_KEYS)
_REFERENCE_KEY_FIELD = {key: field for field, keys in _REFERENCE_FIELD_KEYS.items() for key in keys}
# Fields a caller can page with ``<field>_offset``.
_REFERENCE_PAGEABLE = ("content", "details", "context")
# Budget order of the whole-or-omitted fields: smallest-in-practice first, so
# one oversized field does not push out the ones that would have fitted.
_REFERENCE_WHOLE_ORDER = ("links", "context", "details")


class _ReferenceView:
    """The validated selection / paging / budget arguments of one reference() call."""

    __slots__ = ("fields", "page_field", "offset", "max_chars")

    def __init__(
        self, fields: frozenset[str], page_field: str | None, offset: int, max_chars: int
    ) -> None:
        self.fields = fields
        self.page_field = page_field
        self.offset = offset
        self.max_chars = max_chars

    @property
    def is_default(self) -> bool:
        return self.page_field is None and self.fields == frozenset(_REFERENCE_FIELDS)


class _ReferenceOffsetError(Exception):
    """An offset past the end of the field it pages (known only after the read)."""

    def __init__(self, field: str, offset: int, total: int) -> None:
        self.field = field
        self.offset = offset
        self.total = total
        super().__init__(field)

    def to_response(self) -> list[TextContent]:
        return _error_response(
            "invalid_argument",
            f"{self.field}_offset {self.offset} is beyond the end of {self.field} "
            f"({self.total} characters).",
            received=self.offset,
            help=f"Use {self.field}_next_offset from the previous response, or 0 to start over.",
            **{f"{self.field}_total_chars": self.total},
        )


def _parse_reference_view(
    args: dict[str, Any],
) -> tuple[_ReferenceView | None, list[TextContent] | None]:
    """Validate ``fields`` / ``*_offset`` / ``max_chars`` before any read."""
    from mcp_server.tools._constants import (
        REFERENCE_DEFAULT_MAX_CHARS,
        REFERENCE_MAX_CHARS_LIMIT,
        REFERENCE_MIN_MAX_CHARS,
    )

    # ``type(x) is int`` (not isinstance) so bool, which subclasses int, is refused.
    offsets: dict[str, int] = {}
    for field in _REFERENCE_PAGEABLE:
        name = f"{field}_offset"
        value = args.get(name)
        if value is None:
            continue
        if type(value) is not int or value < 0:
            return None, _error_response(
                "invalid_argument",
                f"{name} must be a non-negative integer (a character offset).",
                received=value,
                help=f"Pass the {name.replace('_offset', '_next_offset')} of the previous response.",
            )
        offsets[field] = value
    if len(offsets) > 1:
        return None, _error_response(
            "invalid_argument",
            "Pass only one of content_offset, details_offset, context_offset per call.",
            received=sorted(f"{field}_offset" for field in offsets),
        )
    page_field, offset = next(iter(offsets.items()), (None, 0))

    raw_fields = args.get("fields")
    if raw_fields is None:
        # An offset alone asks for the next page of that one field.
        fields = frozenset([page_field] if page_field else _REFERENCE_FIELDS)
    elif type(raw_fields) is not list or any(f not in _REFERENCE_FIELDS for f in raw_fields):
        return None, _error_response(
            "invalid_argument",
            f"fields must be an array of: {', '.join(_REFERENCE_FIELDS)}.",
            received=raw_fields,
        )
    else:
        fields = frozenset(raw_fields)
        if page_field and page_field not in fields:
            return None, _error_response(
                "invalid_argument",
                f"{page_field}_offset needs '{page_field}' in fields.",
                received=raw_fields,
            )

    max_chars = args.get("max_chars")
    if max_chars is None:
        max_chars = REFERENCE_DEFAULT_MAX_CHARS
    elif type(max_chars) is not int or not (
        REFERENCE_MIN_MAX_CHARS <= max_chars <= REFERENCE_MAX_CHARS_LIMIT
    ):
        return None, _error_response(
            "invalid_argument",
            f"max_chars must be an integer between {REFERENCE_MIN_MAX_CHARS} and "
            f"{REFERENCE_MAX_CHARS_LIMIT} (characters, not tokens).",
            received=max_chars,
        )
    return _ReferenceView(fields, page_field, offset, max_chars), None


def _member_chars(key: str, value: Any) -> int:
    """Characters that ``,"key":value`` adds to a compact JSON object."""
    return len(_dumps(key)) + len(_dumps(value)) + 2


def _fit_slice(text: str, start: int, budget: int) -> int:
    """Longest ``n`` whose ``text[start:start + n]`` serializes within ``budget``.

    Measured on the escaped JSON string (quotes excluded): a quote, backslash or
    newline costs two characters and a control character six, so the slice
    length cannot be read off the budget. Escaped length only grows with ``n``,
    which makes a binary search exact; every probe is at most ``budget`` long.
    """
    lo, hi = 0, min(len(text) - start, max(budget, 0))
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len(_dumps(text[start : start + mid])) - 2 <= budget:
            lo = mid
        else:
            hi = mid - 1
    return lo


def _omitted_marker(field: str, total: int, next_offset: int | None) -> dict[str, Any]:
    """The markers that stand in for a field left out of the response."""
    marker: dict[str, Any] = {f"{field}_omitted": True, f"{field}_total_chars": total}
    if next_offset is not None:
        marker[f"{field}_next_offset"] = next_offset
    return marker


def _reference_page(
    field: str, text: str, offset: int, remaining: int, *, progress: bool
) -> dict[str, Any]:
    """One page of ``text`` from ``offset`` that fits ``remaining`` characters.

    ``text`` is the content itself, or the compact JSON of details / context
    (returned under ``<field>_json``). With ``progress`` (the caller is paging
    this field) a page always advances by at least one character, so a
    continuation loop terminates even when the light fields crowd the budget.
    Without it (content read from the start), a page that fits nothing is
    reported as omitted instead of as an empty slice.
    """
    total = len(text)
    if offset > total:
        raise _ReferenceOffsetError(field, offset, total)
    value_key = field if field == "content" else f"{field}_json"

    def meta(next_offset: int | None, truncated: bool) -> dict[str, Any]:
        return {
            f"{field}_offset": offset,
            f"{field}_total_chars": total,
            f"{field}_truncated": truncated,
            f"{field}_next_offset": next_offset,
        }

    # Reserve room for the metadata at its widest (``false`` > ``true``; the
    # next offset is either ``null`` or at most ``total``).
    reserve = max(
        sum(_member_chars(k, v) for k, v in meta(nxt, False).items()) for nxt in (None, total)
    )
    budget = remaining - reserve - _member_chars(value_key, "")
    size = _fit_slice(text, offset, budget)
    if size == 0 and offset < total:
        if not progress:
            return _omitted_marker(field, total, offset)
        size = 1
    end = offset + size
    return {
        value_key: text[offset:end],
        **meta(end if end < total else None, end < total),
    }


def _bound_reference_memory(memory: dict[str, Any], view: _ReferenceView) -> dict[str, Any]:
    """Apply field selection, paging and the character budget to ``memory``.

    ``memory`` is the full projection in the historical key order. The result
    keeps that order, with each field's markers next to it; the serialized
    ``{"status": "success", "memory": ...}`` stays within ``view.max_chars``
    unless the always-returned light fields alone nearly fill it.
    """
    head = {k: v for k, v in memory.items() if k not in _REFERENCE_KEY_FIELD}
    # ``head`` always holds memory_id, so every added member costs its comma.
    remaining = view.max_chars - len(_dumps({"status": "success", "memory": head}))
    parts: dict[str, dict[str, Any]] = {}

    def take(field: str, part: dict[str, Any]) -> None:
        nonlocal remaining
        parts[field] = part
        remaining -= sum(_member_chars(k, v) for k, v in part.items())

    # 1. JSON fields: whole, or left out with their size and where paging
    #    starts. The paged field (if any) is handled below instead.
    for field in _REFERENCE_WHOLE_ORDER:
        if field not in view.fields or field == view.page_field:
            continue
        whole = {key: memory[key] for key in _REFERENCE_FIELD_KEYS[field]}
        if sum(_member_chars(k, v) for k, v in whole.items()) <= remaining:
            take(field, whole)
            continue
        if field == "links":
            take(field, _omitted_marker(field, len(_dumps(whole)), None))
        else:
            take(field, _omitted_marker(field, len(_dumps(memory[field])), 0))

    # 2. The requested page, then content from the start, fill what is left.
    content_after_page = "content" in view.fields and view.page_field != "content"
    if view.page_field:
        field = view.page_field
        text = memory[field] if field == "content" else _dumps(memory[field])
        # Hold back room for content's markers, which are reported after the page.
        held = (
            sum(
                _member_chars(k, v)
                for k, v in _omitted_marker("content", len(memory["content"]), 0).items()
            )
            if content_after_page
            else 0
        )
        take(field, _reference_page(field, text, view.offset, remaining - held, progress=True))
    if content_after_page:
        content = memory["content"]
        if _member_chars("content", content) <= remaining:
            take("content", {"content": content})
        else:
            take("content", _reference_page("content", content, 0, remaining, progress=False))

    bounded: dict[str, Any] = {}
    for key, value in memory.items():
        field = _REFERENCE_KEY_FIELD.get(key)
        if field is None:
            bounded[key] = value
        elif key == _REFERENCE_FIELD_KEYS[field][0]:
            bounded.update(parts.get(field, {}))
    return bounded


async def handle_reference(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Get complete memory details (Layer 3), within a character budget (#1685)."""
    memory_uuid, error = _validate_memory_id(args, "reference")
    if error or memory_uuid is None:
        return error or _error_response("invalid_memory_id_format", "Invalid memory_id")

    view, error = _parse_reference_view(args)
    if error or view is None:
        return error or _error_response("invalid_argument", "Invalid reference arguments")

    from db.base import get_db
    from models.schemas import ReferenceRequest
    from services.memory_service import MemoryService

    request = ReferenceRequest(memory_id=memory_uuid)

    start_time = time.time()
    async for db in get_db():
        try:
            current_context_id = _resolve_context_id(args["context_id"])
            # Issue #708 bundle: migrate read paths to the CWE-639-uniform
            # resolver so deny reasons (private non-creator, not a workspace
            # member, etc.) cannot be distinguished by callers.
            current_context = await _resolve_context_for_read(
                db, user_id, current_context_id, operation="reference"
            )

            service = MemoryService(db)
            try:
                result = await execute_with_timeout(
                    service.reference(request.memory_id, user_id=user_id),
                    operation_name="reference",
                )
            except NotFoundException:
                # #1316 review: record the 404 in usage telemetry, keeping
                # reference and explore symmetric for the identical outcome.
                await db.rollback()
                await _log_tool_usage(
                    db, user_id, "reference", start_time, 404, current_context_id, workspace_id
                )
                return _error_response(
                    "memory_not_found",
                    f"Memory not found or you don't have access: {request.memory_id}",
                    help="Use recall() to find memories you have access to.",
                )

            # Surface the FULL ReferenceResponse the service already builds: the
            # handler previously dropped scope/updated_at (#434), source provenance
            # (#215), and the declared-link references (#440), so an agent could not
            # see a memory's links or staleness via reference() (#1054).
            reference_data = {
                "memory_id": str(result.memory_id),
                "summary": result.summary,
                "context_summary": result.context_summary,
                "content": result.content,
                "details": result.details,
                "type": result.type,
                "scope": result.scope,
                "importance": result.importance,
                "tags": result.tags,
                "context": result.context,
                "created_at": to_utc_iso(result.created_at),
                "updated_at": to_utc_iso(result.updated_at),
                "client": result.client,
                "source_uri": result.source_uri,
                "source_type": result.source_type,
                "outgoing_links": [ref.model_dump(mode="json") for ref in result.outgoing_links],
                "outgoing_has_more": result.outgoing_has_more,
                "incoming_links": [ref.model_dump(mode="json") for ref in result.incoming_links],
                "incoming_has_more": result.incoming_has_more,
                # #1403: liveness-guarded supersede suggestion (from the
                # server-only supersede_candidate column, not the details blob).
                "supersede_candidate": (
                    result.supersede_candidate.model_dump(mode="json")
                    if result.supersede_candidate
                    else None
                ),
            }

            # #1685: a default call whose full projection fits is returned
            # exactly as before; anything else is selected / paged / bounded.
            text = _dumps({"status": "success", "memory": reference_data})
            if not (view.is_default and len(text) <= view.max_chars):
                try:
                    bounded = _bound_reference_memory(reference_data, view)
                except _ReferenceOffsetError as e:
                    await db.rollback()
                    await _log_tool_usage(
                        db, user_id, "reference", start_time, 400, current_context_id, workspace_id
                    )
                    return e.to_response()
                text = _dumps({"status": "success", "memory": bounded})

            await _log_tool_usage(
                db, user_id, "reference", start_time, 200, current_context_id, workspace_id
            )
            # #1257: reference marks the context as used (guarded UPDATE at
            # commit time; the memory_not_found return above never touches).
            await _touch_context_last_used(db, current_context)
            await db.commit()

            return [TextContent(type="text", text=text)]
        except _ContextNotFoundError as e:
            await db.rollback()
            return e.to_response()
        except Exception:
            await db.rollback()
            await _log_tool_usage(
                db,
                user_id,
                "reference",
                start_time,
                500,
                args.get("context_id"),
                workspace_id,
            )
            raise

    # Safety: should never reach here (get_db always yields)
    return _error_response("internal_error", "Database session unavailable")
