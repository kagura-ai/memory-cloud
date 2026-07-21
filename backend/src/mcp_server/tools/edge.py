"""MCP tool handlers: Edge CRUD (list/create/update/delete).

Issue #163: Neural Memory edge management tools.
"""

import time
from itertools import chain
from typing import Any
from uuid import UUID

from mcp.types import TextContent

from mcp_server.tools._helpers import (
    _check_viewer_permission,
    _ContextNotFoundError,
    _error_response,
    _log_tool_usage,
    _resolve_context,
    _resolve_context_for_read,
    _resolve_context_id,
    _success_response,
    _validate_memory_id,
    execute_with_timeout,
)
from models.memory import EDGE_ORIGIN_DECLARED
from services.edge_service import (
    VALID_EDGE_TYPES,
    create_declared_edge,
)
from services.edge_service import (
    edge_to_dict as _edge_to_dict,
)

# The declared-edge write core (`create_declared_edge`), the DB-accepted
# `VALID_EDGE_TYPES` set, the `_edge_to_dict` serializer, and the #1403
# supersede self-heal now live in `services/edge_service.py` so the REST
# `POST /graph/edges` endpoint (#1416) shares the exact same path. This module
# keeps the MCP-transport wrapper: TextContent framing, usage telemetry, the
# per-tool timeout, and the context-resolution / viewer-permission gates.
# `VALID_EDGE_TYPES` is re-exported here for backward compatibility with
# existing importers (e.g. tests/test_edge_type_constants.py).


def _validate_edge_endpoints(
    args: dict[str, Any],
) -> tuple[tuple[UUID, UUID] | None, list[TextContent] | None]:
    """Validate and parse source_id/target_id from args."""
    source_id = args.get("source_id")
    target_id = args.get("target_id")
    if not source_id or not target_id:
        return None, _error_response(
            "missing_required_fields",
            "source_id and target_id are required.",
        )
    try:
        source_uuid = UUID(str(source_id))
        target_uuid = UUID(str(target_id))
    except (ValueError, AttributeError):
        return None, _error_response(
            "invalid_uuid_format", "source_id and target_id must be valid UUIDs."
        )
    if source_uuid == target_uuid:
        return None, _error_response(
            "self_loop_not_allowed",
            "source_id and target_id must be different (self-loops are not supported).",
        )
    return (source_uuid, target_uuid), None


def _parse_float(
    value: Any, name: str, min_val: float, max_val: float, default: float | None = None
) -> tuple[float | None, list[TextContent] | None]:
    """Parse and validate a float parameter.

    ``default`` is the fallback returned when ``value is None``. Pass it only
    at call sites that are actually reachable with ``value is None`` (an
    unguarded ``args.get(...)``). At a site already guarded by
    ``if value is not None`` the fallback is unreachable, so omit it
    (``default=None``) rather than passing a misleading literal — see
    ``handle_update_edge``. ``default=None`` means "no fallback; the caller
    guarantees ``value`` is not None", in which case the ``(None, None)``
    return is unreachable.
    """
    if value is None:
        return default, None
    try:
        f = float(value)
    except (ValueError, TypeError):
        return None, _error_response("validation_error", f"{name} must be a number.")
    if f < min_val or f > max_val:
        return None, _error_response(
            "validation_error", f"{name} must be between {min_val} and {max_val}."
        )
    return f, None


