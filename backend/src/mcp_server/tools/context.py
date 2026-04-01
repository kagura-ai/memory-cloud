"""MCP tool handlers: context operations.

Handles get_context_info, create_context, update_context, delete_context, list_contexts.
Extracted from tools.py for modularity (Issue #7).
"""

import json
import logging
import time
from typing import Any
from uuid import UUID

from mcp.types import TextContent

from mcp_server.tools._constants import KAGURA_MEMORY_INSTRUCTIONS
from mcp_server.tools._helpers import (
    _context_response_fields,
    _ContextNotFoundError,
    _error_response,
    _get_workspace_member_role,
    _log_tool_usage,
    _resolve_context_id,
    execute_with_timeout,
)

logger = logging.getLogger(__name__)


async def handle_get_context_info(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Retrieve context information and memory statistics."""
    include_details = args.get("include_details", True)

    from db.base import get_db

    start_time = time.time()
    async for db in get_db():
        try:
            from services.context_service import ContextService
            from services.memory_service import MemoryService

            current_context_id = _resolve_context_id(args["context_id"])

            # Get context details (with fallback for stats if access check fails)
            context_service = ContextService(db)
            current_context = None
            context_for_stats = None

            if current_context_id:
                try:
                    current_context = await context_service.get_context(user_id, current_context_id)
                    context_for_stats = current_context
                except Exception as e:
                    logger.warning(
                        f"Failed to fetch context with access check {current_context_id}: {e}"
                    )
                    from sqlalchemy import select

                    from models.auth import Context

                    result = await db.execute(
                        select(Context).where(
                            Context.id == current_context_id,
                            Context.deleted_at.is_(None),
                        )
                    )
                    context_for_stats = result.scalar_one_or_none()

            # Issue #204: Check if context is shared or if user is workspace owner
            is_shared = False
            workspace = None
            logger.info(
                f"MCP get_context_info: workspace_id={workspace_id}, current_context={current_context is not None}, context_for_stats={context_for_stats is not None}"
            )

            if context_for_stats and workspace_id:
                from sqlalchemy import select

                from models.auth import Workspace

                workspace_result = await db.execute(
                    select(Workspace).where(Workspace.id == workspace_id)
                )
                workspace = workspace_result.scalar_one_or_none()
                logger.info(
                    f"MCP workspace lookup: workspace_id={workspace_id}, workspace_found={workspace is not None}"
                )
                is_workspace_owner = workspace and workspace.owner_user_id == user_id
                is_shared = is_workspace_owner or not context_for_stats.is_private

            service = MemoryService(db)
            result = await execute_with_timeout(
                service.get_stats(
                    user_id=user_id,
                    workspace_id=str(workspace_id) if workspace_id else None,
                    context_id=str(current_context_id) if current_context_id else None,
                    include_details=include_details,
                    time_window_hours=168,
                    is_shared_context=is_shared,
                ),
                operation_name="get_context_info",
            )

            context_data = None
            if current_context:
                # Get embedding model info from search config
                from sqlalchemy import select as _ctx_select

                from config.settings import get_settings as _get_settings
                from models.config import ContextSearchConfig

                _settings = _get_settings()

                config_result = await db.execute(
                    _ctx_select(ContextSearchConfig).where(
                        ContextSearchConfig.context_id == current_context.id
                    )
                )
                search_config = config_result.scalar_one_or_none()

                context_data = {
                    "id": str(current_context.id),
                    "name": current_context.name,
                    "display_name": current_context.display_name,
                    "summary": current_context.summary
                    or "No summary provided. Please add a summary in the context settings.",
                    "usage_guide": current_context.usage_guide
                    or "No usage guide provided. Please add usage guidelines in the context settings.",
                    "is_private": current_context.is_private,
                    "embedding_model": search_config.embedding_model
                    if search_config
                    else _settings.embedding_model,
                    "embedding_dimensions": search_config.embedding_dimensions
                    if search_config
                    else _settings.embedding_dimensions,
                }

            workspace_data = None
            if workspace:
                workspace_data = {
                    "id": str(workspace.id),
                    "name": workspace.name,
                    "description": workspace.description,
                }

            stats_data: dict[str, Any] = {
                "total_memories": result.total_count,
                "working_memories": result.working_count,
                "persistent_memories": result.persistent_count,
            }
            if include_details:
                stats_data["details"] = {
                    "by_type": result.by_type,
                    "by_importance": result.by_importance,
                    "recent_7days": result.recent_activity,
                }

            await _log_tool_usage(
                db,
                user_id,
                "get_context_info",
                start_time,
                200,
                current_context_id,
                workspace_id,
            )
            await db.commit()

            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "status": "success",
                            "context": context_data,
                            "workspace": workspace_data,
                            "stats": stats_data,
                            "instructions": KAGURA_MEMORY_INSTRUCTIONS,
                        }
                    ),
                )
            ]
        except Exception as e:
            await db.rollback()
            await _log_tool_usage(
                db,
                user_id,
                "get_context_info",
                start_time,
                500,
                args.get("context_id"),
                workspace_id,
            )
            logger.error(f"get_context_info_failed: {e}", exc_info=True)
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "status": "error",
                            "error": str(e),
                            "message": "Failed to retrieve context info. Please try again.",
                        }
                    ),
                )
            ]

    # Safety: should never reach here (get_db always yields)
    return _error_response("internal_error", "Database session unavailable")


async def handle_create_context(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Create a new context in the workspace."""
    if "name" not in args:
        return _error_response(
            "missing_fields",
            "Missing required field: name",
            help="Provide a context name (lowercase alphanumeric + hyphen/underscore).",
        )

    from db.base import get_db

    start_time = time.time()
    async for db in get_db():
        try:
            from services.context_service import ContextService
            from services.quota_service import QuotaService

            # Check workspace_id is available
            if not workspace_id:
                return _error_response(
                    "workspace_required",
                    "Workspace ID is required to create a context.",
                    help="Ensure your MCP connection is configured with a workspace.",
                )

            # Check role: only owner/admin can create contexts
            user_role = await _get_workspace_member_role(db, user_id, workspace_id)
            if user_role not in ("owner", "admin"):
                return _error_response(
                    "permission_denied",
                    "Only workspace owners and admins can create contexts.",
                    your_role=user_role or "not_a_member",
                    required_role="owner or admin",
                )

            # Check context creation quota
            quota_service = QuotaService(db)
            can_create, error_msg = await quota_service.check_context_creation_allowed(workspace_id)
            if not can_create:
                return _error_response(
                    "quota_exceeded",
                    error_msg or "Context creation limit reached.",
                    help="Delete unused contexts or upgrade your plan.",
                )

            # Validate embedding_model if provided
            requested_model = args.get("embedding_model")
            if requested_model:
                from config.constants import EMBEDDING_MODEL_REGISTRY

                if requested_model not in EMBEDDING_MODEL_REGISTRY:
                    return _error_response(
                        "invalid_embedding_model",
                        f"Unknown embedding model: {requested_model}",
                        help=f"Supported models: {', '.join(EMBEDDING_MODEL_REGISTRY.keys())}",
                    )

            # Create context
            is_private = args.get("is_private", True)
            context_service = ContextService(db)
            context = await execute_with_timeout(
                context_service.create_context(
                    workspace_id=workspace_id,
                    name=args["name"],
                    display_name=args.get("display_name"),
                    description=args.get("description"),
                    summary=args.get("summary"),
                    usage_guide=args.get("usage_guide"),
                    created_by=user_id,
                    is_private=is_private,
                    embedding_model=requested_model,
                ),
                operation_name="create_context",
            )

            await _log_tool_usage(
                db,
                user_id,
                "create_context",
                start_time,
                200,
                str(context.id),
                workspace_id,
            )

            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "status": "success",
                            "message": f"Context '{args['name']}' created successfully.",
                            **_context_response_fields(context),
                        }
                    ),
                )
            ]
        except _ContextNotFoundError as e:
            await db.rollback()
            return e.to_response()
        except Exception as e:
            await db.rollback()
            error_str = str(e)
            # Surface validation errors clearly
            if "already exists" in error_str or "ValidationError" in type(e).__name__:
                return _error_response(
                    "validation_error",
                    error_str,
                    help="Check the context name and try again.",
                )
            logger.error(f"create_context_failed: {e}", exc_info=True)
            return _error_response("create_context_error", error_str)

    # Safety: should never reach here (get_db always yields)
    return _error_response("internal_error", "Database session unavailable")


