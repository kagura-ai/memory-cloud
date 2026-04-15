"""MCP tool handlers: resource management operations.

Issue #46: Add resource management tools (setup, ingest, stats, schema).

Provides 5 tools for managing resources via MCP:
- setup_resource: Create public context + resource token in one call
- ingest_events: Batch ingest resource events
- get_resource_impact: Get resource stats (tokens, memories, schema version)
- get_resource_schema: Get field definitions for a resource
- list_resource_tokens: List tokens for a resource
"""

import json
import logging
import re
import time
from typing import Any
from uuid import UUID

from mcp.types import TextContent

from mcp_server.tools._helpers import (
    _check_viewer_permission,
    _error_response,
    _get_workspace_member_role,
    _log_tool_usage,
    _success_response,
)
from services.resource_quota_service import (
    check_event_quota,
    resolve_workspace_event_quota_per_hour,
)
from utils.exceptions import RateLimitError

logger = logging.getLogger(__name__)

# Resource ID format: lowercase alphanumeric + underscore + hyphen
_RESOURCE_ID_PATTERN = re.compile(r"^[a-z0-9_-]+$")

# Max payload size per event (100KB)
_MAX_PAYLOAD_SIZE_BYTES = 100_000

# Max events per batch
_MAX_BATCH_SIZE = 100


# ============================================================================
# Validation helpers
# ============================================================================


def _validate_resource_id(resource_id: str) -> list[TextContent] | None:
    """Validate resource_id format. Returns error response or None."""
    if not resource_id or len(resource_id) > 255:
        return _error_response(
            "validation_error",
            "resource_id must be 1-255 characters.",
        )
    if not _RESOURCE_ID_PATTERN.match(resource_id):
        return _error_response(
            "validation_error",
            f"Invalid resource_id format: '{resource_id}'. "
            "Must be lowercase alphanumeric, underscores, and hyphens only.",
        )
    return None


async def _check_owner_admin_role(
    db: Any, user_id: str, workspace_id: UUID
) -> list[TextContent] | None:
    """Check user is owner or admin. Returns error response or None."""
    role = await _get_workspace_member_role(db, user_id, workspace_id)
    if role not in ("owner", "admin"):
        return _error_response(
            "permission_denied",
            "Only workspace owners and admins can perform this operation.",
            your_role=role or "not_a_member",
            required_role="owner or admin",
        )
    return None


async def _check_resource_workspace_boundary(
    db: Any, resource_id: str, workspace_id: UUID
) -> list[TextContent] | None:
    """Verify resource_id belongs to workspace. Returns error response or None."""
    from sqlalchemy import select

    from models.auth import Context

    result = await db.execute(
        select(Context.id).where(
            Context.resource_id == resource_id,
            Context.workspace_id == workspace_id,
            Context.deleted_at.is_(None),
        )
    )
    if not result.scalar_one_or_none():
        return _error_response(
            "resource_not_found",
            f"Resource '{resource_id}' not found in your workspace.",
            help="Use setup_resource() to create a new resource, or check resource_id spelling.",
        )
    return None


# ============================================================================
# Read-only handlers
# ============================================================================