async def handle_list_edges(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """List edges connected to a specific memory.

    Visibility: shared context → workspace members see all creators' edges;
    private context → only the creator sees edges, non-creators get a uniform
    ``context_not_found`` denial. ``context.workspace_id`` (from the resolver)
    is authoritative — the MCP-session ``workspace_id`` is informational only.
    """
    memory_uuid, error = _validate_memory_id(args, "list_edges")
    if error or memory_uuid is None:
        return error or _error_response("invalid_memory_id_format", "Invalid memory_id")

    from db.base import get_db
    from repositories.neural_edge import NeuralEdgeRepository

    min_weight, error = _parse_float(args.get("min_weight"), "min_weight", 0.0, 3.0, 0.0)
    if error:
        return error

    edge_types = args.get("edge_types")
    limit = args.get("limit")

    start_time = time.time()
    current_context_id = None
    async for db in get_db():
        try:
            current_context_id = _resolve_context_id(args["context_id"])
            context = await _resolve_context_for_read(db, user_id, current_context_id)

            ws_id = str(context.workspace_id)
            ctx_id = str(context.id)
            owner_filter = user_id if context.is_private else None

            repo = NeuralEdgeRepository(db)

            outgoing = await execute_with_timeout(
                repo.get_outgoing_edges(
                    user_id=owner_filter,
                    src_id=memory_uuid,
                    min_weight=min_weight,
                    edge_types=edge_types,
                    limit=limit,
                    workspace_id=ws_id,
                    context_id=ctx_id,
                ),
                operation_name="list_edges_outgoing",
            )
            incoming = await execute_with_timeout(
                repo.get_incoming_edges(
                    user_id=owner_filter,
                    dst_id=memory_uuid,
                    min_weight=min_weight,
                    edge_types=edge_types,
                    limit=limit,
                    workspace_id=ws_id,
                    context_id=ctx_id,
                ),
                operation_name="list_edges_incoming",
            )

            seen_ids: set[int] = set()
            edges = []
            for edge in chain(outgoing, incoming):
                if edge.id not in seen_ids:
                    seen_ids.add(edge.id)
                    edges.append(_edge_to_dict(edge))

            await _log_tool_usage(
                db, user_id, "list_edges", start_time, 200, current_context_id, workspace_id
            )
            await db.commit()

            return _success_response(
                memory_id=str(memory_uuid),
                edges=edges,
                count=len(edges),
            )
        except _ContextNotFoundError as e:
            await db.rollback()
            # #1318: the requested context_id failed resolution — do not
            # stamp it as the usage_stats FK.
            await _log_tool_usage(db, user_id, "list_edges", start_time, 404, None, workspace_id)
            return e.to_response()
        except Exception:
            await db.rollback()
            await _log_tool_usage(
                db, user_id, "list_edges", start_time, 500, current_context_id, workspace_id
            )
            raise

    # Safety: should never reach here (get_db always yields)
    return _error_response("internal_error", "Database session unavailable")


async def handle_create_edge(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Create an edge between two memories (deterministic duplicate contract, #1321).

    Duplicate behavior on an existing (source, target) pair:
    - existing ``origin != 'declared'`` (hebbian/semantic auto-edge): upsert
      proceeds — a user assertion may update an automatic edge's values —
      and the response carries ``operation: "updated"`` plus the pre-image.
      Since create_edge asserts ``origin='declared'``, the repo's #1406
      origin CASE makes that assertion win over the row's existing provenance:
      an existing 'hebbian' OR 'semantic' row is promoted to 'declared' (a
      user assertion outranks machine provenance). The response's
      ``edge.origin`` field makes the outcome visible.
    - existing ``origin == 'declared'`` with identical edge_type/weight/
      confidence: no write, ``operation: "unchanged"`` (keeps client
      timeout-retries idempotent).
    - existing ``origin == 'declared'`` with differing values: rejected with
      ``edge_exists`` unless ``overwrite=true`` — declared links are
      provenance (#741) and must not be silently clobbered.

    The repository upsert itself stays untouched: SDK/worker replay paths
    (REST) depend on its unconditional 3-col upsert semantics.
    """
    endpoints, error = _validate_edge_endpoints(args)
    if error or endpoints is None:
        return error or _error_response("validation_error", "Invalid edge endpoints")
    source_uuid, target_uuid = endpoints

    edge_type = args.get("edge_type", "related_to")
    if edge_type not in VALID_EDGE_TYPES:
        return _error_response(
            "invalid_edge_type",
            f"edge_type must be one of: {', '.join(sorted(VALID_EDGE_TYPES))}",
        )

    # Issue #738: MCP `create_edge` is the explicit user-assertion path, so
    # default weight is 1.0 (full confidence) rather than the prior 0.5
    # (Hebbian-era simplified default) and origin is pinned to 'declared' so
    # the row is exempt from `DecayManager` (which only touches `origin='hebbian'`).
    weight, error = _parse_float(args.get("weight"), "weight", 0.0, 3.0, 1.0)
    if error:
        return error

    confidence, error = _parse_float(args.get("confidence"), "confidence", 0.0, 1.0, 1.0)
    if error:
        return error

    # Fail closed: `overwrite` gates a destructive path, so an unrecognized
    # value must error rather than count as truthy. The coercion layer maps
    # "true"/"1"/"yes"/"on" (and the falsy set) to real bools; anything else
    # (e.g. a client stringifying null as "null") arrives here non-bool.
    overwrite = args.get("overwrite", False)
    if not isinstance(overwrite, bool):
        return _error_response(
            "validation_error",
            "overwrite must be a boolean (true or false).",
        )

    from db.base import get_db

    start_time = time.time()
    current_context_id = None
    async for db in get_db():
        try:
            current_context_id = _resolve_context_id(args["context_id"])
            context = await _resolve_context(db, user_id, current_context_id)

            perm_error = await _check_viewer_permission(db, user_id, workspace_id, "create edges")
            if perm_error:
                return perm_error

            ws_id = str(workspace_id) if workspace_id else str(context.workspace_id)
            ctx_id = str(current_context_id)

            # Shared declared-edge core (#1321 duplicate contract + #1403 supersede
            # self-heal), also used by the REST POST /graph/edges endpoint (#1416).
            # Wrapped in the per-tool timeout so a hung DB can't stall the tool;
            # the transaction and telemetry stay owned here (transport concerns).
            result = await execute_with_timeout(
                create_declared_edge(
                    db,
                    user_id=user_id,
                    source_id=source_uuid,
                    target_id=target_uuid,
                    edge_type=edge_type,
                    weight=weight,
                    confidence=confidence,
                    workspace_id=ws_id,
                    context_id=ctx_id,
                    overwrite=overwrite,
                ),
                operation_name="create_edge",
            )

            if result.operation == "conflict":
                await db.rollback()
                await _log_tool_usage(
                    db, user_id, "create_edge", start_time, 409, current_context_id, workspace_id
                )
                return _error_response(
                    "edge_exists",
                    f"A declared edge already exists from {args.get('source_id')} to "
                    f"{args.get('target_id')} with different values. Use update_edge to "
                    "modify it, or pass overwrite=true to re-assert it.",
                    existing_edge=result.existing,
                )

            await _log_tool_usage(
                db, user_id, "create_edge", start_time, 200, current_context_id, workspace_id
            )
            await db.commit()

            if result.operation == "unchanged":
                return _success_response(edge=result.edge, operation="unchanged")
            if result.previous is not None:
                return _success_response(
                    edge=result.edge, operation="updated", previous=result.previous
                )
            return _success_response(edge=result.edge, operation="created")
        except _ContextNotFoundError as e:
            await db.rollback()
            return e.to_response()
        except Exception:
            await db.rollback()
            await _log_tool_usage(
                db, user_id, "create_edge", start_time, 500, current_context_id, workspace_id
            )
            raise

    # Safety: should never reach here (get_db always yields)
    return _error_response("internal_error", "Database session unavailable")


async def handle_update_edge(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Update an existing edge's weight or type."""
    endpoints, error = _validate_edge_endpoints(args)
    if error or endpoints is None:
        return error or _error_response("validation_error", "Invalid edge endpoints")
    source_uuid, target_uuid = endpoints

    new_weight_raw = args.get("weight")
    new_edge_type = args.get("edge_type")

    if new_weight_raw is None and new_edge_type is None:
        return _error_response(
            "no_fields_to_update",
            "Provide at least one of: weight, edge_type.",
        )

    if new_edge_type is not None and new_edge_type not in VALID_EDGE_TYPES:
        return _error_response(
            "invalid_edge_type",
            f"edge_type must be one of: {', '.join(sorted(VALID_EDGE_TYPES))}",
        )

    new_weight = None
    if new_weight_raw is not None:
        # Guarded by the `is not None` check above, so _parse_float's None
        # fallback is unreachable here — omit it. (Contrast create_edge, which
        # parses an unguarded args.get("weight") and passes an explicit 1.0
        # fallback, #814.) Omitting weight from update_edge preserves the
        # edge's current weight; pass edge_type to make a no-weight update.
        new_weight, error = _parse_float(new_weight_raw, "weight", 0.0, 3.0)
        if error:
            return error

    from db.base import get_db
    from repositories.neural_edge import NeuralEdgeRepository

    start_time = time.time()
    current_context_id = None
    async for db in get_db():
        try:
            current_context_id = _resolve_context_id(args["context_id"])
            context = await _resolve_context(db, user_id, current_context_id)

            perm_error = await _check_viewer_permission(db, user_id, workspace_id, "update edges")
            if perm_error:
                return perm_error

            ws_id = str(workspace_id) if workspace_id else str(context.workspace_id)
            ctx_id = str(current_context_id)

            repo = NeuralEdgeRepository(db)

            existing = await repo.get_edge(
                user_id,
                source_uuid,
                target_uuid,
                workspace_id=ws_id,
                context_id=ctx_id,
            )
            if existing is None:
                await db.rollback()
                return _error_response(
                    "edge_not_found",
                    f"No edge found from {args.get('source_id')} to {args.get('target_id')}.",
                )

            edge = await execute_with_timeout(
                repo.create_or_update_edge(
                    user_id=user_id,
                    src_id=source_uuid,
                    dst_id=target_uuid,
                    edge_type=new_edge_type or existing.edge_type,
                    weight=new_weight if new_weight is not None else existing.weight,
                    confidence=existing.confidence,
                    workspace_id=ws_id,
                    context_id=ctx_id,
                    # Issue #782: MCP `update_edge` is the explicit user re-assertion
                    # path (mirrors `handle_create_edge` above); pin origin='declared'
                    # so retyping a hebbian row to a producer-asserted edge_type
                    # (continues_from / references_file) doesn't leave origin='hebbian'
                    # and re-expose the row to DecayManager pruning.
                    origin=EDGE_ORIGIN_DECLARED,
                ),
                operation_name="update_edge",
            )

            # #1403: like handle_create_edge, an update that retypes the edge to
            # 'supersedes' may confirm a stored suggestion — record the
            # acceptance and clear the candidate in the same transaction, so a
            # suggestion confirmed via update_edge (not just create_edge) stops
            # resurfacing on recall/reference.
            if edge.edge_type == EDGE_TYPE_SUPERSEDES:
                await _accept_supersede_candidate_if_matching(
                    db, src_id=source_uuid, dst_id=target_uuid
                )

            await _log_tool_usage(
                db, user_id, "update_edge", start_time, 200, current_context_id, workspace_id
            )
            await db.commit()

            return _success_response(edge=_edge_to_dict(edge))
        except _ContextNotFoundError as e:
            await db.rollback()
            return e.to_response()
        except Exception:
            await db.rollback()
            await _log_tool_usage(
                db, user_id, "update_edge", start_time, 500, current_context_id, workspace_id
            )
            raise

    # Safety: should never reach here (get_db always yields)
    return _error_response("internal_error", "Database session unavailable")


async def handle_delete_edge(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Delete an edge between two memories."""
    endpoints, error = _validate_edge_endpoints(args)
    if error or endpoints is None:
        return error or _error_response("validation_error", "Invalid edge endpoints")
    source_uuid, target_uuid = endpoints

    from db.base import get_db
    from repositories.neural_edge import NeuralEdgeRepository

    start_time = time.time()
    current_context_id = None
    async for db in get_db():
        try:
            current_context_id = _resolve_context_id(args["context_id"])
            context = await _resolve_context(db, user_id, current_context_id)

            perm_error = await _check_viewer_permission(db, user_id, workspace_id, "delete edges")
            if perm_error:
                return perm_error

            ws_id = str(workspace_id) if workspace_id else str(context.workspace_id)
            ctx_id = str(current_context_id)

            repo = NeuralEdgeRepository(db)
            deleted = await execute_with_timeout(
                repo.delete_edge(
                    user_id=user_id,
                    src_id=source_uuid,
                    dst_id=target_uuid,
                    workspace_id=ws_id,
                    context_id=ctx_id,
                ),
                operation_name="delete_edge",
            )

            await _log_tool_usage(
                db, user_id, "delete_edge", start_time, 200, current_context_id, workspace_id
            )
            await db.commit()

            if deleted:
                return _success_response(
                    message=f"Edge deleted: {args.get('source_id')} → {args.get('target_id')}",
                )
            else:
                return _error_response(
                    "edge_not_found",
                    f"No edge found from {args.get('source_id')} to {args.get('target_id')}.",
                )
        except _ContextNotFoundError as e:
            await db.rollback()
            return e.to_response()
        except Exception:
            await db.rollback()
            await _log_tool_usage(
                db, user_id, "delete_edge", start_time, 500, current_context_id, workspace_id
            )
            raise

    # Safety: should never reach here (get_db always yields)
    return _error_response("internal_error", "Database session unavailable")
