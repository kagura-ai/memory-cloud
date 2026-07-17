"""MCP tool handlers: memory operations (remember, recall, forget, reference).

Extracted from tools.py for modularity (Issue #7).
"""

import json
import logging
import time
from typing import Any
from uuid import UUID

from mcp.types import TextContent

from mcp_server.tools._helpers import (
    _check_viewer_permission,
    _context_response_fields,
    _ContextNotFoundError,
    _error_response,
    _format_validation_error,
    _log_tool_usage,
    _resolve_context,
    _resolve_context_for_read,
    _resolve_context_id,
    _touch_context_last_used,
    _validate_memory_id,
    execute_with_timeout,
)
from utils.datetime import to_utc_iso
from utils.exceptions import NotFoundException

logger = logging.getLogger(__name__)

# #1228: server-side cap for cross-context recall — MUST stay in sync with
# the recall inputSchema's context_ids maxItems in _definitions.py.
MAX_CROSS_CONTEXT_IDS = 20


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
                    text=json.dumps(
                        {
                            "status": "success",
                            "memory_id": str(result.memory_id),
                            "scope": result.scope,
                            **_context_response_fields(current_context),
                        }
                    ),
                )
            ]
        except _ContextNotFoundError as e:
            await db.rollback()
            return e.to_response()
        except ValueError as e:
            # MemoryService raises ValueError as its "bad request" signal (e.g.
            # an invalid type="time" details.trigger). Return a structured
            # validation_error rather than re-raising as an opaque tool crash.
            await db.rollback()
            await _log_tool_usage(
                db, user_id, "remember", start_time, 422, args.get("context_id"), workspace_id
            )
            return _error_response("validation_error", str(e))
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
                    text=json.dumps(
                        {
                            "status": "success",
                            "memory_id": str(result.memory_id),
                            "operation": result.operation,
                            "re_embedded": result.re_embedded,
                            "scope": result.scope,
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

    start_time = time.time()
    async for db in get_db():
        current_context_id: UUID | None = None
        try:
            current_context_id = _resolve_context_id(args["context_id"])
            # Read path: uniform context_not_found on any deny (CWE-639 / OWASP
            # A01), mirroring handle_recall.
            current_context = await _resolve_context_for_read(db, user_id, current_context_id)

            results = await query_upcoming_time_memories(
                db, current_context_id, q_from=q_from, q_until=q_until, k=k
            )
            await _log_tool_usage(
                db, user_id, "recall_upcoming", start_time, 200, current_context_id, workspace_id
            )
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
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
                    text=json.dumps(
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
                    text=json.dumps(
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
            return _error_response("validation_error", str(e))

    return _error_response("internal_error", "Database session unavailable")


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
        use_rerank=args.get("use_rerank", False),
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

            results_data = [
                {
                    "memory_id": str(r.memory_id),
                    "summary": r.summary,
                    "context_summary": r.context_summary,
                    "type": r.type,
                    "importance": r.importance,
                    "scope": r.scope,
                    "score": r.score,
                    "tags": r.tags,
                    # Issue #1047: recency/staleness cues for the agent. created_at
                    # is the always-present floor; updated_at is the last real change
                    # (null if never edited) — an old value means the fact may be stale.
                    "created_at": to_utc_iso(r.created_at),
                    "updated_at": to_utc_iso(r.updated_at),
                    # #1208: fact-succession annotations. superseded_by is only
                    # non-null under include_superseded=true; contradicts lists
                    # opposing memories (never hidden, both sides annotated).
                    "superseded_by": str(r.superseded_by) if r.superseded_by else None,
                    "contradicts": [str(c) for c in r.contradicts],
                }
                for r in result.results
            ]

            related_tags_data = [
                {"tag": tag.tag, "count": tag.count, "sample_summary": tag.sample_summary}
                for tag in result.related_tags
            ]

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

            response_data: dict[str, Any] = {
                "status": "success",
                "results": results_data,
                "count": len(results_data),
                "related_tags": related_tags_data,
                **_context_response_fields(current_context),
            }

            if result.explore_hints is not None:
                response_data["explore_hints"] = [
                    {"memory_id": str(h.memory_id), "reason": h.reason}
                    for h in result.explore_hints
                ]

            # Issue #1047: top-level relevance confidence (level=none → the agent
            # can stop probing early / go external instead of hallucinating).
            if result.confidence is not None:
                response_data["confidence"] = result.confidence.model_dump()

            return [
                TextContent(
                    type="text",
                    text=json.dumps(response_data),
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
                    text=json.dumps(
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


async def handle_reference(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Get complete memory details (Layer 3)."""
    memory_uuid, error = _validate_memory_id(args, "reference")
    if error or memory_uuid is None:
        return error or _error_response("invalid_memory_id_format", "Invalid memory_id")

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
            }

            await _log_tool_usage(
                db, user_id, "reference", start_time, 200, current_context_id, workspace_id
            )
            # #1257: reference marks the context as used (guarded UPDATE at
            # commit time; the memory_not_found return above never touches).
            await _touch_context_last_used(db, current_context)
            await db.commit()

            return [
                TextContent(
                    type="text",
                    text=json.dumps({"status": "success", "memory": reference_data}),
                )
            ]
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