async def handle_update_context(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Update an existing context's metadata."""
    if "context_id" not in args:
        return _error_response(
            "missing_fields",
            "Missing required field: context_id",
            help="Use list_contexts() to find context IDs.",
        )

    from db.base import get_db

    start_time = time.time()
    async for db in get_db():
        try:
            from uuid import UUID as _UUID

            from services.permission_service import PermissionService

            ctx_uuid = _UUID(args["context_id"])

            perm_service = PermissionService(db)
            owner_fields = {"summary", "usage_guide", "resource_id", "is_public"}
            requested_fields = {
                k
                for k in (
                    "summary",
                    "usage_guide",
                    "display_name",
                    "description",
                    "resource_id",
                    "is_public",
                )
                if k in args
            }

            if not requested_fields:
                return _error_response(
                    "no_changes",
                    "No fields to update. Provide at least one of: summary, usage_guide, display_name, description, resource_id, is_public.",
                )

            # Permission check using PermissionService (same as REST API)
            try:
                if requested_fields & owner_fields:
                    # Owner-only fields → require context owner
                    context = await perm_service.check_context_owner(user_id, ctx_uuid)
                else:
                    # display_name/description → require editor access
                    context, _ = await perm_service.check_context_access(
                        user_id, ctx_uuid, required_role="editor"
                    )
            except Exception as perm_err:
                return _error_response(
                    "permission_denied",
                    str(perm_err),
                    help="You need owner access for summary/usage_guide/resource_id/is_public, or editor access for display_name/description.",
                )

            # Apply updates
            if "display_name" in args:
                context.display_name = args["display_name"]
            if "description" in args:
                context.description = args["description"]
            if "summary" in args:
                context.summary = args["summary"]
            if "usage_guide" in args:
                context.usage_guide = args["usage_guide"]
            if "is_public" in args:
                is_public = args["is_public"]
                if is_public and not context.is_public:
                    # Making public: check plan allows it
                    from config.plan_tiers import get_plan_tier
                    from models.auth import Workspace

                    ws = await db.get(Workspace, context.workspace_id)
                    if ws:
                        plan = get_plan_tier(ws.plan_name)
                        if not plan.allows_shared_contexts:
                            return _error_response(
                                "plan_required",
                                "Public contexts require a higher tier plan.",
                            )
                if not is_public and context.is_public and context.resource_id:
                    return _error_response(
                        "cannot_make_private",
                        "Cannot make private: context has a resource_id. Revoke tokens and remove resource_id first.",
                    )
                context.is_public = is_public

            if "resource_id" in args:
                import re as _re

                rid = args["resource_id"]
                if not _re.match(r"^[a-z0-9_-]+$", rid) or len(rid) > 255:
                    return _error_response(
                        "invalid_resource_id",
                        "resource_id must be lowercase alphanumeric, underscores, and hyphens only (max 255 chars).",
                    )

                # Revoke old tokens if resource_id is changing
                old_rid = context.resource_id
                if old_rid and old_rid != rid:
                    from sqlalchemy import select as _select

                    from auth.resource_tokens import ResourceTokenManager
                    from models.resource import ResourceToken

                    token_mgr = ResourceTokenManager(db)
                    old_tokens = await db.execute(
                        _select(ResourceToken).where(
                            ResourceToken.resource_id == old_rid,
                            ResourceToken.created_by == user_id,
                            ResourceToken.is_active == True,  # noqa: E712
                        )
                    )
                    for token in old_tokens.scalars().all():
                        await token_mgr.revoke_token(token.id)

                context.resource_id = rid

            try:
                await db.commit()
            except Exception as commit_err:
                await db.rollback()
                if "unique_context_resource_id" in str(commit_err) or "resource_id" in str(
                    commit_err
                ):
                    return _error_response(
                        "resource_id_conflict",
                        f"Resource ID '{args.get('resource_id', '')}' is already used by another context in this workspace.",
                    )
                raise
            await db.refresh(context)

            await _log_tool_usage(
                db,
                user_id,
                "update_context",
                start_time,
                200,
                str(context.id),
                workspace_id,
            )

            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "status": "success",
                            "message": f"Context '{context.name}' updated successfully.",
                            "updated_fields": list(requested_fields),
                            **_context_response_fields(context),
                        }
                    ),
                )
            ]
        except ValueError:
            return _error_response(
                "invalid_context_id",
                f"Invalid context_id format: {args['context_id']}",
                help="Context ID must be a valid UUID.",
            )
        except Exception as e:
            await db.rollback()
            logger.error(f"update_context_failed: {e}", exc_info=True)
            return _error_response("update_context_error", str(e))

    # Safety: should never reach here (get_db always yields)
    return _error_response("internal_error", "Database session unavailable")


