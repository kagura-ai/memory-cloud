"""MCP tool handlers: resource management operations.

Issue #46: Add resource management tools (setup, ingest, stats, schema).

Provides 5 tools for managing resources via MCP:
- setup_resource: Create public context + resource token in one call
- setup_connector: Create ai-worker connector + resource token in one call
- ingest_events: Batch ingest resource events
- get_resource_impact: Get resource stats (tokens, memories, schema version)
- get_resource_schema: Get field definitions for a resource
- list_resource_tokens: List tokens for a resource
"""

import logging
import re
import time
from typing import Any
from uuid import UUID

from mcp.types import TextContent

import services.resource_ingest_service as resource_ingest_service
from mcp_server.tools._helpers import (
    _check_viewer_permission,
    _error_response,
    _get_workspace_member_role,
    _log_tool_usage,
    _success_response,
)
from services.resource_ingest_service import IngestItemError
from services.resource_quota_service import (
    check_event_quota,
    resolve_workspace_event_quota_per_hour,
)
from utils.exceptions import RateLimitError

logger = logging.getLogger(__name__)

# Resource ID format: lowercase alphanumeric + underscore + hyphen
_RESOURCE_ID_PATTERN = re.compile(r"^[a-z0-9_-]+$")

# Max events per batch — single source in the shared ingest service
# (Issue #1255); the per-event payload-size cap lives there too
# (resource_ingest_service.MAX_PAYLOAD_SIZE_BYTES).
_MAX_BATCH_SIZE = resource_ingest_service.MAX_BATCH_SIZE


def _format_batch_item_error(err: IngestItemError) -> dict:
    """Render a structured batch item error in the historic MCP wire shape.

    Strings are byte-compatible with the pre-#1255 in-handler messages.
    Only the idempotency-validation error carries a ``doc_id`` field (the
    duplicate messages embed the doc_id in the string instead) — preserved
    from the original envelope.
    """
    svc = resource_ingest_service
    kind = err.kind
    doc_id_field = False
    if kind == svc.KIND_NOT_AN_OBJECT:
        message = "event must be an object"
    elif kind == svc.KIND_INVALID_OP:
        message = f"Invalid op: {err.detail.get('op')}"
    elif kind == svc.KIND_MISSING_DOC_ID:
        message = "Missing doc_id"
    elif kind == svc.KIND_PAYLOAD_REQUIRED:
        message = "payload required for upsert"
    elif kind == svc.KIND_VERSION_NOT_INT:
        message = "version must be an integer"
    elif kind == svc.KIND_VERSION_TOO_SMALL_UPSERT:
        message = "version >= 1 required for upsert"
    elif kind == svc.KIND_PAYLOAD_TOO_LARGE:
        message = f"Payload too large: {err.detail['payload_size']} bytes (max {err.detail['max']})"
    elif kind == svc.KIND_IMPORTANCE_NOT_NUMBER:
        message = "importance must be a number"
    elif kind == svc.KIND_IMPORTANCE_OUT_OF_RANGE:
        message = "importance must be between 0.0 and 1.0"
    elif kind == svc.KIND_PAYLOAD_NOT_NULL_DELETE:
        message = "payload must be null for delete"
    elif kind == svc.KIND_VERSION_TOO_SMALL:
        message = "version must be >= 1"
    elif kind == svc.KIND_IDEMPOTENCY_INVALID:
        message = err.detail["message"]
        doc_id_field = True
    elif kind == svc.KIND_DUPLICATE_VERSION:
        message = f"Duplicate version for doc_id={err.doc_id}"
    elif kind == svc.KIND_DUPLICATE_IDEMPOTENCY:
        message = "Duplicate idempotency_key"
    elif kind == svc.KIND_CONSTRAINT_VIOLATION:
        message = "Unable to ingest event due to a constraint violation"
    else:  # KIND_UNEXPECTED — previously failed the whole call; now per-item.
        message = "Unexpected error ingesting event"
    out: dict[str, Any] = {"index": err.index, "error": message}
    if doc_id_field:
        out["doc_id"] = err.doc_id
    return out


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


