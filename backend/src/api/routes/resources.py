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

from auth.dependencies import APIKeyOrSessionUser
from db.base import get_db
from models.auth import Context
from models.memory import Memory
from models.resource import ResourceEvent, ResourceSchema, ResourceToken
from services.permission_service import PermissionService
from utils.datetime import to_utc_iso
from utils.exceptions import AuthorizationError
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
    user: APIKeyOrSessionUser,
    db: AsyncSession = Depends(get_db),
):
    """List all resources in the caller's current workspace.

    Returns an ordered list (latest activity first) of contexts that have a
    non-null ``resource_id``, with aggregated counts joined from the
    resource_tokens, memories, resource_schemas, and resource_events tables.

    Args:
        user: Current user (session or API key).
        db: Database session.

    Returns:
        ResourceListResponse with resources[] and total count.

    Raises:
        AuthorizationError: User has no current workspace.

    Example:
        GET /api/v1/resources

        Response:
        {
            "resources": [
                {
                    "resource_id": "ec_products",
                    "context_id": "550e8400-...",
                    "context_name": "ec-products",
                    "context_display_name": "EC Products",
                    "token_count": 2,
                    "memory_count": 1234,
                    "current_schema_version": 3,
                    "created_at": "2026-03-01T12:00:00Z",
                    "updated_at": "2026-04-14T09:15:30Z"
                }
            ],
            "total": 1
        }
    """
    logger.info("list_resources_request", user_id=user["user_id"])

    # auth.dependencies injects current_workspace_id into the user dict already —
    # no extra SELECT needed.
    current_workspace_id = user.get("current_workspace_id")
    if not current_workspace_id:
        raise AuthorizationError("User must belong to a workspace")

    # Access-filter contexts BEFORE running the aggregate query so private
    # contexts (and contexts excluded by WorkspaceMember.allowed_context_ids)
    # don't leak resource_ids/stats to members/viewers. Owners/admins see all
    # contexts in the workspace — this matches the contexts list behavior.
    # Suspended members (allowed_context_ids IS NULL) and users with an empty
    # whitelist get an empty list here and short-circuit out below.
    accessible = await PermissionService(db).get_accessible_contexts(
        user["user_id"], current_workspace_id
    )
    accessible_ids = [c.id for c in accessible]
    if not accessible_ids:
        logger.info(
            "list_resources_success",
            user_id=user["user_id"],
            workspace_id=str(current_workspace_id),
            count=0,
        )
        return ResourceListResponse(resources=[], total=0)

    # Correlated subqueries — one stats bundle per matching context row.
    # resource_* tables are keyed by resource_id only (see Migration 055 note on
    # per-workspace uniqueness). We scope to this workspace via the Context join
    # on the outer query; collisions across workspaces are a pre-existing
    # architectural invariant tracked separately.
    token_count_subq = (
        select(func.count(ResourceToken.id))
        .where(
            ResourceToken.resource_id == Context.resource_id,
            ResourceToken.is_active == True,  # noqa: E712
        )
        .correlate(Context)
        .scalar_subquery()
    )

    # Memory has a workspace_id column, so we can scope defensively here even
    # though the other resource_* tables cannot (per the architectural invariant
    # comment above). For Memory specifically this tightens the count against
    # the unlikely case of a resource_id collision across workspaces.
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
        .where(ResourceSchema.resource_id == Context.resource_id)
        .correlate(Context)
        .scalar_subquery()
    )

    last_event_subq = (
        select(func.max(ResourceEvent.created_at))
        .where(ResourceEvent.resource_id == Context.resource_id)
        .correlate(Context)
        .scalar_subquery()
    )

    # Main query: workspace-scoped, resource-bound contexts with aggregated stats.
    # ORDER BY coalesces the most recent signal (last event vs context update).
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
        user_id=user["user_id"],
        workspace_id=str(current_workspace_id),
        count=len(resources),
    )

    return ResourceListResponse(resources=resources, total=len(resources))
