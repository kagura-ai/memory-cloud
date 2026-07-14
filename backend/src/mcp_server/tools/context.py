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
    _resolve_context_for_read,
    _resolve_context_id,
    execute_with_timeout,
)

logger = logging.getLogger(__name__)


async def handle_get_context_info(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Retrieve context information and memory statistics.

    Visibility: shared context → stats aggregate across all workspace members;
    private context → creator-only. Non-creators get uniform ``context_not_found``.
    The workspace returned by the resolver is authoritative; the MCP-session
    ``workspace_id`` is informational only.
    """
    include_details = args.get("include_details", True)

    from db.base import get_db

    start_time = time.time()
    async for db in get_db():
        try:
            from services.memory_service import MemoryService

            current_context_id = _resolve_context_id(args["context_id"])

            current_context = None
            is_shared = False
            effective_workspace_id = workspace_id

            if current_context_id:
                current_context = await _resolve_context_for_read(db, user_id, current_context_id)
                # Past this gate the caller is either the private-context creator
                # or an authorized shared-context reader, so is_private alone
                # picks the correct scope for MemoryService.get_stats.
                is_shared = not current_context.is_private
                effective_workspace_id = current_context.workspace_id

            workspace = None
            if effective_workspace_id:
                from sqlalchemy import select

                from models.auth import Workspace

                workspace_result = await db.execute(
                    select(Workspace).where(Workspace.id == effective_workspace_id)
                )
                workspace = workspace_result.scalar_one_or_none()

            service = MemoryService(db)
            result = await execute_with_timeout(
                service.get_stats(
                    user_id=user_id,
                    workspace_id=str(effective_workspace_id) if effective_workspace_id else None,
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
                    "is_locked": current_context.is_locked,
                    "embedding_model": search_config.embedding_model
                    if search_config
                    else _settings.embedding_model,
                    "embedding_dimensions": search_config.embedding_dimensions
                    if search_config
                    else _settings.embedding_dimensions,
                    "search_config": {
                        "semantic_weight": float(search_config.semantic_weight)
                        if search_config
                        else 0.60,
                        "bm25_weight": float(search_config.bm25_weight) if search_config else 0.40,
                        "fetch_factor": search_config.fetch_factor if search_config else 3,
                        "use_rerank": search_config.use_rerank if search_config else False,
                        "reranker_provider": (
                            search_config.reranker_provider if search_config else None
                        )
                        or "voyage",
                        "reranker_model": (search_config.reranker_model if search_config else None)
                        or "rerank-2",
                    },
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
        except _ContextNotFoundError as e:
            await db.rollback()
            await _log_tool_usage(
                db,
                user_id,
                "get_context_info",
                start_time,
                404,
                args.get("context_id"),
                workspace_id,
            )
            return e.to_response()
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


def _validate_context_field_lengths(args: dict[str, Any]) -> list[TextContent] | None:
    """Enforce the shared context metadata caps on the MCP write path (#1193).

    The REST surface has always enforced these via Pydantic; the MCP tools
    only DOCUMENTED them, so connector-written values could exceed the REST
    cap — after which every save from the web settings UI 422'd on a field
    the user never touched. One source of truth: config.constants.
    """
    from config.constants import (
        CONTEXT_DESCRIPTION_MAX_LENGTH,
        CONTEXT_SUMMARY_MAX_LENGTH,
        CONTEXT_USAGE_GUIDE_MAX_LENGTH,
    )

    limits = {
        "description": CONTEXT_DESCRIPTION_MAX_LENGTH,
        "summary": CONTEXT_SUMMARY_MAX_LENGTH,
        "usage_guide": CONTEXT_USAGE_GUIDE_MAX_LENGTH,
    }
    for field_name, cap in limits.items():
        value = args.get(field_name)
        if isinstance(value, str) and len(value) > cap:
            return _error_response(
                "field_too_long",
                f"{field_name} exceeds the {cap}-character limit (got {len(value)}).",
                help=f"Shorten {field_name} to at most {cap} characters.",
            )
    return None


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

    length_error = _validate_context_field_lengths(args)
    if length_error:
        return length_error

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

    length_error = _validate_context_field_lengths(args)
    if length_error:
        return length_error

    from db.base import get_db

    start_time = time.time()
    async for db in get_db():
        try:
            from uuid import UUID as _UUID

            from services.permission_service import PermissionService
            from utils.exceptions import AuthorizationError, NotFoundException

            ctx_uuid = _UUID(args["context_id"])

            perm_service = PermissionService(db)
            owner_fields = {"summary", "usage_guide", "resource_id", "is_public", "is_locked"}
            requested_fields = {
                k
                for k in (
                    "summary",
                    "usage_guide",
                    "display_name",
                    "description",
                    "resource_id",
                    "is_public",
                    "is_locked",
                )
                if k in args
            }

            if not requested_fields:
                return _error_response(
                    "no_changes",
                    "No fields to update. Provide at least one of: summary, usage_guide, display_name, description, resource_id, is_public, is_locked.",
                )

            # Mirrors handle_delete_context. Uses exc.message (not str(exc))
            # so AuthorizationError's CWE-639 uniform-message contract holds.
            try:
                if requested_fields & owner_fields:
                    # Owner-only fields → require context owner
                    context = await perm_service.check_context_owner(user_id, ctx_uuid)
                else:
                    # display_name/description → require editor access
                    context, _ = await perm_service.check_context_access(
                        user_id, ctx_uuid, required_role="editor"
                    )
            except NotFoundException:
                await db.rollback()
                return _error_response(
                    "context_not_found",
                    f"Context {ctx_uuid} not found or access denied.",
                )
            except AuthorizationError as perm_err:
                await db.rollback()
                return _error_response(
                    "permission_denied",
                    perm_err.message,
                    help="You need owner access for summary/usage_guide/resource_id/is_public/is_locked, or editor access for display_name/description.",
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

            # Issue #85: Lock/unlock context
            if "is_locked" in args:
                context.is_locked = args["is_locked"]

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

            from datetime import UTC, datetime

            from utils.datetime import to_utc_iso

            # ``Context.last_used_at`` is declared as ``DateTime(timezone=True)``
            # (see models/auth.py) — populated values are aware. Use an aware
            # UTC sentinel so the ``None`` branch sorts consistently; mixing a
            # naive ``datetime.min`` with aware values raises ``TypeError`` at
            # comparison time.
            _UTC_MIN = datetime.min.replace(tzinfo=UTC)
            contexts_sorted = sorted(
                contexts,
                key=lambda c: c.last_used_at or _UTC_MIN,
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
                    "is_locked": ctx.is_locked,
                    # #1257 made this live data; Z-suffix per backend.md (raw
                    # .isoformat() rendered '+00:00', unlike every other
                    # timestamp on the MCP surface, e.g. tags[].last_used_at).
                    "last_used_at": to_utc_iso(ctx.last_used_at),
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


async def handle_merge_contexts(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Merge memories from source context into target context (Issue #90)."""
    # #990: params renamed source_id/target_id → source_context_id/
    # target_context_id (they are CONTEXT UUIDs; edge tools use source_id/
    # target_id for MEMORY UUIDs). Accept the old names for one release as a
    # deprecated alias so the current SDK (kagura-memory-python-sdk#196) keeps
    # working until it updates; the schema advertises only the new names.
    source_raw = args.get("source_context_id", args.get("source_id"))
    target_raw = args.get("target_context_id", args.get("target_id"))
    if source_raw is None or target_raw is None:
        return _error_response(
            "missing_fields",
            "Missing required fields: source_context_id and target_context_id",
        )
    if "source_id" in args or "target_id" in args:
        logger.warning(
            "merge_contexts called with deprecated params source_id/target_id; "
            "use source_context_id/target_context_id (#990)"
        )

    from db.base import get_db

    start_time = time.time()
    async for db in get_db():
        try:
            from services.context_service import ContextService

            source_id = _resolve_context_id(source_raw)
            target_id = _resolve_context_id(target_raw)
            delete_source_raw = args.get("delete_source", False)
            if isinstance(delete_source_raw, str):
                delete_source = delete_source_raw.lower() == "true"
            else:
                delete_source = bool(delete_source_raw)

            # Workspace-boundary guard at the MCP boundary (Issue #966), mirror
            # of handle_recall's cross-context guard. Resolve both contexts via
            # _resolve_context_for_read so an unreadable context (not found /
            # private non-creator / non-member) surfaces a uniform
            # context_not_found (CWE-639) instead of leaking through the generic
            # merge_contexts_error path, and reject a cross-workspace merge
            # before ContextService.merge_contexts is invoked. The service still
            # enforces owner-only access as the authorization gate.
            source_ctx = await _resolve_context_for_read(db, user_id, source_id)
            target_ctx = await _resolve_context_for_read(db, user_id, target_id)
            if source_ctx.workspace_id != target_ctx.workspace_id:
                return _error_response(
                    "workspace_mismatch",
                    "Source and target contexts must belong to the same workspace.",
                )

            service = ContextService(db)
            result = await execute_with_timeout(
                service.merge_contexts(
                    user_id=user_id,
                    source_context_id=source_id,
                    target_context_id=target_id,
                    delete_source=delete_source,
                ),
                operation_name="merge_contexts",
            )

            await _log_tool_usage(
                db,
                user_id,
                "merge_contexts",
                start_time,
                200,
                str(source_id),
                workspace_id,
            )
            await db.commit()

            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "status": "success",
                            "message": f"Merged {result['merged']} memories from source to target.",
                            **result,
                            "delete_source": delete_source,
                        }
                    ),
                )
            ]
        except _ContextNotFoundError as e:
            await db.rollback()
            return e.to_response()
        except Exception as e:
            await db.rollback()
            await _log_tool_usage(
                db,
                user_id,
                "merge_contexts",
                start_time,
                500,
                # source_raw (the raw input) is always bound after the
                # missing-fields check and is correct for either param name
                # (#990); args.get("source_id") would be None for the new
                # canonical source_context_id calls, losing the audit trail.
                source_raw,
                workspace_id,
            )
            logger.error("merge_contexts_failed", exc_info=True)
            return _error_response("merge_contexts_error", str(e))

    return _error_response("internal_error", "Database session unavailable")