def _parse_optional_datetime(value: Any) -> Any:
    """Parse optional ISO datetime values from MCP JSON arguments."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("virtual_key_valid_until must be an ISO 8601 string.")
    from datetime import datetime

    return datetime.fromisoformat(value.replace("Z", "+00:00"))


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

            # Issue #390 Phase 2: resolve ``resource_pk`` and filter the
            # satellite queries by authoritative FK instead of slug. This
            # closes the cross-workspace slug-reuse leak vector on
            # ResourceToken and ResourceSchema (neither has a context_id
            # column to enforce the workspace boundary at query time).
            from sqlalchemy import func, select

            from models.memory import Memory
            from models.resource import ResourceSchema, ResourceToken
            from services.resource_lookup import resolve_resource_pk

            resource_pk = await resolve_resource_pk(db, workspace_id, resource_id)
            if resource_pk is None:
                # Boundary check passed but no Resource row — pre-a97
                # orphan or post-a97 gap. Return zero impact rather than
                # surface slug-only counts.
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
                    token_count=0,
                    memory_count=0,
                    current_schema_version=None,
                )

            token_count_subq = (
                select(func.count(ResourceToken.id))
                .where(
                    ResourceToken.resource_pk == resource_pk,
                    ResourceToken.is_active == True,  # noqa: E712
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
                .where(ResourceSchema.resource_pk == resource_pk)
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

            from models.resource import ResourceSchema
            from services.resource_lookup import resolve_resource_pk

            # Issue #390 Phase 2: resolve ``resource_pk`` once and filter the
            # schema lookup by authoritative FK. ResourceSchema has no
            # context_id, so the prior Context-JOIN pattern was defensive
            # but not definitive — soft-deleted context slug reuse could
            # still have surfaced another workspace's schema under the
            # Phase 1 writer gap.
            resource_pk = await resolve_resource_pk(db, workspace_id, resource_id)
            if resource_pk is None:
                # The workspace boundary check passed but no Resource entity
                # row exists — this is a data-integrity gap (setup_resource
                # never ran, or the Resource row was dropped while the
                # Context persists). Returning ``schema_not_found`` would
                # mislead callers into creating a schema for a resource
                # that cannot accept it. Surface a distinct error so the
                # next step is "bind the resource", not "create a schema".
                return _error_response(
                    "resource_not_found",
                    f"Resource '{resource_id}' is not initialized or is no longer bound in this workspace.",
                    help="Run setup_resource for this resource or rebind it before requesting its schema.",
                )

            query = select(ResourceSchema).where(ResourceSchema.resource_pk == resource_pk)

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
            from models.resource import Resource, ResourceToken

            include_revoked = args.get("include_revoked", True)
            try:
                limit = min(max(int(args.get("limit", 50)), 1), 100)
                offset = max(int(args.get("offset", 0)), 0)
            except (ValueError, TypeError):
                return _error_response("validation_error", "limit and offset must be integers.")

            # Issue #390 Phase 2: filter tokens by ``resource_pk`` instead of
            # the Context-JOIN / slug pattern. Workspace scope is enforced by
            # joining against ``resources`` (workspace-scoped by table
            # invariant ``uq_resources_workspace_resource_id``) — a token's
            # resource_pk FK guarantees it belongs to exactly one workspace.
            # Legacy resource_pk IS NULL rows are excluded from the list view;
            # they are draining in production within the observation window
            # before Phase C tightens the column to NOT NULL.
            #
            # Preserve the pre-#390 behavior of hiding tokens whose backing
            # Context was soft-deleted — the existence filter runs as an
            # EXISTS subquery against ``contexts`` so the CWE-639 fix
            # (resource_pk-scoped reads) is not compromised.
            active_context_exists = exists().where(
                Context.workspace_id == Resource.workspace_id,
                Context.resource_id == Resource.resource_id,
                Context.deleted_at.is_(None),
            )
            conditions = [
                Resource.workspace_id == workspace_id,
                active_context_exists,
            ]
            if resource_id:
                conditions.append(Resource.resource_id == resource_id)
            if not include_revoked:
                conditions.append(ResourceToken.is_active == True)  # noqa: E712

            total_result = await db.execute(
                sa_select(func.count(ResourceToken.id))
                .join(Resource, Resource.id == ResourceToken.resource_pk)
                .where(and_(*conditions))
            )
            total = total_result.scalar() or 0

            tokens_result = await db.execute(
                sa_select(ResourceToken)
                .join(Resource, Resource.id == ResourceToken.resource_pk)
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
            # workspace-scoped Redis counter. Keep the check after permission
            # and workspace-boundary gates but before any DB writes so a quota
            # failure cannot partially ingest a batch. The DB helper resolves
            # resource_pk internally to avoid slug-only quota scope.
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

            # Domain validation + persistence via the shared service
            # (Issue #1255). The MCP surface relies on the service for all
            # per-item validation; wire strings are rendered locally by
            # _format_batch_item_error to stay byte-compatible.
            valid_events, validation_errors = resource_ingest_service.validate_events(events)

            created_ids: list[int] = []
            persist_errors: list[IngestItemError] = []

            if valid_events:
                # Resolve resources.id once per batch via the shared chokepoint
                # (Issue #390 Phase 2). If the Resource entity row does not
                # exist, ingest cannot safely proceed — the before_insert
                # invariant listener on ResourceEvent would raise IntegrityError
                # for every event in the batch and the errors would be masked
                # as generic "constraint violation" strings. Reject the batch
                # up front with an actionable error so the caller knows to run
                # ``setup_resource`` or ``setup_connector`` first.
                resource_pk = await resource_ingest_service.resolve_authoritative_resource_pk(
                    db, workspace_id=workspace_id, resource_id=resource_id
                )
                if resource_pk is None:
                    return _error_response(
                        "resource_not_found",
                        f"Resource '{resource_id}' has no backing entity row in your workspace.",
                        help="Run setup_resource() or setup_connector() first to bind the resource.",
                    )

                result = await resource_ingest_service.persist_events(
                    db,
                    resource_id=resource_id,
                    resource_pk=resource_pk,
                    events=valid_events,
                )
                created_ids = result.created_ids
                persist_errors = result.errors

            # Historic MCP ordering: validation errors first (pass 1), then
            # persistence errors (pass 2).
            errors = [
                _format_batch_item_error(err) for err in [*validation_errors, *persist_errors]
            ]

            # Commit + post-commit indexer boundary (shared service). The
            # scheduler is injected so the service never imports from the
            # adapter layer; this lazy import mirrors the pre-refactor MCP
            # path and keeps the routes module as the helper's single home
            # (it also serves the REST single-event path).
            from api.routes.resource_ingest import _schedule_indexer_for_resource

            await resource_ingest_service.finalize_batch(
                db,
                workspace_id=workspace_id,
                resource_id=resource_id,
                created_ids=created_ids,
                schedule_indexer=_schedule_indexer_for_resource,
            )

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

            # 7a. Upsert Resource entity (Issue #390 Phase 2). Every satellite
            # table write (IndexerState, ResourceEvent, ResourceSchema,
            # ResourceToken) references ``resources.id`` as ``resource_pk``;
            # the entity row must exist before any satellite write fires its
            # ``before_insert`` invariant listener. Pre-a97 deployments
            # backfilled Resource rows from existing Contexts; post-a97
            # setup_resource calls create the Resource row themselves to
            # close that gap.
            from services.resource_lookup import upsert_resource

            resource_pk = await upsert_resource(
                db,
                workspace_id=workspace_id,
                resource_id=resource_id,
                name=args.get("display_name") or name,
                created_by=user_id,
            )

            # 7b. Create context (direct ORM — ContextService.create_context commits internally)
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
                resource_pk=resource_pk,
                workspace_id=workspace_id,
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


async def handle_setup_connector(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Provision an ai-worker connector using Resource Foundation."""
    if "connector_type" not in args or "resource_id" not in args:
        return _error_response(
            "missing_fields",
            "Missing required fields: connector_type, resource_id",
        )

    connector_type = args["connector_type"]
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
            role_err = await _check_owner_admin_role(db, user_id, workspace_id)
            if role_err:
                return role_err

            try:
                quota_events_per_hour = int(args.get("quota_events_per_hour", 1000))
                virtual_key_valid_until = _parse_optional_datetime(
                    args.get("virtual_key_valid_until")
                )
            except (TypeError, ValueError) as ve:
                return _error_response("validation_error", str(ve))

            oauth_tokens = args.get("oauth_tokens")
            if oauth_tokens is not None and not isinstance(oauth_tokens, dict):
                return _error_response("validation_error", "oauth_tokens must be an object.")
            # #866: validate pii_guardrail_config against the documented schema
            # (shared with the REST provision path). Fail-secure on malformed input;
            # rejects unknown keys and missing detectors, not just non-objects.
            from models.schemas import validate_pii_guardrail_config

            try:
                pii_guardrail_config = validate_pii_guardrail_config(
                    args.get("pii_guardrail_config")
                )
            except ValueError as ve:
                return _error_response("validation_error", str(ve))

            # Value-based guard (#1350 review): an explicit "runtime": null is
            # a common client spelling of "no override" and must provision
            # with runtime_config=NULL, same as the REST create path.
            runtime_config = None
            if args.get("runtime") is not None:
                from pydantic import ValidationError as PydanticValidationError

                from models.worker_runtime import WorkerRuntimeConfig

                try:
                    runtime_config = WorkerRuntimeConfig.model_validate(args["runtime"]).model_dump(
                        mode="json"
                    )
                except PydanticValidationError as ve:
                    # errors(include_input=False): the rejected document may
                    # carry values the tenant should not see echoed (e.g. a
                    # mistakenly pasted redis://:password@ URL) — same
                    # sanitization contract as validate_pii_guardrail_config.
                    reasons = "; ".join(
                        f"{'.'.join(str(loc) for loc in err['loc'])}: {err['msg']}"
                        for err in ve.errors(include_url=False, include_input=False)
                    )
                    return _error_response("validation_error", f"Invalid runtime config: {reasons}")
                except (TypeError, ValueError) as ve:
                    return _error_response("validation_error", str(ve))

            from services.connector_provisioning import ConnectorProvisioningService
            from utils.exceptions import MemoryCloudException

            result = await ConnectorProvisioningService(db).provision_connector(
                workspace_id=workspace_id,
                user_id=user_id,
                connector_type=connector_type,
                resource_id=resource_id,
                display_name=args.get("display_name"),
                oauth_tokens=oauth_tokens,
                pii_guardrail_config=pii_guardrail_config,
                litellm_virtual_key_id=args.get("litellm_virtual_key_id"),
                virtual_key_valid_until=virtual_key_valid_until,
                quota_events_per_hour=quota_events_per_hour,
                # Spec 2026-06-02 registration fields: forwarded so the MCP path
                # can bind a write-target context and mint a KMC write key.
                context_id=args.get("context_id"),
                auto_create_context_name=args.get("auto_create_context_name"),
                llm_config=args.get("llm_config"),
                channel_ids=args.get("channel_ids"),
                locale=args.get("locale"),
                external_team_id=args.get("external_team_id"),
                runtime_config=runtime_config,
            )
            await db.commit()
            await db.refresh(result.connector)
            await db.refresh(result.token)

            await _log_tool_usage(
                db,
                user_id,
                "setup_connector",
                start_time,
                201,
                workspace_id=workspace_id,
            )

            success_kwargs: dict[str, object] = {
                "message": f"Connector '{result.connector.id}' set up successfully.",
                "connector_id": str(result.connector.id),
                "connector_type": result.connector.connector_type,
                "resource_id": result.resource_id,
                "resource_pk": str(result.resource_pk),
                "token_id": result.token.id,
                "token": result.plaintext_token,
                "quota_events_per_hour": result.token.quota_events_per_hour,
                "idempotency_key_prefix": f"{result.connector.id}:",
            }
            if result.context_id is not None:
                success_kwargs["context_id"] = str(result.context_id)
            if result.plaintext_kmc_api_key is not None:
                success_kwargs["kmc_api_key"] = result.plaintext_kmc_api_key
            return _success_response(**success_kwargs)

        except MemoryCloudException as exc:
            await db.rollback()
            await _log_tool_usage(
                db,
                user_id,
                "setup_connector",
                start_time,
                exc.status_code,
                workspace_id=workspace_id,
            )
            return _error_response(
                exc.error_code,
                exc.message,
                **exc.details,
            )
        except Exception as e:
            await db.rollback()
            logger.error(f"setup_connector_failed: {e}", exc_info=True)
            await _log_tool_usage(
                db,
                user_id,
                "setup_connector",
                start_time,
                500,
                workspace_id=workspace_id,
            )
            return _error_response("setup_connector_error", str(e))

    return _error_response("internal_error", "Database session unavailable")
