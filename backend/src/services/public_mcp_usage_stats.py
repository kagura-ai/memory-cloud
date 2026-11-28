"""Owner statistics of a public MCP endpoint, from its usage events (#3035).

``GET /api/v1/resources/{resource_id}/public-mcp/usage-stats`` reads the
usage events of the resource's live endpoint: calls, searches and search
outcomes per UTC day, by route (the public MCP endpoint; REST public searches
of the context made with a key bound to it, while the server records them)
and by caller (anonymous, or a bound key), the distinct visitors such a key
reported per UTC day, and the documents searches returned most often.
Anonymous REST public searches are never recorded, so never counted here.
Nothing is written.

Bounds: one wall-clock budget for the whole read (no step starts after it);
before each step a transaction-local ``statement_timeout`` of the smaller of
``USAGE_STATS_STATEMENT_TIMEOUT_MS`` and the time left; each section runs
read-only inside its own savepoint, so a failure in it leaves the session
usable; the document scan reads at most ``USAGE_STATS_DOCUMENT_SCAN_LIMIT``
events; reads are counted per workspace and minute.

The day counts are the answer: when they cannot be read the read fails
(``PublicMcpUsageStatsUnavailableError``, 503). The visitor and document
sections are optional: when one fails or the budget is spent it is
``unavailable`` and the counts are still returned. Visitors of separate days
are never added up: a distinct count per day is not a count of people. Titles and URLs appear only while the endpoint
serves right now, through the resolver the observability API uses
(``resolve_public_documents``): what an anonymous caller could read.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from datetime import time as day_time
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import get_settings
from db.redis import increment_counter
from mcp_server.public.publication import load_serving_endpoint, serving_paused
from models.public_mcp_usage import (
    PMU_CALLER_ANONYMOUS,
    PMU_CALLERS,
    PMU_OUTCOMES,
    PMU_ROUTE_MCP,
    PMU_ROUTES,
    PMU_SEARCH_TOOL,
)
from services.observability_documents import resolve_public_documents
from services.permission_service import CapacityPolicy, PermissionService
from utils.datetime import utcnow
from utils.exceptions import (
    NotFoundException,
    PublicMcpUsageStatsRateLimitError,
    PublicMcpUsageStatsUnavailableError,
)
from utils.logger import get_logger

logger = get_logger(__name__)

USAGE_STATS_DEFAULT_DAYS = 7
USAGE_STATS_MAX_DAYS = 90
USAGE_STATS_TOP_DOCUMENTS = 10
USAGE_STATS_DOCUMENT_SCAN_LIMIT = 20_000
USAGE_STATS_STATEMENT_TIMEOUT_MS = 5_000
USAGE_STATS_BUDGET_SECONDS = 6.0
USAGE_STATS_RATE_LIMIT_PER_MINUTE = 30

# A search the endpoint answered (the base of the documents and visitors).
_ANSWERED = frozenset({"success", "degraded"})

_LIVE_ENDPOINT = text(
    "SELECT id, public_id, resource_pk, context_id FROM public_mcp_endpoints "
    "WHERE workspace_id = :workspace_id AND resource_id = :slug AND revoked_at IS NULL"
)
_READ_ONLY = text("SET TRANSACTION READ ONLY")
_STATEMENT_TIMEOUT = text("SELECT set_config('statement_timeout', :ms, true)")
# Route and caller: NULL (a row written before f67) is a public MCP call by
# an anonymous caller; both defaults are bound, never written in the SQL.
DAY_COUNTS = text(
    "SELECT (occurred_at)::date AS day, tool, outcome, "
    "COALESCE(route, :default_route) AS route, "
    "COALESCE(caller, :default_caller) AS caller, "
    "count(*) AS calls "
    "FROM public_mcp_usage_events "
    "WHERE endpoint_id = :endpoint_id AND workspace_id = :workspace_id "
    "AND kind = 'tool_call' AND occurred_at >= :first_day_start "
    "GROUP BY 1, 2, 3, 4, 5"
)
VISITORS_BY_DAY = text(
    "SELECT day, count(*) AS visitors FROM public_mcp_daily_visitors "
    "WHERE endpoint_id = :endpoint_id AND workspace_id = :workspace_id AND day >= :first_day "
    "GROUP BY day"
)
TOP_DOCUMENTS = text(
    "WITH recent AS ("
    " SELECT documents, occurred_at FROM public_mcp_usage_events"
    " WHERE endpoint_id = :endpoint_id AND workspace_id = :workspace_id"
    " AND kind = 'tool_call' AND tool = :search_tool AND outcome IN ('success', 'degraded')"
    " AND occurred_at >= :first_day_start"
    " AND documents IS NOT NULL AND jsonb_typeof(documents) = 'array'"
    " ORDER BY occurred_at DESC"
    " LIMIT :scan_limit"
    "), top AS ("
    " SELECT d.doc_key, count(*) AS searches, min(d.rank) AS best_rank"
    " FROM recent, jsonb_to_recordset(recent.documents) AS d(doc_key text, rank int)"
    " WHERE d.doc_key IS NOT NULL"
    " GROUP BY d.doc_key"
    " ORDER BY searches DESC, best_rank ASC, d.doc_key ASC"
    " LIMIT :top"
    ") "
    "SELECT s.scanned, s.scanned_since, top.doc_key, top.searches, top.best_rank "
    "FROM (SELECT count(*) AS scanned, min(occurred_at) AS scanned_since FROM recent) AS s "
    "LEFT JOIN top ON true "
    "ORDER BY top.searches DESC NULLS LAST, top.best_rank ASC, top.doc_key ASC"
)


@dataclass(frozen=True)
class LiveEndpoint:
    """The resource's live public endpoint, as plain columns."""

    id: UUID
    public_id: str
    resource_pk: UUID
    context_id: UUID