async def handle_list_contexts(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """List all contexts accessible to the user."""
    include_stats = args.get("include_stats", False)

    from db.base import get_db

    start_time = time.time()
    async for db in get_db():
        try:
            from services.context_service import ContextService

            context_service = ContextService(db)
            contexts = await execute_with_timeout(
                context_service.list_contexts(user_id),
                operation_name="list_contexts",
            )

            from datetime import datetime

            contexts_sorted = sorted(
                contexts,
                key=lambda c: c.last_used_at or datetime.min,
                reverse=True,
            )

            # Batch-fetch embedding configs to avoid N+1
            from sqlalchemy import select as _select

            from config.settings import get_settings as _get_settings2
            from models.config import ContextSearchConfig

            _settings2 = _get_settings2()

            context_ids = [ctx.id for ctx in contexts_sorted]
            config_results = await db.execute(
                _select(ContextSearchConfig).where(ContextSearchConfig.context_id.in_(context_ids))
            )
            config_by_ctx = {c.context_id: c for c in config_results.scalars().all()}

            context_list = []
            for ctx in contexts_sorted:
                cfg = config_by_ctx.get(ctx.id)
                ctx_data: dict[str, Any] = {
                    "id": str(ctx.id),
                    "name": ctx.name,
                    "summary": ctx.summary,
                    "is_private": ctx.is_private,
                    "last_used_at": ctx.last_used_at.isoformat() if ctx.last_used_at else None,
                    "embedding_model": cfg.embedding_model if cfg else _settings2.embedding_model,
                }
                if include_stats:
                    try:
                        stats = await context_service.get_context_stats(user_id, ctx.id)
                        ctx_data["memory_count"] = stats.get("memory_count", 0)
                    except Exception:
                        ctx_data["memory_count"] = 0
                context_list.append(ctx_data)

            # Get context quota (workspace-wide count, not just user-visible)
            quota_info: dict[str, Any] = {"count": len(context_list)}
            if workspace_id:
                try:
                    from sqlalchemy import func as sql_func
                    from sqlalchemy import select as sql_select

                    from models.auth import Context as ContextModel
                    from services.effective_quota_service import EffectiveQuotaService

                    # Count all non-deleted contexts in workspace
                    ws_count_result = await db.execute(
                        sql_select(sql_func.count())
                        .select_from(ContextModel)
                        .where(
                            ContextModel.workspace_id == workspace_id,
                            ContextModel.deleted_at.is_(None),
                        )
                    )
                    ws_context_count = ws_count_result.scalar_one() or 0

                    effective = await EffectiveQuotaService(db).get_effective_quotas(workspace_id)
                    max_contexts = effective.get("max_contexts", 0)
                    quota_info["count"] = ws_context_count
                    quota_info["limit"] = max_contexts
                    quota_info["can_create"] = ws_context_count < max_contexts
                except Exception as e:
                    logger.warning("list_contexts_quota_failed: %s", str(e))
                    quota_info["limit"] = 0
                    quota_info["can_create"] = False

            await _log_tool_usage(db, user_id, "list_contexts", start_time, 200, None, workspace_id)

            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "status": "success",
                            "contexts": context_list,
                            **quota_info,
                        }
                    ),
                )
            ]
        except Exception as e:
            await db.rollback()
            logger.error(f"list_contexts_failed: {e}", exc_info=True)
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"status": "error", "error": str(e)}),
                )
            ]

    # Safety: should never reach here (get_db always yields)
    return _error_response("internal_error", "Database session unavailable")


