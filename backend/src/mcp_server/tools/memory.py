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
    _log_tool_usage,
    _resolve_context,
    _resolve_context_id,
    _validate_memory_id,
    execute_with_timeout,
)

logger = logging.getLogger(__name__)


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

            current_context = await _resolve_context(db, user_id, current_context_id)

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
        )
    except (ValueError, ValidationError) as e:
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

            current_context = await _resolve_context(db, user_id, current_context_id)

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
        search_mode=args.get("search_mode", "hybrid"),
    )

    start_time = time.time()
    async for db in get_db():
        try:
            # Issue #81: Cross-context recall — context_ids overrides context_id
            context_ids_arg = args.get("context_ids")
            cross_context_ids: list[UUID] | None = None

            if isinstance(context_ids_arg, list) and context_ids_arg:
                # Multi-context mode
                cross_context_ids = [_resolve_context_id(cid) for cid in context_ids_arg]
                current_context_id = cross_context_ids[0]
                # Validate access to all contexts, keep primary context object
                current_context = await _resolve_context(db, user_id, current_context_id)
                for cid in cross_context_ids[1:]:
                    await _resolve_context(db, user_id, cid)

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
                current_context_id = _resolve_context_id(args["context_id"])
                current_context = await _resolve_context(db, user_id, current_context_id)

            service = MemoryService(db)
            result = await execute_with_timeout(
                service.recall(
                    request,
                    user_id=user_id,
                    current_context_id=current_context_id,
                    current_workspace_id=workspace_id,
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
                }
                for r in result.results
            ]

            related_tags_data = [
                {"tag": tag.tag, "count": tag.count, "sample_summary": tag.sample_summary}
                for tag in result.related_tags
            ]

            await _log_tool_usage(
                db, user_id, "recall", start_time, 200, current_context_id, workspace_id
            )
            await db.commit()

            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "status": "success",
                            "results": results_data,
                            "count": len(results_data),
                            "related_tags": related_tags_data,
                            **_context_response_fields(current_context),
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

            current_context = await _resolve_context(db, user_id, current_context_id)

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
            await _resolve_context(db, user_id, current_context_id)

            service = MemoryService(db)
            try:
                result = await execute_with_timeout(
                    service.reference(request.memory_id, user_id=user_id),
                    operation_name="reference",
                )
            except Exception as e:
                from utils.exceptions import NotFoundException

                if isinstance(e, NotFoundException):
                    return _error_response(
                        "memory_not_found",
                        f"Memory not found or you don't have access: {request.memory_id}",
                        help="Use recall() to find memories you have access to.",
                    )
                raise

            reference_data = {
                "memory_id": str(result.memory_id),
                "summary": result.summary,
                "context_summary": result.context_summary,
                "content": result.content,
                "details": result.details,
                "type": result.type,
                "importance": result.importance,
                "tags": result.tags,
                "context": result.context,
                "created_at": result.created_at.isoformat(),
                "client": result.client,
            }

            await _log_tool_usage(
                db, user_id, "reference", start_time, 200, current_context_id, workspace_id
            )
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