@dataclass
class DayCounts:
    """Calls, searches and search outcomes of one UTC day (or of the window).

    ``searches`` counts every recorded search whatever its outcome;
    ``searches_answered_by_route`` only the answered ones (success or
    degraded), the base the documents and visitors share. ``visitors`` is set
    on days only (no window total), ``None`` when it could not be read.
    """

    calls: int = 0
    searches: int = 0
    search_outcomes: dict[str, int] = field(default_factory=lambda: dict.fromkeys(PMU_OUTCOMES, 0))
    calls_by_route: dict[str, int] = field(default_factory=lambda: dict.fromkeys(PMU_ROUTES, 0))
    searches_by_route: dict[str, int] = field(default_factory=lambda: dict.fromkeys(PMU_ROUTES, 0))
    searches_answered_by_route: dict[str, int] = field(
        default_factory=lambda: dict.fromkeys(PMU_ROUTES, 0)
    )
    calls_by_caller: dict[str, int] = field(default_factory=lambda: dict.fromkeys(PMU_CALLERS, 0))
    visitors: int | None = None


@dataclass(frozen=True)
class DocumentItem:
    """One of the most returned documents."""

    doc_key: str
    searches: int
    best_rank: int | None
    title: str | None
    source_url: str | None


@dataclass(frozen=True)
class DocumentStats:
    """The most returned documents, or ``unavailable``."""

    status: Literal["ok", "unavailable"]
    publication_serving: bool
    searches_scanned: int
    scan_limit: int
    items: list[DocumentItem]
    # The oldest search the list is based on; None when none was scanned.
    scanned_since: datetime | None = None


@dataclass(frozen=True)
class UsageStats:
    """What the route returns, before it becomes the response model."""

    window_days: int
    first_day: date
    last_day: date
    retention_days: int
    recording_enabled: bool
    rest_recording_enabled: bool
    visitor_counting_enabled: bool
    visitors_status: Literal["ok", "unavailable"]
    totals: DayCounts
    days: list[tuple[date, DayCounts]]
    documents: DocumentStats


class _BudgetSpent(Exception):
    """No time was left before a step; the step did not start."""


# ============================================================================
# Authorization (after ``WorkspaceOwner``) and the rate bucket
# ============================================================================


async def resolve_stats_endpoint(
    db: AsyncSession, *, user_id: str, workspace_id: UUID, resource_id: str
) -> LiveEndpoint:
    """The live endpoint of ``resource_id`` in the owner's workspace, or the one 404.

    Steps 2 and 3 of the route's chain: the live endpoint of the workspace
    ``WorkspaceOwner`` verified, then the context read chokepoint (private
    context: creator only; agent-bound key: its binding at read access; the
    workspace confinement). Every refusal is the same ``Public MCP endpoint
    not found``; any other error of the chokepoint passes unchanged.
    """
    row = (
        await db.execute(_LIVE_ENDPOINT, {"workspace_id": workspace_id, "slug": resource_id})
    ).first()
    if row is None:
        raise NotFoundException("Public MCP endpoint")
    try:
        await PermissionService(db).resolve_context_for_workspace_read(
            user_id,
            row.context_id,
            capacity=CapacityPolicy.ALLOWED,
            key_workspace_id=workspace_id,
        )
    except NotFoundException:
        raise NotFoundException("Public MCP endpoint") from None
    return LiveEndpoint(
        id=row.id, public_id=row.public_id, resource_pk=row.resource_pk, context_id=row.context_id
    )


