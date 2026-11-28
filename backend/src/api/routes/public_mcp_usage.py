"""Owner views of a public MCP endpoint's usage events (#2678, #3035).

``GET /api/v1/resources/{resource_id}/public-mcp/usage-health`` tells the
workspace owner whether usage of the resource's public endpoint is being
recorded: the last 24 hours of events by outcome, the deployment's gap rows
(events the server could not record), and this process's writer queue. The
writer numbers are process-local; the gap rows are the persistent view.

``GET /api/v1/resources/{resource_id}/public-mcp/usage-stats`` gives the
owner the live endpoint's calls, searches and search outcomes per UTC day, by
route and caller, the daily visitors a server with a bound key reported, and
the documents its searches returned most often, with their public title and
URL while the endpoint serves (``services.public_mcp_usage_stats``).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import WorkspaceOwner
from config.constants import RESOURCE_ID_MAX_LENGTH, RESOURCE_ID_PATTERN
from config.settings import get_settings
from db.base import get_db
from models.api_base import TZAwareBaseModel
from models.public_mcp import PublicMcpEndpoint
from models.public_mcp_usage import PMU_ROUTE_REST
from services.permission_service import PermissionService
from services.public_mcp_usage_events import usage_health_counts
from services.public_mcp_usage_stats import (
    USAGE_STATS_DEFAULT_DAYS,
    USAGE_STATS_MAX_DAYS,
    DayCounts,
    UsageStats,
    check_usage_stats_rate_limit,
    read_usage_stats,
    resolve_stats_endpoint,
    usage_recording_flags,
)
from services.public_mcp_usage_writer import get_usage_writer
from utils.datetime import utcnow
from utils.logger import get_logger

logger = get_logger(__name__)

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
    slug of another workspace. The live endpoint's events include the REST
    public searches recorded for it (#3035): this tells whether usage is being
    recorded at all.
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


_CALLS_BY_ROUTE = (
    "Calls by route: `mcp` (the public MCP endpoint) and `rest` (REST public searches of the "
    "context made with a key bound to it); every route is a key"
)
_SEARCHES_BY_ROUTE = "Searches by route, whatever their outcome"
_ANSWERED_BY_ROUTE = (
    "Answered searches (outcome success or degraded) by route; every route is a key"
)
_CALLS_BY_CALLER = (
    "`anonymous` (public MCP calls), or `bound_key` (REST searches made with a key bound to "
    "the context); every caller is a key"
)


class PublicMcpUsageStatsCounts(TZAwareBaseModel):
    """Calls, searches and search outcomes, by route and caller."""

    calls: int = Field(..., description="Calls of the endpoint, any tool, route and outcome")
    searches: int = Field(..., description="Calls of the `recall` tool, whatever their outcome")
    search_outcomes: dict[str, int] = Field(
        ..., description="Searches by outcome; every outcome is a key (zero-filled)"
    )
    calls_by_route: dict[str, int] = Field(..., description=_CALLS_BY_ROUTE)
    searches_by_route: dict[str, int] = Field(..., description=_SEARCHES_BY_ROUTE)
    searches_answered_by_route: dict[str, int] = Field(..., description=_ANSWERED_BY_ROUTE)
    calls_by_caller: dict[str, int] = Field(..., description=_CALLS_BY_CALLER)


class PublicMcpUsageStatsDay(TZAwareBaseModel):
    """One UTC calendar day."""

    date: date
    calls: int
    searches: int
    search_outcomes: dict[str, int]
    calls_by_route: dict[str, int] = Field(..., description=_CALLS_BY_ROUTE)
    searches_by_route: dict[str, int] = Field(..., description=_SEARCHES_BY_ROUTE)
    searches_answered_by_route: dict[str, int] = Field(..., description=_ANSWERED_BY_ROUTE)
    calls_by_caller: dict[str, int] = Field(..., description=_CALLS_BY_CALLER)
    visitors: int | None = Field(
        ...,
        description=(
            "Distinct visitor values a server reported with a bound key that UTC day; null when "
            "visitors could not be read"
        ),
    )


class PublicMcpUsageStatsDocument(TZAwareBaseModel):
    """A document the endpoint's searches returned."""

    doc_key: str = Field(..., description="Opaque document key of the usage events")
    searches: int = Field(..., description="Searches that returned the document")
    best_rank: int | None = Field(None, description="Its best (lowest) rank in those searches")
    title: str | None = Field(
        None, description="What the public endpoint shows as its title; null when not shown now"
    )
    source_url: str | None = None


class PublicMcpUsageStatsDocuments(TZAwareBaseModel):
    """The documents searches returned most often."""

    status: Literal["ok", "unavailable"] = Field(
        ..., description="`unavailable`: this section could not be read; the counts still are"
    )
    publication_serving: bool = Field(
        ..., description="Titles and URLs are given only while the endpoint serves right now"
    )
    searches_scanned: int = Field(..., description="Searches read for this list (newest first)")
    scan_limit: int = Field(..., description="At most this many searches are read")
    scanned_since: datetime | None = Field(
        None,
        description=(
            "The time of the oldest search the list is based on, UTC; null when no search was "
            "scanned"
        ),
    )
    items: list[PublicMcpUsageStatsDocument]


class PublicMcpUsageStatsVisitors(TZAwareBaseModel):
    """Whether the daily visitors could be read, and whether they are counted."""

    status: Literal["ok", "unavailable"] = Field(
        ..., description="`unavailable`: visitors could not be read; the counts still are"
    )
    counting_enabled: bool = Field(
        ..., description="This server counts the visitors servers report with a bound key"
    )