async def handle_list_tags(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """List tag vocabulary in a context (Issue #614).

    Read-only tag-discovery tool that helps callers reuse existing tag
    spellings before remember()/recall(), mitigating tag drift. Access is
    gated inside ``ContextService.aggregate_tags`` via ``get_context``,
    which raises ``NotFoundException`` for not-found / private-non-creator /
    non-member / whitelist-miss — all caught here and reshaped into the
    uniform ``context_not_found`` MCP error (CWE-639).
    """
    from db.base import get_db
    from utils.datetime import to_utc_iso
    from utils.exceptions import NotFoundException, ValidationError

    start_time = time.time()

    if "context_id" not in args:
        return _error_response(
            "context_id_required",
            "list_tags requires context_id.",
            help="Use list_contexts() to discover available context IDs.",
        )
    try:
        context_id = _resolve_context_id(args["context_id"])
    except ValueError as e:
        return _error_response("invalid_context_id_format", str(e))

    # ``type(x) is T`` (not ``isinstance``) so bool — which subclasses int — is
    # rejected here. Service validates the same bounds defensively; the handler
    # check stays so the MCP error code is ``invalid_argument`` rather than
    # the generic 422-equivalent path. ``sort`` is service-validated.
    limit = args.get("limit", 50)
    if type(limit) is not int or limit < 1 or limit > 500:
        return _error_response(
            "invalid_argument",
            "limit must be an integer between 1 and 500.",
            received=limit,
        )

    min_count = args.get("min_count", 1)
    if type(min_count) is not int or min_count < 1 or min_count > 10_000:
        return _error_response(
            "invalid_argument",
            "min_count must be an integer between 1 and 10000.",
            received=min_count,
        )

    # Treat absent / None as empty; reject any non-string up-front (without the
    # ``or ""`` shorthand, which would silently coerce 0 / False to "").
    prefix = args.get("prefix")
    if prefix is None:
        prefix = ""
    if type(prefix) is not str or len(prefix) > 200:
        return _error_response(
            "invalid_argument",
            "prefix must be a string up to 200 characters.",
        )

    sort = args.get("sort", "count")

    async for db in get_db():
        try:
            from services.context_service import ContextService

            service = ContextService(db)
            result = await execute_with_timeout(
                service.aggregate_tags(
                    user_id,
                    context_id,
                    limit=limit,
                    min_count=min_count,
                    sort=sort,
                    prefix=prefix,
                ),
                operation_name="list_tags",
            )

            tags_payload = [
                {
                    "tag": row["tag"],
                    "count": row["count"],
                    "last_used_at": to_utc_iso(row["last_used_at"]),
                }
                for row in result["rows"]
            ]

            await _log_tool_usage(
                db,
                user_id,
                "list_tags",
                start_time,
                200,
                context_id,
                workspace_id,
            )

            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "status": "success",
                            "context_id": str(context_id),
                            "context_name": result["context_name"],
                            "tags": tags_payload,
                            "total": len(tags_payload),
                        }
                    ),
                )
            ]
        except NotFoundException:
            # CWE-639: never leak the deny reason. Surface shape matches
            # `_ContextNotFoundError.to_response()` used by sibling handlers.
            await db.rollback()
            await _log_tool_usage(
                db,
                user_id,
                "list_tags",
                start_time,
                404,
                context_id,
                workspace_id,
            )
            return _error_response(
                "context_not_found",
                "Context not found or you don't have access to it.",
                context_id=str(context_id),
                help="Use list_contexts() to see contexts you have access to.",
            )
        except ValidationError as e:
            await db.rollback()
            await _log_tool_usage(
                db,
                user_id,
                "list_tags",
                start_time,
                422,
                context_id,
                workspace_id,
            )
            return _error_response("invalid_argument", str(e))
        except Exception as e:
            await db.rollback()
            await _log_tool_usage(
                db,
                user_id,
                "list_tags",
                start_time,
                500,
                context_id,
                workspace_id,
            )
            logger.error(f"list_tags_failed: {e}", exc_info=True)
            return _error_response("list_tags_error", str(e))

    return _error_response("internal_error", "Database session unavailable")
