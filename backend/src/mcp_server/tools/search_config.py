"""MCP tool handler: update_search_config.

Extracted from tools.py for modularity (Issue #7).
"""

import json
import logging
import time
from typing import Any
from uuid import UUID

from mcp.types import TextContent

from mcp_server.tools._helpers import (
    _error_response,
    _log_tool_usage,
)

logger = logging.getLogger(__name__)


async def handle_update_search_config(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Update search configuration for a context."""
    if "context_id" not in args:
        return _error_response("missing_fields", "Missing required field: context_id")

    start_time = time.time()

    from db.base import get_db

    async for db in get_db():
        try:
            from uuid import UUID as _UUID

            from models.schemas import ContextSearchConfigUpdate
            from repositories.config_repository import ContextSearchConfigRepository
            from services.permission_service import PermissionService

            # Parse context_id
            try:
                ctx_uuid = _UUID(args["context_id"])
            except ValueError:
                return _error_response(
                    "invalid_context_id",
                    f"Invalid context_id: {args['context_id']}",
                )

            # Permission check (owner/editor)
            perm_service = PermissionService(db)
            try:
                await perm_service.check_context_write(user_id, ctx_uuid)
            except Exception as perm_err:
                return _error_response("permission_denied", str(perm_err))

            repo = ContextSearchConfigRepository(db)
            config = await repo.get_by_context(ctx_uuid)

            if not config:
                return _error_response(
                    "not_found",
                    f"No search config for context {args['context_id']}",
                )

            # Build update with current values as defaults
            update_fields = {
                "semantic_weight": args.get("semantic_weight", float(config.semantic_weight)),
                "bm25_weight": args.get("bm25_weight", float(config.bm25_weight)),
                "fetch_factor": args.get("fetch_factor", config.fetch_factor),
                "use_rerank": args.get("use_rerank", config.use_rerank),
                "reranker_provider": args.get(
                    "reranker_provider", config.reranker_provider or "voyage"
                ),
                "reranker_model": args.get("reranker_model", config.reranker_model or "rerank-2"),
                # Issue #1048: reinforce re-rank knobs (round-trip current values).
                "reinforce_enabled": args.get("reinforce_enabled", config.reinforce_enabled),
                "reinforce_max_boost": args.get(
                    "reinforce_max_boost", float(config.reinforce_max_boost)
                ),
            }

            # Validate via Pydantic (same as REST API)
            try:
                update_data = ContextSearchConfigUpdate(**update_fields)
            except Exception as validation_err:
                return _error_response("invalid_search_config", str(validation_err))

            # Apply via repository (same as REST API)
            config = await repo.update(ctx_uuid, update_data)

            await _log_tool_usage(
                db,
                user_id,
                "update_search_config",
                start_time,
                200,
                str(ctx_uuid),
                workspace_id,
            )

            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "status": "success",
                            "message": "Search configuration updated.",
                            "context_id": str(ctx_uuid),
                            "config": {
                                "semantic_weight": float(config.semantic_weight),
                                "bm25_weight": float(config.bm25_weight),
                                "fetch_factor": config.fetch_factor,
                                "use_rerank": config.use_rerank,
                                "reranker_provider": config.reranker_provider,
                                "reranker_model": config.reranker_model,
                                "reinforce_enabled": config.reinforce_enabled,
                                "reinforce_max_boost": float(config.reinforce_max_boost),
                            },
                        }
                    ),
                )
            ]
        except Exception as e:
            await db.rollback()
            logger.error(f"update_search_config_failed: {e}", exc_info=True)
            return _error_response("update_search_config_error", str(e))

    # Safety: should never reach here (get_db always yields)
    return _error_response("internal_error", "Database session unavailable")