class PublicMcpUsageStatsResponse(TZAwareBaseModel):
    """Usage statistics of the resource's live public MCP endpoint.

    Calls to the resource's public MCP endpoint and, while the server records
    them, REST public searches of its context made with a key bound to it
    (`calls_by_route`). Anonymous REST public searches are not included.
    `calls_by_caller` tells anonymous calls from searches made with a bound
    key. Visitors are distinct values such a caller reported, per UTC day;
    anonymous calls are never visitors. The numbers count what this server
    answered; answers a cache in front of it served are not counted.
    """

    resource_id: str
    endpoint_id: UUID = Field(..., description="The live public endpoint's id")
    window_days: int = Field(..., description="Days covered: the requested days, at most retention")
    first_day: date = Field(..., description="First UTC day of the window")
    last_day: date = Field(..., description="Last UTC day of the window (today, partial)")
    retention_days: int = Field(..., description="How long usage events are kept")
    recording_enabled: bool = Field(..., description="Usage events are being recorded")
    rest_recording_enabled: bool = Field(
        ...,
        description=(
            "REST public searches made with a key bound to the context are being recorded"
        ),
    )
    visitors: PublicMcpUsageStatsVisitors
    totals: PublicMcpUsageStatsCounts
    days: list[PublicMcpUsageStatsDay] = Field(
        ..., description="Every day of the window, oldest first"
    )
    documents: PublicMcpUsageStatsDocuments
    generated_at: datetime


def _counts(counts: DayCounts) -> dict:
    return {
        "calls": counts.calls,
        "searches": counts.searches,
        "search_outcomes": counts.search_outcomes,
        "calls_by_route": counts.calls_by_route,
        "searches_by_route": counts.searches_by_route,
        "searches_answered_by_route": counts.searches_answered_by_route,
        "calls_by_caller": counts.calls_by_caller,
    }


def _stats_response(
    resource_id: str, endpoint_id: UUID, stats: UsageStats
) -> PublicMcpUsageStatsResponse:
    return PublicMcpUsageStatsResponse(
        resource_id=resource_id,
        endpoint_id=endpoint_id,
        window_days=stats.window_days,
        first_day=stats.first_day,
        last_day=stats.last_day,
        retention_days=stats.retention_days,
        recording_enabled=stats.recording_enabled,
        rest_recording_enabled=stats.rest_recording_enabled,
        visitors=PublicMcpUsageStatsVisitors(
            status=stats.visitors_status, counting_enabled=stats.visitor_counting_enabled
        ),
        totals=PublicMcpUsageStatsCounts(**_counts(stats.totals)),
        days=[
            PublicMcpUsageStatsDay(date=day, visitors=counts.visitors, **_counts(counts))
            for day, counts in stats.days
        ],
        documents=PublicMcpUsageStatsDocuments(
            status=stats.documents.status,
            publication_serving=stats.documents.publication_serving,
            searches_scanned=stats.documents.searches_scanned,
            scan_limit=stats.documents.scan_limit,
            scanned_since=stats.documents.scanned_since,
            items=[
                PublicMcpUsageStatsDocument(
                    doc_key=item.doc_key,
                    searches=item.searches,
                    best_rank=item.best_rank,
                    title=item.title,
                    source_url=item.source_url,
                )
                for item in stats.documents.items
            ],
        ),
        generated_at=utcnow(),
    )


@router.get(
    "/{resource_id}/public-mcp/usage-stats",
    response_model=PublicMcpUsageStatsResponse,
)
async def get_public_mcp_usage_stats(
    owner: WorkspaceOwner,
    resource_id: str = Path(
        ...,
        min_length=1,
        max_length=RESOURCE_ID_MAX_LENGTH,
        pattern=RESOURCE_ID_PATTERN,
        description="Resource slug (`setup_resource`'s resource_id).",
    ),
    days: int = Query(
        USAGE_STATS_DEFAULT_DAYS,
        ge=1,
        le=USAGE_STATS_MAX_DAYS,
        description="UTC days to cover, ending today (at most the event retention)",
    ),
    db: AsyncSession = Depends(get_db),
) -> PublicMcpUsageStatsResponse:
    """Calls, searches, visitors and the most returned documents of the live public endpoint.

    Calls of the public MCP endpoint and, while the server records them, REST
    public searches of its context made with a key bound to it; anonymous REST
    public searches are not included. Workspace owner only. An unknown slug, a resource without a live
    endpoint, or a context the credential cannot read are one 404. 429 when
    the workspace read its statistics too often this minute; 503 when the day
    counts could not be read (``Retry-After``).
    """
    user_id, workspace_id = owner
    endpoint = await resolve_stats_endpoint(
        db, user_id=user_id, workspace_id=workspace_id, resource_id=resource_id
    )
    await check_usage_stats_rate_limit(workspace_id)
    settings = get_settings()
    recording, rest_recording, visitor_counting = usage_recording_flags()
    stats = await read_usage_stats(
        db,
        endpoint=endpoint,
        workspace_id=workspace_id,
        days=days,
        retention_days=settings.mcp_public_telemetry_retention_days,
        recording_enabled=recording,
        rest_recording_enabled=rest_recording,
        visitor_counting_enabled=visitor_counting,
    )
    logger.info(
        "public_mcp_usage_stats_read",
        endpoint_id=str(endpoint.id),
        window_days=stats.window_days,
        calls=stats.totals.calls,
        rest_calls=stats.totals.calls_by_route.get(PMU_ROUTE_REST, 0),
        documents=len(stats.documents.items),
        documents_status=stats.documents.status,
        visitors_status=stats.visitors_status,
    )
    return _stats_response(resource_id, endpoint.id, stats)