async def handle_delete_context(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Soft-delete a context and its memories."""
    if "context_id" not in args:
        return _error_response("missing_fields", "Missing required field: context_id")

    try:
        context_id = _resolve_context_id(args["context_id"])
    except ValueError:
        return _error_response("invalid_context_id", f"Invalid UUID: {args['context_id']}")

    from db.base import get_db
    from services.context_service import ContextService
    from services.permission_service import PermissionService
    from utils.exceptions import AuthorizationError, NotFoundException, ValidationError

    start_time = time.time()

    async for db in get_db():
        try:
            # Owner-only: verify before delete
            perm_service = PermissionService(db)
            await perm_service.check_context_owner(user_id, context_id)

            service = ContextService(db)
            context = await service.delete_context(user_id, context_id)

            await _log_tool_usage(
                db,
                user_id,
                "delete_context",
                start_time,
                200,
                str(context_id),
                workspace_id,
            )

            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "status": "success",
                            "message": f"Context '{context.name}' has been soft-deleted.",
                            "context_id": str(context_id),
                            "context_name": context.name,
                        }
                    ),
                )
            ]
        except (NotFoundException, _ContextNotFoundError):
            await db.rollback()
            return _error_response(
                "context_not_found",
                f"Context {context_id} not found or access denied.",
            )
        except (AuthorizationError, Exception) as e:
            await db.rollback()
            if isinstance(e, (AuthorizationError, ValidationError)):
                return _error_response("permission_denied", str(e))
            await _log_tool_usage(
                db,
                user_id,
                "delete_context",
                start_time,
                500,
                args.get("context_id"),
                workspace_id,
            )
            logger.error("delete_context_failed", exc_info=True)
            return _error_response("delete_context_error", str(e))

    return _error_response("internal_error", "Database session unavailable")
