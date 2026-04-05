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
    _resolve_context_id,
    _success_response,
    _validate_memory_id,
    execute_with_timeout,
)

VALID_EDGE_TYPES = frozenset({"neural_association", "related_to", "depends_on", "learned_from"})


def _edge_to_dict(edge: Any) -> dict[str, Any]:
    """Convert NeuralMemoryEdge to JSON-serializable dict."""
    return {
        "source_id": str(edge.src_id),
        "target_id": str(edge.dst_id),
        "edge_type": edge.edge_type,
        "weight": edge.weight,
        "confidence": edge.confidence,
        "created_at": edge.created_at.isoformat() if edge.created_at else None,
        "last_updated": edge.last_updated.isoformat() if edge.last_updated else None,
    }


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
    value: Any, name: str, min_val: float, max_val: float, default: float
) -> tuple[float | None, list[TextContent] | None]:
    """Parse and validate a float parameter."""
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
    """List edges connected to a specific memory."""
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
    async for db in get_db():
        try:
            current_context_id = _resolve_context_id(args["context_id"])
            context = await _resolve_context(db, user_id, current_context_id)

            # Use context's workspace_id for isolation (fallback from MCP session)
            ws_id = str(workspace_id) if workspace_id else str(context.workspace_id)
            ctx_id = str(current_context_id)

            repo = NeuralEdgeRepository(db)

            outgoing = await execute_with_timeout(
                repo.get_outgoing_edges(
                    user_id=user_id,
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
                    user_id=user_id,
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
            return e.to_response()
        except Exception:
            await db.rollback()
            await _log_tool_usage(
                db, user_id, "list_edges", start_time, 500, args.get("context_id"), workspace_id
            )
            raise

    # Safety: should never reach here (get_db always yields)
    return _error_response("internal_error", "Database session unavailable")


async def handle_create_edge(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Create a new edge between two memories."""
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

    weight, error = _parse_float(args.get("weight"), "weight", 0.0, 3.0, 0.5)
    if error:
        return error

    confidence, error = _parse_float(args.get("confidence"), "confidence", 0.0, 1.0, 1.0)
    if error:
        return error

    from db.base import get_db
    from repositories.neural_edge import NeuralEdgeRepository

    start_time = time.time()
    async for db in get_db():
        try:
            current_context_id = _resolve_context_id(args["context_id"])
            context = await _resolve_context(db, user_id, current_context_id)

            perm_error = await _check_viewer_permission(db, user_id, workspace_id, "create edges")
            if perm_error:
                return perm_error

            ws_id = str(workspace_id) if workspace_id else str(context.workspace_id)
            ctx_id = str(current_context_id)

            repo = NeuralEdgeRepository(db)
            edge = await execute_with_timeout(
                repo.create_or_update_edge(
                    user_id=user_id,
                    src_id=source_uuid,
                    dst_id=target_uuid,
                    edge_type=edge_type,
                    weight=weight,
                    confidence=confidence,
                    workspace_id=ws_id,
                    context_id=ctx_id,
                ),
                operation_name="create_edge",
            )

            await _log_tool_usage(
                db, user_id, "create_edge", start_time, 200, current_context_id, workspace_id
            )
            await db.commit()

            return _success_response(edge=_edge_to_dict(edge))
        except _ContextNotFoundError as e:
            await db.rollback()
            return e.to_response()
        except Exception:
            await db.rollback()
            await _log_tool_usage(
                db, user_id, "create_edge", start_time, 500, args.get("context_id"), workspace_id
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
        new_weight, error = _parse_float(new_weight_raw, "weight", 0.0, 3.0, 0.5)
        if error:
            return error

    from db.base import get_db
    from repositories.neural_edge import NeuralEdgeRepository

    start_time = time.time()
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
                ),
                operation_name="update_edge",
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
                db, user_id, "update_edge", start_time, 500, args.get("context_id"), workspace_id
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
                db, user_id, "delete_edge", start_time, 500, args.get("context_id"), workspace_id
            )
            raise

    # Safety: should never reach here (get_db always yields)
    return _error_response("internal_error", "Database session unavailable")
