"""Resource list API route.

Issue #47: Web UI for resource management.

Provides a workspace-scoped list of resources with aggregated stats
(token count, memory count, current schema version, last event time)
for the Resource list page at /workspace/resources.

Complements the existing per-resource endpoints in ``resource_schema.py``
(``/resources/{resource_id}/impact``, ``/resources/{resource_id}/schema``).
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import APIKeyOrSessionUser
from db.base import get_db
from models.auth import Context
from models.memory import Memory
from models.resource import ResourceEvent, ResourceSchema, ResourceToken
from utils.exceptions import AuthorizationError
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/resources", tags=["resources"])


def _iso_utc(dt: datetime) -> str:
    """Render a DB timestamp as ISO 8601 UTC with explicit Z suffix.

    The project's DateTime columns are naive (TIMESTAMP WITHOUT TIME ZONE) and
    stored by convention in UTC. ``datetime.isoformat()`` on a naive value omits
    any offset, which browsers then parse as local time. Appending ``Z`` makes
    the UTC contract explicit on the wire.
    """
    if dt.tzinfo is not None:
        return dt.isoformat()
    return dt.isoformat() + "Z"


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
        ..., description="Latest activity time — max(context.updated_at, last_event_at)"
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
            )
        )
        # ORDER BY references the SELECT alias so the correlated subquery is
        # evaluated once per row, not twice. SQLAlchemy re-emits scalar_subquery
        # objects at each use site; referencing the alias via text() avoids that.
        # The 3-level coalesce mirrors the response `updated_at` fallback so the
        # server-side sort order agrees with the timestamp each row exposes.
        .order_by(
            func.coalesce(
                text("last_event_at"),
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
            created_at=_iso_utc(row.created_at),
            # Fall back through last_event → context.updated_at → context.created_at
            # so we never call .isoformat() on None. _iso_utc() appends the Z
            # suffix that naive UTC timestamps need for JS clients to parse.
            updated_at=_iso_utc(row.last_event_at or row.context_updated_at or row.created_at),
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