def _rate_key(workspace_id: UUID) -> str:
    return f"pmcp_usage_stats_rl:{workspace_id}:{utcnow():%Y%m%d%H%M}"


async def check_usage_stats_rate_limit(workspace_id: UUID) -> None:
    """Count one read against the workspace's minute bucket (429 past the limit).

    Called after the authorization chain, so a refused caller never spends
    it. A store failure serves the read (the caller is an authenticated owner
    and every read has its own time budget) and logs the error class only.
    """
    try:
        count = await increment_counter(_rate_key(workspace_id), ttl=60)
    except Exception as exc:
        logger.warning(
            "public_mcp_usage_stats_rate_limit_unavailable", error_type=type(exc).__name__
        )
        return
    if count > USAGE_STATS_RATE_LIMIT_PER_MINUTE:
        raise PublicMcpUsageStatsRateLimitError()


# ============================================================================
# Pure assembly
# ============================================================================


def window(days: int, retention_days: int, today: date) -> tuple[int, date, datetime]:
    """``(window_days, first_day, first_day_start)``; the start is a naive UTC datetime.

    The events' ``occurred_at`` is a timestamp without time zone (naive UTC).
    """
    window_days = max(1, min(days, retention_days))
    first_day = today - timedelta(days=window_days - 1)
    return window_days, first_day, datetime.combine(first_day, day_time.min)


def usage_recording_flags() -> tuple[bool, bool, bool]:
    """``(recording_enabled, rest_recording_enabled, visitor_counting_enabled)``.

    One reading of the switches for every statistics response (the owner
    route and any route that nests its body), so they cannot disagree. One
    switch governs REST recording and visitor counting together.
    """
    settings = get_settings()
    recording = bool(settings.mcp_public_telemetry_enabled)
    rest = recording and bool(settings.public_search_telemetry_enabled)
    return recording, rest, rest


def assemble_days(
    rows: Sequence[Any], first_day: date, window_days: int
) -> tuple[list[tuple[date, DayCounts]], DayCounts]:
    """Zero-filled days (oldest first) and totals.

    From ``(day, tool, outcome, route, caller, calls)`` rows; a route or
    caller outside the stored vocabularies counts as a public MCP call by an
    anonymous caller.
    """
    days = [(first_day + timedelta(days=i), DayCounts()) for i in range(window_days)]
    by_day = dict(days)
    totals = DayCounts()
    for day, tool, outcome, route, caller, calls in rows:
        counts = by_day.get(day)
        if counts is None:
            continue
        calls = int(calls)
        route = route if route in PMU_ROUTES else PMU_ROUTE_MCP
        caller = caller if caller in PMU_CALLERS else PMU_CALLER_ANONYMOUS
        for target in (counts, totals):
            target.calls += calls
            target.calls_by_route[route] += calls
            target.calls_by_caller[caller] += calls
            if tool == PMU_SEARCH_TOOL:
                target.searches += calls
                target.searches_by_route[route] += calls
                if outcome in target.search_outcomes:
                    target.search_outcomes[outcome] += calls
                if outcome in _ANSWERED:
                    target.searches_answered_by_route[route] += calls
    return days, totals


# ============================================================================
# The read
# ============================================================================


def _unavailable_documents() -> DocumentStats:
    return DocumentStats(
        status="unavailable",
        publication_serving=False,
        searches_scanned=0,
        scan_limit=USAGE_STATS_DOCUMENT_SCAN_LIMIT,
        items=[],
    )


