"""MCP tool handler: update_search_config.

Extracted from tools.py for modularity (Issue #7).
"""

import time
from typing import Any
from uuid import UUID

from mcp.types import TextContent

from mcp_server.tools._errors import _tool_exception_response
from mcp_server.tools._helpers import (
    _dumps,
    _error_response,
    _format_validation_error,
    _log_tool_usage,
)
from services import plan_seats
from services.capacity_lock import CapacityPolicy


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

            from config.settings import get_settings
            from models.schemas import ContextSearchConfigUpdate
            from repositories.config_repository import (
                ContextSearchConfigRepository,
                search_config_defaults,
            )
            from services.content_chunk_policy import content_chunks_effective
            from services.llm_selector_policy import llm_selector_effective
            from services.permission_service import (
                PermissionService,
                gate_content_chunks_change,
                gate_llm_selector_change,
            )
            from services.reranker_service import default_reranker_model_for
            from utils.exceptions import (
                AuthorizationError,
                FeatureNotAvailableError,
                NotFoundException,
                QuotaExceededError,
                ValidationError,
            )

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
                # dispatcher-gated (update_search_config is a blocked tool)
                await perm_service.check_context_write(
                    user_id, ctx_uuid, capacity=CapacityPolicy.ALLOWED
                )
                # #1943: search settings change how the context's content is
                # searched — treated as a content write, not metadata, like the
                # REST twin.
                await plan_seats.ensure_context_writable(db, ctx_uuid)
            except (AuthorizationError, NotFoundException) as perm_err:
                # The two designed denials only (#1684): a database failure
                # here reaches the catch-all below instead of being reported
                # as permission_denied with the driver's text.
                return _error_response("permission_denied", str(perm_err))

            repo = ContextSearchConfigRepository(db)
            config = await repo.get_by_context(ctx_uuid)

            if not config:
                return _error_response(
                    "not_found",
                    f"No search config for context {args['context_id']}",
                )

            # Build update with current values as defaults. A row with a NULL
            # provider/model falls back to the deployment default (#1572), the
            # model derived for whichever provider is in effect.
            settings = get_settings()
            defaults = search_config_defaults(settings)
            provider = args.get(
                "reranker_provider", config.reranker_provider or defaults["reranker_provider"]
            )
            update_fields = {
                "semantic_weight": args.get("semantic_weight", float(config.semantic_weight)),
                "bm25_weight": args.get("bm25_weight", float(config.bm25_weight)),
                "fetch_factor": args.get("fetch_factor", config.fetch_factor),
                "use_rerank": args.get("use_rerank", config.use_rerank),
                "reranker_provider": provider,
                "reranker_model": args.get(
                    "reranker_model",
                    config.reranker_model or default_reranker_model_for(provider, settings),
                ),
                # Issue #1048: reinforce re-rank knobs (round-trip current values).
                "reinforce_enabled": args.get("reinforce_enabled", config.reinforce_enabled),
                "reinforce_max_boost": args.get(
                    "reinforce_max_boost", float(config.reinforce_max_boost)
                ),
                # Issue #1065: forge-resistant mode (round-trip current value).
                "reinforce_require_host_arbitration": args.get(
                    "reinforce_require_host_arbitration",
                    config.reinforce_require_host_arbitration,
                ),
                # Issue #1212: query-intent router gate (round-trip current value).
                "routing_mode": args.get("routing_mode", config.routing_mode),
            }
            # Issue #2015: content chunk vectors opt-in. Not round-tripped: an
            # omitted flag stays out of the update (the repository applies
            # exclude_unset), and changing it is gated below.
            if "content_chunks_enabled" in args:
                update_fields["content_chunks_enabled"] = args["content_chunks_enabled"]
            # Issue #2144: the recall LLM selector opt-in, on the same terms.
            if "llm_selector_enabled" in args:
                update_fields["llm_selector_enabled"] = args["llm_selector_enabled"]

            # Validate via Pydantic (same as REST API)
            try:
                update_data = ContextSearchConfigUpdate(**update_fields)
            except Exception as validation_err:
                # #1323: don't leak the raw pydantic dump into the envelope.
                return _error_response(
                    "invalid_search_config", _format_validation_error(validation_err)
                )

            # #2015: changing content_chunks_enabled takes the context owner (or
            # a workspace owner/admin), like the REST twin; the stored value is
            # a no-op and not written.
            try:
                update_data = await gate_content_chunks_change(
                    perm_service,
                    user_id,
                    ctx_uuid,
                    update_data,
                    current_enabled=config.content_chunks_enabled,
                )
            except AuthorizationError as perm_err:
                return _error_response(
                    "permission_denied",
                    str(perm_err),
                    required_role="owner",
                    help=(
                        "Ask the context owner or a workspace owner/admin to change "
                        "content_chunks_enabled. Other search settings need only "
                        "editor access."
                    ),
                )
            except ValidationError as config_err:
                # #2072: the vector store has no content chunk points (LanceDB).
                if config_err.details.get("field") != "content_chunks_enabled":
                    raise
                return _error_response("invalid_search_config", config_err.message)
            except (FeatureNotAvailableError, QuotaExceededError) as gate_err:
                # #2103: the plan lacks content chunks, or the workspace is at
                # its cap — the vocabulary code (feature_not_available /
                # quota_exceeded) with the REST gate details, not this tool's
                # catch-all code.
                await db.rollback()
                return _tool_exception_response("update_search_config", gate_err)

            # #2144: the same owner rule and plan gate for the recall LLM
            # selector, after the chunk gate (a fixed order for the two
            # per-workspace cap locks), like the REST twin.
            try:
                update_data = await gate_llm_selector_change(
                    perm_service,
                    user_id,
                    ctx_uuid,
                    update_data,
                    current_enabled=config.llm_selector_enabled is True,
                )
            except AuthorizationError as perm_err:
                await db.rollback()
                return _error_response(
                    "permission_denied",
                    str(perm_err),
                    required_role="owner",
                    help=(
                        "Ask the context owner or a workspace owner/admin to change "
                        "llm_selector_enabled. Other search settings need only "
                        "editor access."
                    ),
                )
            except (FeatureNotAvailableError, QuotaExceededError) as gate_err:
                # The plan lacks llm_selector, or the workspace is at its cap.
                await db.rollback()
                return _tool_exception_response("update_search_config", gate_err)

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
                    text=_dumps(
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
                                "reinforce_require_host_arbitration": (
                                    config.reinforce_require_host_arbitration
                                ),
                                "routing_mode": config.routing_mode,
                                "content_chunks_enabled": config.content_chunks_enabled,
                                # #2103 / #2080: whether the stored flag is acted
                                # on (store, and the plan where gated).
                                "content_chunks_effective": await content_chunks_effective(
                                    db,
                                    ctx_uuid,
                                    stored=config.content_chunks_enabled is True,
                                ),
                                "llm_selector_enabled": config.llm_selector_enabled,
                                # #2144: whether recall acts on the stored flag
                                # (the plan, where the deployment gates it).
                                "llm_selector_effective": await llm_selector_effective(
                                    db,
                                    ctx_uuid,
                                    stored=config.llm_selector_enabled is True,
                                ),
                            },
                        }
                    ),
                )
            ]
        except Exception as e:
            await db.rollback()
            return _tool_exception_response(
                "update_search_config", e, error="update_search_config_error"
            )

    # Safety: should never reach here (get_db always yields)
    return _error_response("internal_error", "Database session unavailable")
