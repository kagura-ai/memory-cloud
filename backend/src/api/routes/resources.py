"""Resource list API route.

Issue #47: Web UI for resource management.

Provides a workspace-scoped list of resources with aggregated stats
(token count, memory count, current schema version, last event time)
for the Resource list page at /workspace/resources.

Complements the existing per-resource endpoints in ``resource_schema.py``
(``/resources/{resource_id}/impact``, ``/resources/{resource_id}/schema``).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import WorkspaceOwner
from db.base import get_db
from models.auth import Context
from models.memory import Memory
from models.resource import Resource, ResourceEvent, ResourceSchema, ResourceToken
from services.permission_service import PermissionService
from services.resource_events import MAX_EVENTS_PAGE_SIZE, list_resource_events
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


# ============================================================================
# Issue #316 — Resource event data browser (cursor pagination)
# ============================================================================


# Defensive cap on inline payload size. Ingest is expected to reject payloads
# above this, but a multi-MB row must not bloat the response or the browser —
# over the cap, ``payload`` is omitted and ``payload_truncated`` is set so the
# UI can offer a "too large to preview" affordance (issue #316 P2).
PAYLOAD_INLINE_MAX_BYTES = 1_000_000


class ResourceEventRecord(BaseModel):
    """A single ingest event row for the Resource Detail Data tab."""

    id: int = Field(..., description="BigInt append-only event id (cursor key)")
    op: str = Field(..., description="Operation: 'upsert' or 'delete'")
    doc_id: str = Field(..., description="Document identifier (stable across versions)")
    version: int | None = Field(None, description="Document version; null for delete-all-versions")
    idempotency_key: str | None = Field(
        None, description="Client-provided deduplication key, if any"
    )
    importance: float = Field(..., description="Importance score (0.0–1.0)")
    created_at: str = Field(..., description="Event creation time (ISO 8601 UTC)")
    payload: dict | None = Field(
        None,
        description=(
            "JSONB payload; null for delete ops OR when omitted because it "
            "exceeds the inline size cap (see payload_truncated)"
        ),
    )
    event_metadata: dict | None = Field(
        None, description="Additional metadata (source, correlation_id, etc.)"
    )
    payload_bytes: int = Field(..., description="Serialized payload size in bytes (0 when null)")
    payload_truncated: bool = Field(
        ...,
        description=(
            "True when the payload exceeded the inline size cap and was omitted from this response"
        ),
    )


class ResourceEventsResponse(BaseModel):
    """Cursor-paginated resource events response."""

    events: list[ResourceEventRecord]
    next_cursor: str | None = Field(
        None, description="Opaque cursor for the next page; null on the last page"
    )


def _to_event_record(event: ResourceEvent) -> ResourceEventRecord:
    """Map a ``ResourceEvent`` ORM row to the API record, applying the
    inline payload-size guard."""
    payload = event.payload
    payload_bytes = 0
    payload_out: dict | None = None
    payload_truncated = False
    if payload is not None:
        # separators drop insignificant whitespace so the byte count reflects
        # the compact wire size, not Python's default pretty spacing.
        payload_bytes = len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        if payload_bytes > PAYLOAD_INLINE_MAX_BYTES:
            payload_truncated = True
        else:
            payload_out = payload

    return ResourceEventRecord(
        id=event.id,
        op=event.op,
        doc_id=event.doc_id,
        version=event.version,
        idempotency_key=event.idempotency_key,
        importance=event.importance,
        created_at=to_utc_iso(event.created_at),
        payload=payload_out,
        event_metadata=event.event_metadata,
        payload_bytes=payload_bytes,
        payload_truncated=payload_truncated,
    )


@router.get("/{resource_id}/events", response_model=ResourceEventsResponse)
async def list_resource_events_route(
    resource_id: str,
    owner: WorkspaceOwner,
    op: Literal["upsert", "delete"] | None = Query(None, description="Filter by operation type"),
    doc_id: str | None = Query(None, description="Filter by exact document id"),
    version: int | None = Query(None, description="Filter by exact document version"),
    since: datetime | None = Query(
        None, description="Only events created at or after this ISO 8601 timestamp"
    ),
    limit: int | None = Query(
        None,
        ge=1,
        le=MAX_EVENTS_PAGE_SIZE,
        description="Page size (default 20, max 100)",
    ),
    cursor: str | None = Query(
        None, description="Opaque cursor from a previous response's next_cursor"
    ),
    db: AsyncSession = Depends(get_db),
):
    """Browse ingested events for a resource, newest first (cursor-paginated).

    Owner-only (``WorkspaceOwner``) for parity with the rest of the resources
    surface (#389). Events are scoped to the caller's workspace via the
    authoritative ``resource_pk``; a slug that does not resolve to a Resource
    in this workspace returns an empty page (CWE-639 fail-safe).

    Example:
        GET /api/v1/resources/ec-products/events?op=upsert&limit=20
    """
    user_id, current_workspace_id = owner

    cursor_id: int | None = None
    if cursor is not None:
        try:
            cursor_id = int(cursor)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid cursor") from exc

    events, next_cursor = await list_resource_events(
        db,
        current_workspace_id,
        resource_id,
        limit=limit,
        cursor_id=cursor_id,
        op=op,
        doc_id=doc_id,
        version=version,
        since=since,
    )

    logger.info(
        "list_resource_events_success",
        user_id=user_id,
        workspace_id=str(current_workspace_id),
        resource_id=resource_id,
        count=len(events),
        has_next=next_cursor is not None,
    )

    return ResourceEventsResponse(
        events=[_to_event_record(e) for e in events],
        next_cursor=next_cursor,
    )
