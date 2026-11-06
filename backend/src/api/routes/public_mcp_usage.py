"""Owner health of a public MCP endpoint's usage events (#2678).

``GET /api/v1/resources/{resource_id}/public-mcp/usage-health`` tells the
workspace owner whether usage of the resource's public endpoint is being
recorded: the last 24 hours of events by outcome, the deployment's gap rows
(events the server could not record), and this process's writer queue. The
writer numbers are process-local; the gap rows are the persistent view.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Path
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import WorkspaceOwner
from config.constants import RESOURCE_ID_MAX_LENGTH, RESOURCE_ID_PATTERN
from config.settings import get_settings
from db.base import get_db
from models.api_base import TZAwareBaseModel
from models.public_mcp import PublicMcpEndpoint
from services.permission_service import PermissionService
from services.public_mcp_usage_events import usage_health_counts
from services.public_mcp_usage_writer import get_usage_writer
from utils.datetime import utcnow

router = APIRouter(prefix="/resources", tags=["resources"])

HEALTH_WINDOW_HOURS = 24


class PublicMcpUsageGaps(TZAwareBaseModel):
    """Gap rows of the deployment in the window."""

    count: int = Field(..., description="Gap rows written in the window")
    dropped_events: int = Field(..., description="Events those gap rows account for")
    last_gap_at: datetime | None = Field(None, description="When the latest gap row was written")


class PublicMcpUsageWriterState(TZAwareBaseModel):
    """The usage event writer of the process that answered."""

    enabled: bool = Field(..., description="A writer runs in this process")
    queue_depth: int
    queue_capacity: int
    dropped_since_start: int = Field(..., description="Events this process dropped since start")
    last_flush_at: datetime | None = None
    last_flush_ok: bool | None = None
    process_local: Literal[True] = Field(
        default=True, description="These numbers describe one API process, not the deployment"
    )


class PublicMcpUsageHealthResponse(TZAwareBaseModel):
    """Whether usage of the resource's public MCP endpoint is being recorded."""

    resource_id: str
    endpoint_id: UUID | None = Field(
        None, description="The live public endpoint's id; null when the resource is not published"
    )
    window_hours: int
    events: int = Field(..., description="Events of the live endpoint in the window")
    outcomes: dict[str, int] = Field(..., description="Those events by outcome")
    gaps: PublicMcpUsageGaps
    writer: PublicMcpUsageWriterState
    retention_days: int


@router.get(
    "/{resource_id}/public-mcp/usage-health",
    response_model=PublicMcpUsageHealthResponse,
)
async def get_public_mcp_usage_health(
    owner: WorkspaceOwner,
    resource_id: str = Path(
        ...,
        min_length=1,
        max_length=RESOURCE_ID_MAX_LENGTH,
        pattern=RESOURCE_ID_PATTERN,
        description="Resource slug (`setup_resource`'s resource_id).",
    ),
    db: AsyncSession = Depends(get_db),
) -> PublicMcpUsageHealthResponse:
    """Recent usage events of the resource's public endpoint and recording health.

    Owner-only: ``WorkspaceOwner`` refuses non-owners (403) and
    ``resolve_resource_by_slug(required_role="owner")`` answers 404 for a
    slug of another workspace.
    """
    user_id, _ = owner
    context = await PermissionService(db).resolve_resource_by_slug(
        user_id=user_id, resource_id=resource_id, required_role="owner"
    )
    endpoint_id = (
        await db.execute(
            select(PublicMcpEndpoint.id).where(
                PublicMcpEndpoint.workspace_id == context.workspace_id,
                PublicMcpEndpoint.context_id == context.id,
                PublicMcpEndpoint.revoked_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    counts = await usage_health_counts(
        db, endpoint_id=endpoint_id, since=utcnow() - timedelta(hours=HEALTH_WINDOW_HOURS)
    )
    settings = get_settings()
    writer = get_usage_writer()
    if writer is not None:
        h = writer.health()
        writer_state = PublicMcpUsageWriterState(
            enabled=True,
            queue_depth=h.queue_depth,
            queue_capacity=h.queue_capacity,
            dropped_since_start=h.dropped_since_start,
            last_flush_at=h.last_flush_at,
            last_flush_ok=h.last_flush_ok,
        )
    else:
        writer_state = PublicMcpUsageWriterState(
            enabled=False, queue_depth=0, queue_capacity=0, dropped_since_start=0
        )
    return PublicMcpUsageHealthResponse(
        resource_id=resource_id,
        endpoint_id=endpoint_id,
        window_hours=HEALTH_WINDOW_HOURS,
        events=counts.events,
        outcomes=counts.outcomes,
        gaps=PublicMcpUsageGaps(
            count=counts.gap_count,
            dropped_events=counts.gap_dropped,
            last_gap_at=counts.last_gap_at,
        ),
        writer=writer_state,
        retention_days=settings.mcp_public_telemetry_retention_days,
    )
