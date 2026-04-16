"""Resource Indexer Status API route.

Issue #326: Read-only visibility into indexer state + recent ingest events
for a single resource. Complements the write-side ``resource_ingest.py``
and the schema-side ``resource_schema.py`` — kept as a separate module so
per-resource *observability* has a single, greppable home and does not
bloat ``resources.py`` (workspace-level list) or force the ingest path
router to carry read payload shapes.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import APIKeyOrSessionUser
from db.base import get_db
from services.permission_service import PermissionService
from services.resource_indexer import get_indexer_status_for_context

router = APIRouter(prefix="/resources", tags=["resource-indexer"])


# ============================================================================
# Pydantic Models
# ============================================================================


IndexerJobStatus = Literal["idle", "queued", "running", "failed"]
"""Mirrors the CHECK constraint on indexer_state.job_status. Kept as a
Literal so OpenAPI clients see an enum and schema drift is caught at
the pydantic boundary rather than in the UI."""


IndexerSkippedReason = Literal[
    "no_pending_events",
    "schema_not_found",
    "context_not_found",
    "empty_valid_points",
]
"""Reasons the indexer may record under ``metrics.reason`` when a run was
skipped. Enum is derived from in-tree uses in ``services.resource_indexer``;
unknown values degrade to ``None`` on the wire (see service layer)."""


class IndexerStateMetrics(BaseModel):
    """Per-run indexer metrics, flattened from the JSONB column."""

    applied_upserts: int = Field(0, description="Upsert events applied in the last run")
    applied_deletes: int = Field(0, description="Delete events applied in the last run")
    errors: int = Field(0, description="Errors observed in the last run")
    skipped_reason: IndexerSkippedReason | None = Field(
        None,
        description=(
            "Present only when the last run was skipped. Surfacing a stale "
            "reason after a successful run is a UI bug we explicitly avoid."
        ),
    )


class IndexerState(BaseModel):
    """Indexer state snapshot for one resource/context."""

    job_status: IndexerJobStatus
    last_run_at: str | None = Field(
        None, description="ISO-8601 UTC timestamp of the most recent run"
    )
    next_run_at: str | None = Field(
        None, description="ISO-8601 UTC timestamp of the next scheduled run"
    )
    active_version: int = Field(
        ...,
        description=(
            "Active Qdrant collection version (blue/green); used for rollout coordination."
        ),
    )
    last_offset: int = Field(
        ...,
        description=(
            "Highest resource_events.id processed so far. Monotonically "
            "increasing; compare against ResourceEvent.id to estimate lag."
        ),
    )
    lag_seconds: float | None = Field(
        None,
        description=(
            "Server-computed (now - last_run_at). ``None`` when the indexer "
            "has never run. The UI uses this for health thresholds only — "
            "display a human relative time from ``last_run_at`` directly "
            "to avoid value drift while the tab is open."
        ),
    )
    metrics: IndexerStateMetrics


class ResourceEventItem(BaseModel):
    """Single row in the recent ingest events list."""

    id: int = Field(..., description="Event sequence id (stable ordering)")
    op: Literal["upsert", "delete"] = Field(..., description="Event operation")
    doc_id: str = Field(..., description="Document identifier (stable across versions)")
    version: int | None = Field(
        None,
        description=(
            "Document version. NULL is a legitimate value for a "
            "delete-all-versions op (Issue #262)."
        ),
    )
    created_at: str | None = Field(None, description="ISO-8601 UTC creation timestamp")


class IndexerStatusResponse(BaseModel):
    """Response body for ``GET /api/v1/resources/{resource_id}/indexer-status``."""

    resource_id: str = Field(..., description="Echo of the requested slug")
    state: IndexerState | None = Field(
        None,
        description=(
            "Indexer state row, or ``None`` when the indexer has never run "
            "against this resource/context. The 2-value model is intentional "
            "so the UI branches on ``state is None`` rather than inferring "
            "emptiness from individual field values."
        ),
    )
    recent_events: list[ResourceEventItem] = Field(
        default_factory=list,
        description=(
            "Latest ingest events, newest first. Capped server-side at 5 — "
            "there is no client-tunable limit on this endpoint by design; "
            "a cursor-paginated timeline belongs on a dedicated endpoint."
        ),
    )


# ============================================================================
# Route
# ============================================================================


@router.get("/{resource_id}/indexer-status", response_model=IndexerStatusResponse)
async def get_indexer_status(
    resource_id: str,
    user: APIKeyOrSessionUser,
    db: AsyncSession = Depends(get_db),
) -> IndexerStatusResponse:
    """Return indexer state and recent ingest events for a resource.

    The ``resource_id`` URL path parameter accepts the human-readable slug
    (e.g. ``my-github-repo``), not an internal UUID. The slug is resolved to
    the caller's workspace-scoped resource via
    ``PermissionService.resolve_resource_by_slug``. This backward-compatible
    contract is maintained so existing integrations continue to work without
    modification after the v0.12.0 UUID FK migration.

    Authorization:
        - ``APIKeyOrSessionUser``: session cookie or API key. ``resource_token``
          is a write-scoped credential and is intentionally rejected here
          (enforced by the dependency choice, pinned by isolation tests).
        - Workspace boundary: resolved via
          ``PermissionService.resolve_resource_by_slug``, which returns 404
          (not 403) when the slug exists only in another workspace. 404 keeps
          cross-workspace existence from leaking (CWE-639 / OWASP A01).

    Args:
        resource_id: Resource slug from the URL path.
        user: Authenticated principal (session or API key).
        db: Async DB session.

    Returns:
        IndexerStatusResponse with state + last 5 events.

    Raises:
        HTTPException(401): no valid authentication.
        HTTPException(404): resource not found OR not accessible by the caller.
    """
    permissions = PermissionService(db)
    context = await permissions.resolve_resource_by_slug(
        user_id=user["user_id"],
        resource_id=resource_id,
    )

    payload = await get_indexer_status_for_context(db, context)
    return IndexerStatusResponse(**payload)