async def read_usage_stats(
    db: AsyncSession,
    *,
    endpoint: LiveEndpoint,
    workspace_id: UUID,
    days: int,
    retention_days: int,
    recording_enabled: bool,
    rest_recording_enabled: bool,
    visitor_counting_enabled: bool,
    budget_seconds: float = USAGE_STATS_BUDGET_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> UsageStats:
    """Read the endpoint's statistics within one time budget; nothing is written.

    Raises:
        PublicMcpUsageStatsUnavailableError: The day counts could not be read.
    """
    deadline = clock() + budget_seconds

    def time_left_ms() -> int:
        """The next step's statement timeout; no step starts once the budget is spent."""
        left = deadline - clock()
        if left <= 0:
            raise _BudgetSpent
        return max(1, min(USAGE_STATS_STATEMENT_TIMEOUT_MS, int(left * 1000)))

    async def step(ms: int | None = None) -> None:
        ms = time_left_ms() if ms is None else ms
        await db.execute(_STATEMENT_TIMEOUT, {"ms": str(ms)})

    async def section_start() -> None:
        """Read-only until the savepoint ends; the first step's timeout."""
        ms = time_left_ms()
        await db.execute(_READ_ONLY)
        await step(ms)

    window_days, first_day, first_day_start = window(days, retention_days, utcnow().date())
    scope = {
        "endpoint_id": endpoint.id,
        "workspace_id": workspace_id,
        "first_day_start": first_day_start,
    }

    # A. Day counts: the answer itself.
    try:
        time_left_ms()
        async with db.begin_nested():
            await section_start()
            rows = (
                await db.execute(
                    DAY_COUNTS,
                    {
                        **scope,
                        "default_route": PMU_ROUTE_MCP,
                        "default_caller": PMU_CALLER_ANONYMOUS,
                    },
                )
            ).all()
    except Exception as exc:
        logger.warning("public_mcp_usage_stats_counts_failed", error_type=type(exc).__name__)
        raise PublicMcpUsageStatsUnavailableError() from None
    day_list, totals = assemble_days(rows, first_day, window_days)

    # V. Visitors per day: optional; on any failure every day is None.
    visitors_status: Literal["ok", "unavailable"] = "unavailable"
    try:
        time_left_ms()
        async with db.begin_nested():
            await section_start()
            visitor_rows = (
                await db.execute(
                    VISITORS_BY_DAY,
                    {
                        "endpoint_id": endpoint.id,
                        "workspace_id": workspace_id,
                        "first_day": first_day,
                    },
                )
            ).all()
        by_day = {day: int(n) for day, n in visitor_rows}
        for day, counts in day_list:
            counts.visitors = by_day.get(day, 0)
        visitors_status = "ok"
    except Exception as exc:
        logger.warning("public_mcp_usage_stats_visitors_unavailable", error_type=type(exc).__name__)
        for _, counts in day_list:
            counts.visitors = None

    # B. Documents: optional; on any failure the counts are still returned.
    documents = _unavailable_documents()
    try:
        time_left_ms()
        async with db.begin_nested():
            await section_start()
            top = (
                await db.execute(
                    TOP_DOCUMENTS,
                    {
                        **scope,
                        "search_tool": PMU_SEARCH_TOOL,
                        "scan_limit": USAGE_STATS_DOCUMENT_SCAN_LIMIT,
                        "top": USAGE_STATS_TOP_DOCUMENTS,
                    },
                )
            ).all()
            scanned = int(top[0].scanned) if top else 0
            scanned_since = top[0].scanned_since if top and scanned else None
            ranked = [r for r in top if r.doc_key is not None]
            await step()
            serving = await load_serving_endpoint(db, endpoint.public_id)
            serving_now = (
                serving is not None
                and serving.id == endpoint.id
                and not await serving_paused(db, serving)
            )
            resolved: dict[str, dict[str, str | None] | None] = {}
            if serving is not None and serving_now and ranked:
                await step()
                resolved = await resolve_public_documents(db, serving, [r.doc_key for r in ranked])
            items = []
            for r in ranked:
                shown = resolved.get(r.doc_key) if serving_now else None
                items.append(
                    DocumentItem(
                        doc_key=r.doc_key,
                        searches=int(r.searches),
                        best_rank=None if r.best_rank is None else int(r.best_rank),
                        title=shown["title"] if shown else None,
                        source_url=shown["source_url"] if shown else None,
                    )
                )
            documents = DocumentStats(
                status="ok",
                publication_serving=serving_now,
                searches_scanned=scanned,
                scan_limit=USAGE_STATS_DOCUMENT_SCAN_LIMIT,
                items=items,
                scanned_since=scanned_since,
            )
    except Exception as exc:
        logger.warning(
            "public_mcp_usage_stats_documents_unavailable", error_type=type(exc).__name__
        )
        documents = _unavailable_documents()

    return UsageStats(
        window_days=window_days,
        first_day=first_day,
        last_day=first_day + timedelta(days=window_days - 1),
        retention_days=retention_days,
        recording_enabled=recording_enabled,
        rest_recording_enabled=rest_recording_enabled,
        visitor_counting_enabled=visitor_counting_enabled,
        visitors_status=visitors_status,
        totals=totals,
        days=day_list,
        documents=documents,
    )
