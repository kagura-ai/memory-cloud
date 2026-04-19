"""Resource list API route.

Issue #47: Web UI for resource management.

Provides a workspace-scoped list of resources with aggregated stats
(token count, memory count, current schema version, last event time)
for the Resource list page at /workspace/resources.

Complements the existing per-resource endpoints in ``resource_schema.py``
(``/resources/{resource_id}/impact``, ``/resources/{resource_id}/schema``).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import WorkspaceOwner
from db.base import get_db
from models.auth import Context
from models.memory import Memory
from models.resource import Resource, ResourceEvent, ResourceSchema, ResourceToken
from services.permission_service import PermissionService
from utils.datetime import to_utc_iso
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/resources", tags=["resources"])


# ============================================================================
# Pydantic Models
# ============================================================================


class ResourceListItem(BaseModel):
    """Single resource entry in the workspace resource list."""

    resource_id: str = Field(..., description="Resource identifier")
    context_id: str = Field(..., description="UUID of the context bound to this resource")
    context_name: str = Field(..., description="Context name (URL-safe identifier)")
    context_display_name: str | None = Field(None, description="Human-readable context name")
    token_count: int = Field(..., description="Number of active resource tokens")
    memory_count: int = Field(..., description="Number of non-deleted memories")
    current_schema_version: int | None = Field(
        None, description="Latest schema version, or null if no schema registered"
    )
    created_at: str = Field(..., description="Context creation time (ISO 8601 UTC)")
    updated_at: str = Field(
        ...,
        description=(
            "Most recent activity — GREATEST(last_event_at, context.updated_at, "
            "context.created_at) as ISO 8601 UTC"
        ),
    )


class ResourceListResponse(BaseModel):
    """Workspace resource list response."""

    resources: list[ResourceListItem]
    total: int


# ============================================================================
# Route
# ============================================================================


@router.get("", response_model=ResourceListResponse)
async def list_resources(
    owner: WorkspaceOwner,
    db: AsyncSession = Depends(get_db),
):
    """List all resources in the caller's current workspace.

    Returns an ordered list (latest activity first) of contexts that have a
    non-null ``resource_id``, with aggregated counts joined from the
    resource_tokens, memories, resource_schemas, and resource_events tables.

    Owner-only (#389): ``WorkspaceOwner`` rejects non-owners with 403.
    ``get_accessible_contexts`` below is a no-op for owners and retained
    as defense-in-depth against a role-gate regression.

    Example:
        GET /api/v1/resources
    """
    user_id, current_workspace_id = owner
    logger.info("list_resources_request", user_id=user_id)

    # Defense-in-depth: owners see every context, so this is a no-op for the
    # intended caller; retained so a future role-gate regression does not
    # leak stats for private / allowed_context_ids-restricted contexts.
    accessible = await PermissionService(db).get_accessible_contexts(user_id, current_workspace_id)
    accessible_ids = [c.id for c in accessible]
    if not accessible_ids:
        logger.info(
            "list_resources_success",
            user_id=user_id,
            workspace_id=str(current_workspace_id),
            count=0,
        )
        return ResourceListResponse(resources=[], total=0)

    # Issue #390 Phase 2: satellite stats are scoped by ``resource_pk``
    # (authoritative FK to ``resources.id``) instead of by slug. The outer
    # query joins ``Context`` to ``Resource`` via
    # ``(workspace_id, resource_id)`` so each row carries the UUID that the
    # correlated subqueries filter against — this closes the cross-workspace
    # slug-reuse leak that could otherwise surface counts from a different
    # workspace's soft-deleted resource.
    token_count_subq = (
        select(func.count(ResourceToken.id))
        .where(
            ResourceToken.resource_pk == Resource.id,
            ResourceToken.is_active == True,  # noqa: E712
        )
        .correlate(Resource)
        .scalar_subquery()
    )

    # Memory has a workspace_id column and is not part of the resource_pk
    # migration — keep the slug + workspace_id filter. Context carries both.
    memory_count_subq = (
        select(func.count(Memory.id))
        .where(
            Memory.resource_id == Context.resource_id,
            Memory.workspace_id == Context.workspace_id,
            Memory.deleted_at.is_(None),
        )
        .correlate(Context)
        .scalar_subquery()
    )

    schema_version_subq = (
        select(func.max(ResourceSchema.schema_version))
        .where(ResourceSchema.resource_pk == Resource.id)
        .correlate(Resource)
        .scalar_subquery()
    )

    last_event_subq = (
        select(func.max(ResourceEvent.created_at))
        .where(ResourceEvent.resource_pk == Resource.id)
        .correlate(Resource)
        .scalar_subquery()
    )

    # Main query: workspace-scoped, resource-bound contexts with aggregated stats.
    # ORDER BY coalesces the most recent signal (last event vs context update).
    # Resource is joined via (workspace_id, resource_id) and the subqueries
    # above correlate against ``Resource.id`` (the authoritative resource_pk).
    # LEFT OUTER JOIN: Contexts with a resource_id but no Resource row yet
    # (pre-a97 migration gap) still appear in the list with zero stats,
    # preserving backward visibility during the Phase 1 → Phase 2 transition.
    result = await db.execute(
        select(
            Context.id.label("context_id"),
            Context.name.label("context_name"),
            Context.display_name.label("context_display_name"),
            Context.resource_id.label("resource_id"),
            Context.created_at.label("created_at"),
            Context.updated_at.label("context_updated_at"),
            token_count_subq.label("token_count"),
            memory_count_subq.label("memory_count"),
            schema_version_subq.label("schema_version"),
            last_event_subq.label("last_event_at"),
        )
        .select_from(Context)
        .outerjoin(
            Resource,
            and_(
                Resource.workspace_id == Context.workspace_id,
                Resource.resource_id == Context.resource_id,
            ),
        )
        .where(
            and_(
                Context.workspace_id == current_workspace_id,
                Context.resource_id.is_not(None),
                Context.deleted_at.is_(None),
                # Scope to the subset the caller can actually see, per RBAC.
                Context.id.in_(accessible_ids),
            )
        )
        # "Most recent activity" = max across the three signals. PostgreSQL's
        # GREATEST ignores NULLs, so a missing last_event_at still picks up
        # context.updated_at (or created_at as the final fallback).
        # Note: SELECT aliases are not visible inside function calls in ORDER BY
        # per PG's scoping rules, so we reference the scalar_subquery object
        # directly here. This does cause the subquery to be emitted twice, but
        # with < 50 resources/workspace the duplication is negligible, and PG's
        # query planner can often hoist the correlated subquery to a join.
        .order_by(
            func.greatest(
                last_event_subq,
                Context.updated_at,
                Context.created_at,
            ).desc(),
        )
    )
    rows = result.all()

    resources = [
        ResourceListItem(
            resource_id=row.resource_id,
            context_id=str(row.context_id),
            context_name=row.context_name,
            context_display_name=row.context_display_name,
            token_count=row.token_count or 0,
            memory_count=row.memory_count or 0,
            current_schema_version=row.schema_version,
            created_at=to_utc_iso(row.created_at),
            # Pick the most recent signal across the three timestamps, ignoring
            # None — matches the ORDER BY greatest() above so the sort order
            # agrees with the rendered value. to_utc_iso() handles None + adds
            # the explicit Z suffix that JS clients need.
            updated_at=to_utc_iso(
                max(
                    filter(
                        None,
                        (row.last_event_at, row.context_updated_at, row.created_at),
                    )
                )
            ),
        )
        for row in rows
    ]

    logger.info(
        "list_resources_success",
        user_id=user_id,
        workspace_id=str(current_workspace_id),
        count=len(resources),
    )

    return ResourceListResponse(resources=resources, total=len(resources))
