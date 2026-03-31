"""MCP tool handler: explore (graph traversal).

Extracted from tools.py for modularity (Issue #7).
"""

import json
import logging
import time
from typing import Any
from uuid import UUID

from mcp.types import TextContent

from mcp_server.tools._helpers import (
    _ContextNotFoundError,
    _error_response,
    _log_tool_usage,
    _resolve_context,
    _resolve_context_id,
    _validate_memory_id,
    execute_with_timeout,
)

logger = logging.getLogger(__name__)


async def handle_explore(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Discover related memories via Neural Memory graph traversal."""
    memory_uuid, error = _validate_memory_id(args, "explore")
    if error or memory_uuid is None:
        return error or _error_response("invalid_memory_id_format", "Invalid memory_id")

    from db.base import get_db
    from models.schemas import ExploreRequest
    from services.memory_service import MemoryService

    request = ExploreRequest(
        memory_id=memory_uuid,
        depth=args.get("depth", 2),
        relation_types=args.get("relation_types"),
        min_weight=args.get("min_weight", 0.05),
    )

    start_time = time.time()
    async for db in get_db():
        try:
            current_context_id = _resolve_context_id(args["context_id"])
            await _resolve_context(db, user_id, current_context_id)

            service = MemoryService(db)
            result = await execute_with_timeout(
                service.explore(
                    request,
                    user_id=user_id,
                    current_context_id=current_context_id,
                    current_workspace_id=workspace_id,
                ),
                operation_name="explore",
            )

            explore_data = {
                "seed_memory": {
                    "memory_id": str(result.seed_memory.memory_id),
                    "summary": result.seed_memory.summary,
                    "type": result.seed_memory.type,
                },
                "related_memories": [
                    {
                        "memory_id": str(r.memory_id),
                        "summary": r.summary,
                        "activation": r.activation,
                        "hop": r.hop,
                        "weight": r.weight,
                        "path": [str(p) for p in r.path],
                    }
                    for r in result.related_memories
                ],
                "metadata": result.metadata,
            }

            await _log_tool_usage(
                db, user_id, "explore", start_time, 200, current_context_id, workspace_id
            )
            await db.commit()

            return [
                TextContent(
                    type="text",
                    text=json.dumps({"status": "success", "exploration": explore_data}),
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
                "explore",
                start_time,
                500,
                args.get("context_id"),
                workspace_id,
            )
            raise

    # Safety: should never reach here (get_db always yields)
    return _error_response("internal_error", "Database session unavailable")