async def handle_get_resource_impact(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Get resource change impact information."""
    if "resource_id" not in args:
        return _error_response("missing_fields", "Missing required field: resource_id")

    resource_id = args["resource_id"]
    err = _validate_resource_id(resource_id)
    if err:
        return err

    if not workspace_id:
        return _error_response("workspace_required", "No active workspace.")

    from db.base import get_db

    start_time = time.time()
    async for db in get_db():
        try:
            # Workspace boundary check
            boundary_err = await _check_resource_workspace_boundary(db, resource_id, workspace_id)
            if boundary_err:
                return boundary_err

            # Combined stats query — all subqueries workspace-scoped to prevent
            # cross-workspace leakage when resource_id collides across workspaces
            # (resource_id is unique per workspace, not globally).
            from sqlalchemy import func, select

            from models.auth import Context
            from models.memory import Memory
            from models.resource import ResourceSchema, ResourceToken

            token_count_subq = (
                select(func.count(ResourceToken.id))
                .join(Context, Context.resource_id == ResourceToken.resource_id)
                .where(
                    ResourceToken.resource_id == resource_id,
                    ResourceToken.is_active == True,  # noqa: E712
                    Context.workspace_id == workspace_id,
                    Context.deleted_at.is_(None),
                )
                .scalar_subquery()
            )
            memory_count_subq = (
                select(func.count(Memory.id))
                .where(
                    Memory.resource_id == resource_id,
                    Memory.deleted_at.is_(None),
                    Memory.workspace_id == workspace_id,
                )
                .scalar_subquery()
            )
            schema_version_subq = (
                select(func.max(ResourceSchema.schema_version))
                .join(Context, Context.resource_id == ResourceSchema.resource_id)
                .where(
                    ResourceSchema.resource_id == resource_id,
                    Context.workspace_id == workspace_id,
                    Context.deleted_at.is_(None),
                )
                .scalar_subquery()
            )

            result = await db.execute(
                select(
                    token_count_subq.label("token_count"),
                    memory_count_subq.label("memory_count"),
                    schema_version_subq.label("schema_version"),
                )
            )
            row = result.one()

            await _log_tool_usage(
                db,
                user_id,
                "get_resource_impact",
                start_time,
                200,
                workspace_id=workspace_id,
            )

            return _success_response(
                resource_id=resource_id,
                token_count=row.token_count or 0,
                memory_count=row.memory_count or 0,
                current_schema_version=row.schema_version,
            )

        except Exception as e:
            await db.rollback()
            logger.error(f"get_resource_impact_failed: {e}", exc_info=True)
            await _log_tool_usage(
                db,
                user_id,
                "get_resource_impact",
                start_time,
                500,
                workspace_id=workspace_id,
            )
            return _error_response("get_resource_impact_error", str(e))

    return _error_response("internal_error", "Database session unavailable")


async def handle_get_resource_schema(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Get field schema definition for a resource."""
    if "resource_id" not in args:
        return _error_response("missing_fields", "Missing required field: resource_id")

    resource_id = args["resource_id"]
    err = _validate_resource_id(resource_id)
    if err:
        return err

    if not workspace_id:
        return _error_response("workspace_required", "No active workspace.")

    from db.base import get_db

    start_time = time.time()
    async for db in get_db():
        try:
            # Workspace boundary check
            boundary_err = await _check_resource_workspace_boundary(db, resource_id, workspace_id)
            if boundary_err:
                return boundary_err

            from sqlalchemy import select

            from models.auth import Context
            from models.resource import ResourceSchema

            # Workspace-scope the schema lookup (resource_id is unique per workspace,
            # not globally — join via Context to avoid cross-workspace leakage).
            query = (
                select(ResourceSchema)
                .join(Context, Context.resource_id == ResourceSchema.resource_id)
                .where(
                    ResourceSchema.resource_id == resource_id,
                    Context.workspace_id == workspace_id,
                    Context.deleted_at.is_(None),
                )
            )

            schema_version = args.get("schema_version")
            if schema_version is not None:
                try:
                    schema_version = int(schema_version)
                except (ValueError, TypeError):
                    return _error_response("validation_error", "schema_version must be an integer.")
                if schema_version < 1:
                    return _error_response("validation_error", "schema_version must be >= 1.")
                query = query.where(ResourceSchema.schema_version == schema_version)
            else:
                query = query.order_by(ResourceSchema.schema_version.desc())

            query = query.limit(1)

            result = await db.execute(query)
            schema = result.scalar_one_or_none()

            if not schema:
                return _error_response(
                    "schema_not_found",
                    f"No schema found for resource '{resource_id}'."
                    + (f" (version {schema_version})" if schema_version is not None else ""),
                    help="Use the REST API to create a schema first.",
                )

            await _log_tool_usage(
                db,
                user_id,
                "get_resource_schema",
                start_time,
                200,
                workspace_id=workspace_id,
            )

            return _success_response(
                resource_id=schema.resource_id,
                schema_version=schema.schema_version,
                field_definitions=schema.field_definitions,
                created_at=schema.created_at.isoformat(),
            )

        except Exception as e:
            await db.rollback()
            logger.error(f"get_resource_schema_failed: {e}", exc_info=True)
            await _log_tool_usage(
                db,
                user_id,
                "get_resource_schema",
                start_time,
                500,
                workspace_id=workspace_id,
            )
            return _error_response("get_resource_schema_error", str(e))

    return _error_response("internal_error", "Database session unavailable")


async def handle_list_resource_tokens(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """List resource tokens for a workspace."""
    if not workspace_id:
        return _error_response("workspace_required", "No active workspace.")

    from db.base import get_db

    start_time = time.time()
    async for db in get_db():
        try:
            # Owner/admin only
            role_err = await _check_owner_admin_role(db, user_id, workspace_id)
            if role_err:
                return role_err

            resource_id = args.get("resource_id")

            # Validate + workspace boundary check if filtering by resource_id
            if resource_id:
                format_err = _validate_resource_id(resource_id)
                if format_err:
                    return format_err
                boundary_err = await _check_resource_workspace_boundary(
                    db, resource_id, workspace_id
                )
                if boundary_err:
                    return boundary_err

            from sqlalchemy import and_, exists, func
            from sqlalchemy import select as sa_select

            from models.auth import Context
            from models.resource import ResourceToken

            include_revoked = args.get("include_revoked", True)
            try:
                limit = min(max(int(args.get("limit", 50)), 1), 100)
                offset = max(int(args.get("offset", 0)), 0)
            except (ValueError, TypeError):
                return _error_response("validation_error", "limit and offset must be integers.")

            # Workspace-scoped token query using EXISTS subquery
            workspace_filter = exists().where(
                Context.resource_id == ResourceToken.resource_id,
                Context.workspace_id == workspace_id,
                Context.deleted_at.is_(None),
            )

            conditions = [workspace_filter]
            if resource_id:
                conditions.append(ResourceToken.resource_id == resource_id)
            if not include_revoked:
                conditions.append(ResourceToken.is_active == True)  # noqa: E712

            total_result = await db.execute(
                sa_select(func.count(ResourceToken.id)).where(and_(*conditions))
            )
            total = total_result.scalar() or 0

            tokens_result = await db.execute(
                sa_select(ResourceToken)
                .where(and_(*conditions))
                .order_by(ResourceToken.created_at.desc())
                .offset(offset)
                .limit(limit)
            )
            tokens = list(tokens_result.scalars().all())

            token_list = [
                {
                    "id": t.id,
                    "resource_id": t.resource_id,
                    "description": t.description,
                    "quota_events_per_hour": t.quota_events_per_hour,
                    "is_active": t.is_active,
                    "created_at": t.created_at.isoformat() if t.created_at else None,
                    "last_used_at": t.last_used_at.isoformat() if t.last_used_at else None,
                }
                for t in tokens
            ]

            await _log_tool_usage(
                db,
                user_id,
                "list_resource_tokens",
                start_time,
                200,
                workspace_id=workspace_id,
            )

            return _success_response(
                tokens=token_list,
                total=total,
                limit=limit,
                offset=offset,
            )

        except Exception as e:
            await db.rollback()
            logger.error(f"list_resource_tokens_failed: {e}", exc_info=True)
            await _log_tool_usage(
                db,
                user_id,
                "list_resource_tokens",
                start_time,
                500,
                workspace_id=workspace_id,
            )
            return _error_response("list_resource_tokens_error", str(e))

    return _error_response("internal_error", "Database session unavailable")


# ============================================================================
# Write handlers
# ============================================================================


async def handle_ingest_events(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Batch ingest resource events."""
    if "resource_id" not in args or "events" not in args:
        return _error_response("missing_fields", "Missing required fields: resource_id, events")

    resource_id = args["resource_id"]
    err = _validate_resource_id(resource_id)
    if err:
        return err

    events = args["events"]
    if not isinstance(events, list) or len(events) == 0:
        return _error_response("validation_error", "events must be a non-empty list.")
    if len(events) > _MAX_BATCH_SIZE:
        return _error_response(
            "validation_error",
            f"Batch size {len(events)} exceeds maximum of {_MAX_BATCH_SIZE}.",
        )

    if not workspace_id:
        return _error_response("workspace_required", "No active workspace.")

    from db.base import get_db

    start_time = time.time()
    async for db in get_db():
        try:
            # Viewer permission check
            viewer_err = await _check_viewer_permission(db, user_id, workspace_id, "ingest events")
            if viewer_err:
                return viewer_err

            # Workspace boundary check
            boundary_err = await _check_resource_workspace_boundary(db, resource_id, workspace_id)
            if boundary_err:
                return boundary_err

            # MCP shares the per-hour ceiling with the HTTP ingest path via a
            # workspace-scoped Redis counter. The cap is derived from the most
            # permissive ResourceToken for this (workspace, resource) pair, with
            # a default fallback when no token exists.
            quota_per_hour = await resolve_workspace_event_quota_per_hour(
                db, workspace_id, resource_id
            )
            try:
                await check_event_quota(
                    resource_id, workspace_id, quota_per_hour, count=len(events)
                )
            except RateLimitError as quota_err:
                return _error_response(
                    "quota_exceeded",
                    quota_err.message,
                    retry_after_seconds=quota_err.retry_after,
                )

            # Process events
            from sqlalchemy.exc import IntegrityError

            from db.constraint_names import (
                RESOURCE_EVENTS_IDEMPOTENCY_UNIQUE,
                RESOURCE_EVENTS_UPSERT_UNIQUE,
                integrity_error_constraint_name,
            )
            from models.resource import ResourceEvent

            created_ids: list[int] = []
            errors: list[dict] = []

            for i, event_data in enumerate(events):
                if not isinstance(event_data, dict):
                    errors.append({"index": i, "error": "event must be an object"})
                    continue
                op = event_data.get("op")
                doc_id = event_data.get("doc_id")

                # Basic validation
                if op not in ("upsert", "delete"):
                    errors.append({"index": i, "error": f"Invalid op: {op}"})
                    continue
                if not doc_id:
                    errors.append({"index": i, "error": "Missing doc_id"})
                    continue

                payload = event_data.get("payload")
                version = None

                # Upsert requires payload and version >= 1
                if op == "upsert":
                    if not payload:
                        errors.append({"index": i, "error": "payload required for upsert"})
                        continue
                    version = event_data.get("version")
                    try:
                        version = int(version) if version is not None else None
                    except (ValueError, TypeError):
                        errors.append({"index": i, "error": "version must be an integer"})
                        continue
                    if version is None or version < 1:
                        errors.append({"index": i, "error": "version >= 1 required for upsert"})
                        continue

                # Payload size check
                if payload:
                    payload_size = len(json.dumps(payload).encode("utf-8"))
                    if payload_size > _MAX_PAYLOAD_SIZE_BYTES:
                        errors.append(
                            {
                                "index": i,
                                "error": f"Payload too large: {payload_size} bytes "
                                f"(max {_MAX_PAYLOAD_SIZE_BYTES})",
                            }
                        )
                        continue

                # Validate importance range
                importance = event_data.get("importance", 0.6)
                try:
                    importance = float(importance)
                except (ValueError, TypeError):
                    errors.append({"index": i, "error": "importance must be a number"})
                    continue
                if importance < 0.0 or importance > 1.0:
                    errors.append({"index": i, "error": "importance must be between 0.0 and 1.0"})
                    continue

                # Delete validation: payload must be null, version (if provided) must be >= 1
                if op == "delete":
                    if payload is not None:
                        errors.append({"index": i, "error": "payload must be null for delete"})
                        continue
                    version = event_data.get("version")
                    if version is not None:
                        try:
                            version = int(version)
                        except (ValueError, TypeError):
                            errors.append({"index": i, "error": "version must be an integer"})
                            continue
                        if version < 1:
                            errors.append({"index": i, "error": "version must be >= 1"})
                            continue

                event = ResourceEvent(
                    resource_id=resource_id,
                    op=op,
                    doc_id=doc_id,
                    version=version,
                    payload=payload if op == "upsert" else None,
                    idempotency_key=event_data.get("idempotency_key"),
                    event_metadata=event_data.get("event_metadata", {}),
                    importance=importance,
                )

                try:
                    async with db.begin_nested():
                        db.add(event)
                        await db.flush()
                    created_ids.append(event.id)
                except IntegrityError as ie:
                    constraint = integrity_error_constraint_name(ie)
                    if constraint == RESOURCE_EVENTS_UPSERT_UNIQUE:
                        errors.append(
                            {
                                "index": i,
                                "error": f"Duplicate version for doc_id={doc_id}",
                            }
                        )
                    elif constraint == RESOURCE_EVENTS_IDEMPOTENCY_UNIQUE:
                        errors.append(
                            {
                                "index": i,
                                "error": "Duplicate idempotency_key",
                            }
                        )
                    else:
                        # Do not leak raw DB constraint details to clients;
                        # log the constraint name for triage instead.
                        logger.warning(
                            "ingest_events integrity error: resource_id=%s index=%s doc_id=%s constraint=%s",
                            resource_id,
                            i,
                            doc_id,
                            constraint,
                            exc_info=ie,
                        )
                        errors.append(
                            {
                                "index": i,
                                "error": "Unable to ingest event due to a constraint violation",
                            }
                        )
                    continue

            # Schedule indexer if any events were created
            if created_ids:
                from api.routes.resource_ingest import _schedule_indexer_for_resource

                await _schedule_indexer_for_resource(db, resource_id)
                await db.commit()

            await _log_tool_usage(
                db,
                user_id,
                "ingest_events",
                start_time,
                200,
                workspace_id=workspace_id,
            )

            return _success_response(
                resource_id=resource_id,
                created_count=len(created_ids),
                failed_count=len(errors),
                event_ids=created_ids,
                errors=errors,
            )

        except Exception as e:
            await db.rollback()
            logger.error(f"ingest_events_failed: {e}", exc_info=True)
            await _log_tool_usage(
                db,
                user_id,
                "ingest_events",
                start_time,
                500,
                workspace_id=workspace_id,
            )
            return _error_response("ingest_events_error", str(e))

    return _error_response("internal_error", "Database session unavailable")


async def handle_setup_resource(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Create public context + resource token in one atomic operation."""
    if "name" not in args or "resource_id" not in args:
        return _error_response("missing_fields", "Missing required fields: name, resource_id")

    resource_id = args["resource_id"]
    err = _validate_resource_id(resource_id)
    if err:
        return err

    name = args["name"]
    if not workspace_id:
        return _error_response("workspace_required", "No active workspace.")

    from db.base import get_db

    start_time = time.time()
    async for db in get_db():
        try:
            # 1. Role check: owner/admin only
            role_err = await _check_owner_admin_role(db, user_id, workspace_id)
            if role_err:
                return role_err

            # 2. Validate context name
            from services.context_service import ContextService
            from utils.exceptions import ValidationError

            try:
                ContextService.validate_context_name(name)
            except ValidationError as ve:
                return _error_response("validation_error", str(ve))

            # 3. Check context name doesn't already exist
            context_service = ContextService(db)
            existing = await context_service.get_context_by_name_for_workspace(workspace_id, name)
            if existing:
                return _error_response(
                    "validation_error",
                    f"Context '{name}' already exists in this workspace.",
                )

            # 4. Check resource_id not already bound to an existing context in this workspace.
            # DB constraint `unique_context_resource_id_per_workspace` also enforces this,
            # but an explicit pre-insert lookup returns a clean `resource_id_conflict`
            # instead of relying on exception-based control flow.
            from sqlalchemy import func, select

            from config.plan_tiers import get_plan_tier
            from models.auth import Context, Workspace

            resource_dup_result = await db.execute(
                select(Context.id).where(
                    Context.workspace_id == workspace_id,
                    Context.resource_id == resource_id,
                    Context.deleted_at.is_(None),
                )
            )
            if resource_dup_result.scalar_one_or_none():
                return _error_response(
                    "resource_id_conflict",
                    f"resource_id '{resource_id}' is already in use in this workspace.",
                    help="Choose a different resource_id, or update the existing context.",
                )

            ws_result = await db.execute(
                select(Workspace.plan_name).where(Workspace.id == workspace_id)
            )
            plan_name = ws_result.scalar_one_or_none()
            if not plan_name:
                return _error_response("workspace_not_found", "Workspace not found.")

            plan = get_plan_tier(plan_name)
            # setup_resource creates a shared + public context; both require Pro.
            # Basic has max_resource_tokens>0 but does NOT allow shared/public contexts.
            if not plan.allows_shared_contexts or plan.max_resource_tokens == 0:
                return _error_response(
                    "plan_required",
                    "setup_resource requires PRO plan (public/shared contexts + resource tokens).",
                )

            # 5. Check context creation quota
            from services.quota_service import QuotaService

            can_create, error_msg = await QuotaService(db).check_context_creation_allowed(
                workspace_id
            )
            if not can_create:
                return _error_response(
                    "quota_exceeded",
                    error_msg or "Context creation limit reached.",
                    help="Delete unused contexts or upgrade your plan.",
                )

            # 6. Determine embedding model
            from config.constants import EMBEDDING_MODEL_REGISTRY
            from config.settings import get_settings

            settings = get_settings()
            actual_embedding_model = settings.embedding_model
            if actual_embedding_model in EMBEDDING_MODEL_REGISTRY:
                actual_dimensions = EMBEDDING_MODEL_REGISTRY[actual_embedding_model][0]
            else:
                actual_dimensions = settings.embedding_dimensions

            # 7. Create context (direct ORM — ContextService.create_context commits internally)
            context = Context(
                workspace_id=workspace_id,
                name=name,
                display_name=args.get("display_name") or name,
                description=f"Resource context for {resource_id}",
                created_by=user_id,
                is_private=False,
                is_public=True,
                resource_id=resource_id,
            )
            db.add(context)
            await db.flush()
            await db.refresh(context)

            # 8. Create search config
            from models.config import ContextSearchConfig

            search_config = ContextSearchConfig(
                context_id=context.id,
                semantic_weight=0.6,
                fetch_factor=3,
                use_rerank=False,
                reranker_provider="voyage",
                reranker_model="rerank-2-lite",
                embedding_model=actual_embedding_model,
                embedding_dimensions=actual_dimensions,
            )
            db.add(search_config)
            await db.flush()

            # 9. Qdrant collection is created lazily on first remember() call
            # via MemoryService — no need to create it here.

            # 10. Check active token count against plan limit (workspace-scoped)
            from models.resource import ResourceToken

            active_count_result = await db.execute(
                select(func.count(ResourceToken.id))
                .join(Context, Context.resource_id == ResourceToken.resource_id)
                .where(
                    Context.workspace_id == workspace_id,
                    Context.deleted_at.is_(None),
                    ResourceToken.is_active == True,  # noqa: E712
                )
            )
            active_count = active_count_result.scalar() or 0
            if active_count >= plan.max_resource_tokens:
                await db.rollback()
                await _log_tool_usage(
                    db,
                    user_id,
                    "setup_resource",
                    start_time,
                    403,
                    workspace_id=workspace_id,
                )
                return _error_response(
                    "quota_exceeded",
                    f"Token limit reached. Your {plan_name.upper()} plan allows "
                    f"{plan.max_resource_tokens} active tokens.",
                    help="Revoke unused tokens or upgrade your plan.",
                )

            # 11. Validate and create resource token
            try:
                quota_events_per_hour = int(args.get("quota_events_per_hour", 1000))
            except (ValueError, TypeError):
                return _error_response(
                    "validation_error", "quota_events_per_hour must be an integer."
                )
            if quota_events_per_hour < 1 or quota_events_per_hour > 10000:
                return _error_response(
                    "validation_error",
                    "quota_events_per_hour must be between 1 and 10000.",
                )

            from auth.resource_tokens import ResourceTokenManager

            manager = ResourceTokenManager(db)
            plaintext_token, token_record = await manager.create_token(
                resource_id=resource_id,
                description=args.get("description"),
                quota_events_per_hour=quota_events_per_hour,
                created_by=user_id,
            )

            # 12. Commit everything in one transaction
            await db.commit()
            await db.refresh(token_record)

            await _log_tool_usage(
                db,
                user_id,
                "setup_resource",
                start_time,
                200,
                str(context.id),
                workspace_id,
            )

            return _success_response(
                message=f"Resource '{resource_id}' set up successfully.",
                context_id=str(context.id),
                context_name=context.name,
                resource_id=resource_id,
                token=plaintext_token,
                token_id=token_record.id,
                warning="Save this token — it will not be shown again.",
            )

        except Exception as e:
            await db.rollback()
            error_str = str(e)

            # Map known constraint failures to sanitized responses
            from sqlalchemy.exc import IntegrityError as SQLIntegrityError

            if isinstance(e, SQLIntegrityError) or "already exists" in error_str:
                if "unique_context_resource_id" in error_str or "resource_id" in error_str:
                    sanitized_msg = f"resource_id '{resource_id}' is already in use."
                    error_code = "resource_id_conflict"
                elif "unique_context_name" in error_str or "already exists" in error_str:
                    sanitized_msg = f"Context name '{name}' already exists in this workspace."
                    error_code = "context_name_conflict"
                else:
                    sanitized_msg = "A uniqueness constraint was violated."
                    error_code = "conflict"
                await _log_tool_usage(
                    db,
                    user_id,
                    "setup_resource",
                    start_time,
                    409,
                    workspace_id=workspace_id,
                )
                return _error_response(
                    error_code,
                    sanitized_msg,
                    help="Check the context name and resource_id.",
                )
            logger.error(f"setup_resource_failed: {e}", exc_info=True)
            await _log_tool_usage(
                db,
                user_id,
                "setup_resource",
                start_time,
                500,
                workspace_id=workspace_id,
            )
            return _error_response("setup_resource_error", error_str)

    return _error_response("internal_error", "Database session unavailable")
