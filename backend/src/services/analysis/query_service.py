"""Read-path service for memory analysis runs (Issue #496).

Centralizes the SELECT logic that REST routes (``api/routes/analyses.py``),
MCP tools (``mcp_server/tools/analysis.py``), the ``recall`` filter
extension (``services/memory_service.py``), and the ``/usage/current``
endpoint (``api/routes/usage.py``) all need to share. Putting these in
one module keeps three call sites grep-able and prevents the
"workspace_id boundary check forgotten in one tool" class of leak.

Tenancy invariant: every public method (including the recall filter
helper ``get_memory_ids_in_cluster``) takes ``workspace_id`` and uses
it as the first WHERE filter on ``memory_analyses``. ``run_id`` alone
is never trusted — a stolen run UUID from another workspace returns
None on every code path. ``recall``'s caller passes
``current_workspace_id`` through to ``get_memory_ids_in_cluster`` so
the cross-workspace cluster lookup vector is closed at this layer
even though ``recall`` has already validated workspace context once.

Pagination contract for ``get_cluster``:

- ``limit`` clamped to ``MAX_CLUSTER_PAGE_SIZE`` (200) server-side.
- ``cursor`` is an opaque token; v1 implementation is the last
  ``memory_id`` UUID encoded as a string. Keyset pagination on
  the assignments PK guarantees stable order under concurrent writes
  (assignments are insert-only after the run finishes).
- ``next_cursor`` is ``None`` when the page is the last one.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.analysis import (
    MemoryAnalysis,
    MemoryAnalysisAssignment,
    MemoryAnalysisCluster,
)
from models.memory import Memory
from utils.logger import get_logger

logger = get_logger(__name__)


# Server-enforced clamp for ``get_cluster`` (Issue #496 AC). Keeps any
# single MCP transport frame under ~200 KB even if every memory in the
# cluster has a 500-char summary + tags array.
MAX_CLUSTER_PAGE_SIZE = 200
DEFAULT_CLUSTER_PAGE_SIZE = 50

# Server-enforced clamp for ``list_analyses`` (mirrors the recall paging
# convention so MCP clients can rely on a single behavior).
MAX_LIST_PAGE_SIZE = 100
DEFAULT_LIST_PAGE_SIZE = 20


def _clamp_limit(value: int | None, default: int, max_value: int) -> int:
    """Clamp a caller-supplied ``limit`` parameter."""
    if value is None or value <= 0:
        return default
    return min(value, max_value)


def day_window_utc(user_timezone: str) -> tuple[datetime, datetime]:
    """Return ``(day_start_utc, day_end_utc)`` naive UTC for caller's tz today.

    Centralized so REST gate / MCP gate / ``/usage/current`` and any
    future caller share one tz handling — exotic tz strings safely fall
    back to UTC, DST transitions stay deterministic, and the
    ``replace(tzinfo=None)`` step that asyncpg requires (#489 wire-format
    convention) is applied in exactly one place.

    DST handling: ``day_end_local`` is computed as ``date + 1 day`` at
    local midnight, NOT ``day_start_local + timedelta(days=1)``. With
    ZoneInfo timezones that observe DST, adding 24h in absolute time
    can yield a result that is not the next local midnight (e.g. on
    DST start the +24h would land at 01:00 local). Building from the
    next calendar date keeps the "local calendar day" semantic intact
    so quota counting AND ``resets_at`` align with the user's wall
    clock. Issue #496 Copilot review.
    """
    try:
        tz = ZoneInfo(user_timezone)
    except Exception:
        # Defensive — User.timezone is varchar(50) without a CHECK; an
        # exotic value should not 500 the gate.
        tz = ZoneInfo("UTC")

    now_local = datetime.now(tz)
    day_start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    # Next local midnight via date arithmetic — DST-correct.
    next_date = (day_start_local + timedelta(days=1)).date()
    day_end_local = datetime(
        next_date.year,
        next_date.month,
        next_date.day,
        tzinfo=tz,
    )
    day_start_utc = day_start_local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    day_end_utc = day_end_local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    return day_start_utc, day_end_utc


async def verify_context_in_workspace(
    db: AsyncSession,
    *,
    workspace_id: UUID,
    context_id: UUID,
) -> bool:
    """True iff the context exists and belongs to ``workspace_id``.

    Single source of truth for the boundary check used by REST routes
    (``_verify_context_in_workspace`` raises HTTPException 404) and MCP
    tools (``_verify_context_in_workspace_mcp`` returns an error
    envelope). Both wrappers call this helper so the SELECT shape only
    lives in one place.
    """
    from models.auth import Context

    result = await db.execute(
        select(Context.id).where(
            Context.id == context_id,
            Context.workspace_id == workspace_id,
            Context.deleted_at.is_(None),
        )
    )
    return result.scalar_one_or_none() is not None


def _live_run_boundary_stmt(run_id: UUID, workspace_id: UUID):
    """Boundary SELECT shared by every run_id-keyed reader (#1243).

    Matches iff the run exists, belongs to ``workspace_id``, AND its
    parent context is alive. The Context-liveness join makes deleted-
    context runs structurally invisible to ALL callers: REST already
    404s at its URL-context boundary check, but the run_id-keyed MCP
    tools (``get_analysis`` / ``get_cluster``) reach these readers with
    no context in hand — without the join they served LLM-derived
    labels, descriptions and property_stats of soft-deleted contexts
    indefinitely. Enforcing it in the query keeps the invariant out of
    per-handler checklists (same principle as #1228's schema
    separation: structural invisibility beats remember-to-exclude).
    """
    from models.auth import Context

    return (
        select(MemoryAnalysis.id)
        .join(Context, Context.id == MemoryAnalysis.context_id)
        .where(
            and_(
                MemoryAnalysis.id == run_id,
                MemoryAnalysis.workspace_id == workspace_id,
                Context.deleted_at.is_(None),
            )
        )
    )


async def count_context_memories(
    db: AsyncSession,
    *,
    workspace_id: UUID,
    context_id: UUID,
) -> int:
    """Count non-deleted memories in a context for cost-preview math.

    Shared by REST ``/preview`` and MCP ``analyze_context(dry_run=true)``
    so the count semantics (filters ignored in v1, soft-delete excluded)
    are defined in one place.
    """
    from models.memory import Memory

    stmt = select(func.count(Memory.id)).where(
        and_(
            Memory.workspace_id == workspace_id,
            Memory.context_id == context_id,
            Memory.deleted_at.is_(None),
        )
    )
    return int((await db.execute(stmt)).scalar() or 0)


# ============================================================================
# Run-level reads
# ============================================================================


async def get_analysis(
    db: AsyncSession,
    *,
    workspace_id: UUID,
    run_id: UUID,
) -> MemoryAnalysis | None:
    """Fetch one run by id, scoped to ``workspace_id``.

    Returns None if the run does not exist, belongs to a different
    workspace, OR its parent context has been soft-deleted (#1243) —
    callers convert None into 404 at the API layer so the "exists but
    not yours" and "existed but deleted" paths do not leak existence.
    """
    from models.auth import Context

    stmt = (
        select(MemoryAnalysis)
        .join(Context, Context.id == MemoryAnalysis.context_id)
        .where(
            and_(
                MemoryAnalysis.id == run_id,
                MemoryAnalysis.workspace_id == workspace_id,
                Context.deleted_at.is_(None),
            )
        )
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def list_analyses(
    db: AsyncSession,
    *,
    workspace_id: UUID,
    context_id: UUID,
    limit: int | None = None,
    cursor: str | None = None,
) -> tuple[list[MemoryAnalysis], str | None]:
    """List runs for a context, newest first, with cursor pagination.

    Cursor is the ``started_at`` ISO-8601 of the last item on the
    previous page. Newer runs that arrive between requests appear at
    the top of page 1; the cursor walks backward in time so missing
    them on a subsequent page is intentional (poll page 1 for the
    freshest list).
    """
    page_size = _clamp_limit(limit, DEFAULT_LIST_PAGE_SIZE, MAX_LIST_PAGE_SIZE)

    conditions = [
        MemoryAnalysis.workspace_id == workspace_id,
        MemoryAnalysis.context_id == context_id,
    ]
    if cursor:
        try:
            cursor_dt = datetime.fromisoformat(cursor)
        except ValueError:
            logger.warning("list_analyses_invalid_cursor", cursor=cursor)
        else:
            # Keyset pagination on started_at DESC — strict less-than
            # so the cursor row itself is not duplicated on the next
            # page. ``started_at`` is naive UTC by repo convention.
            if cursor_dt.tzinfo is not None:
                cursor_dt = cursor_dt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
            conditions.append(MemoryAnalysis.started_at < cursor_dt)

    stmt = (
        select(MemoryAnalysis)
        .where(and_(*conditions))
        .order_by(MemoryAnalysis.started_at.desc())
        .limit(page_size + 1)  # peek-one to detect last page
    )
    rows = list((await db.execute(stmt)).scalars().all())

    next_cursor: str | None = None
    if len(rows) > page_size:
        rows = rows[:page_size]
        # Cursor for next page is the started_at of the LAST row we
        # returned, formatted with the project's standard ``Z`` suffix
        # so JS clients that round-trip the cursor through their own
        # parser don't drop the timezone info (#489 wire-format rule).
        from utils.datetime import to_utc_iso

        next_cursor = to_utc_iso(rows[-1].started_at)

    return rows, next_cursor


async def get_active_analysis(
    db: AsyncSession,
    *,
    workspace_id: UUID,
    context_id: UUID,
) -> MemoryAnalysis | None:
    """Return the most recent ``status='succeeded'`` run for a context.

    Returns None when the context has no successful runs yet — callers
    map this to 404. Sorts by ``finished_at DESC`` so a brand-new
    succeeded run wins over an older one even if their ``started_at``
    are out of order (rare, but possible if a long run finishes after
    a quick one started later).
    """
    stmt = (
        select(MemoryAnalysis)
        .where(
            and_(
                MemoryAnalysis.workspace_id == workspace_id,
                MemoryAnalysis.context_id == context_id,
                MemoryAnalysis.status == "succeeded",
            )
        )
        .order_by(MemoryAnalysis.finished_at.desc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


# ============================================================================
# Cluster-level reads
# ============================================================================


async def _resolve_cluster(
    db: AsyncSession,
    *,
    run_id: UUID,
    cluster_index: int,
) -> MemoryAnalysisCluster | None:
    """Look up the cluster row by ``(run_id, cluster_index)`` pair.

    ``cluster_index`` is the user-visible 0-based ordinal assigned by
    KMeans; ``cluster_id`` is the UUID PK. Routes / MCP tools / the
    recall filter all start from ``cluster_index`` so this lookup is
    factored out.
    """
    stmt = select(MemoryAnalysisCluster).where(
        and_(
            MemoryAnalysisCluster.analysis_id == run_id,
            MemoryAnalysisCluster.cluster_index == cluster_index,
        )
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def get_cluster(
    db: AsyncSession,
    *,
    workspace_id: UUID,
    run_id: UUID,
    cluster_index: int,
    limit: int | None = None,
    cursor: str | None = None,
) -> dict[str, Any] | None:
    """Drill-down view: cluster metadata + paginated memory list.

    The workspace boundary check is enforced via a JOIN to
    ``memory_analyses`` rather than a second SELECT: if the run does
    not belong to ``workspace_id``, the join returns 0 rows and the
    function returns None (404 at the API layer).

    Returns a dict shaped to match the MCP tool contract directly so
    REST and MCP can serialize the same payload.
    """
    # Tenant + run validity in a single query — joins ``memory_analyses``
    # to enforce workspace_id boundary before paying for the cluster lookup.
    boundary = (
        await db.execute(_live_run_boundary_stmt(run_id, workspace_id))
    ).scalar_one_or_none()
    if boundary is None:
        return None

    cluster = await _resolve_cluster(db, run_id=run_id, cluster_index=cluster_index)
    if cluster is None:
        return None

    page_size = _clamp_limit(limit, DEFAULT_CLUSTER_PAGE_SIZE, MAX_CLUSTER_PAGE_SIZE)

    # Page through the cluster's memories. Keyset on memory_id (UUID
    # textual order) — opaque cursor that the client should not parse.
    mem_conditions = [
        MemoryAnalysisAssignment.analysis_id == run_id,
        MemoryAnalysisAssignment.cluster_id == cluster.id,
    ]
    if cursor:
        try:
            cursor_uuid = UUID(cursor)
        except ValueError:
            logger.warning("get_cluster_invalid_cursor", cursor=cursor)
        else:
            mem_conditions.append(MemoryAnalysisAssignment.memory_id > cursor_uuid)

    page_stmt = (
        select(
            Memory.id,
            Memory.summary,
            Memory.tags,
            Memory.importance,
        )
        .join(
            MemoryAnalysisAssignment,
            MemoryAnalysisAssignment.memory_id == Memory.id,
        )
        .where(
            and_(
                *mem_conditions,
                Memory.deleted_at.is_(None),
                # Defense-in-depth — ``memory_analysis_assignments.memory_id``
                # FKs to ``memories.id`` only (no workspace constraint at the
                # DB level). If a future bug ever assigns a foreign-workspace
                # memory_id into a cluster (e.g. via direct SQL or a
                # repair-script gone wrong), this predicate would still keep
                # the API tenancy invariant. Issue #496 Copilot review.
                Memory.workspace_id == workspace_id,
            )
        )
        .order_by(MemoryAnalysisAssignment.memory_id.asc())
        .limit(page_size + 1)
    )
    rows = list((await db.execute(page_stmt)).all())

    next_cursor: str | None = None
    if len(rows) > page_size:
        rows = rows[:page_size]
        next_cursor = str(rows[-1].id)

    memories_out = [
        {
            "memory_id": str(row.id),
            "summary": row.summary,
            "tags": list(row.tags) if row.tags else [],
            "importance": float(row.importance) if row.importance is not None else None,
        }
        for row in rows
    ]

    # ``representative_memory_ids`` is an ARRAY(UUID) on the cluster row
    # (already capped at 5 by the labeler). Filter out stale UUIDs whose
    # memories were soft-deleted post-analysis (model docstring guard).
    rep_ids = list(cluster.representative_memory_ids or [])
    if rep_ids:
        rep_rows = (
            await db.execute(
                select(Memory.id, Memory.summary, Memory.tags, Memory.importance).where(
                    and_(
                        Memory.id.in_(rep_ids),
                        Memory.deleted_at.is_(None),
                        # Same defense-in-depth predicate as the page query
                        # above — Issue #496 Copilot review.
                        Memory.workspace_id == workspace_id,
                    )
                )
            )
        ).all()
        # Preserve the order from ``representative_memory_ids`` so the UI
        # gets stable "top-k" semantics across repeated calls.
        rep_by_id = {r.id: r for r in rep_rows}
        representatives = [
            {
                "memory_id": str(rid),
                "summary": rep_by_id[rid].summary,
                "tags": list(rep_by_id[rid].tags) if rep_by_id[rid].tags else [],
                "importance": (
                    float(rep_by_id[rid].importance)
                    if rep_by_id[rid].importance is not None
                    else None
                ),
            }
            for rid in rep_ids
            if rid in rep_by_id
        ]
    else:
        representatives = []

    return {
        "run_id": str(run_id),
        "cluster_index": cluster_index,
        "cluster_id": str(cluster.id),
        "label": cluster.label,
        "description": cluster.description,
        "count": int(cluster.count),
        "label_confidence": float(cluster.label_confidence),
        "centroid_2d": list(cluster.centroid_2d) if cluster.centroid_2d else None,
        "property_stats": cluster.property_stats or {},
        "representatives": representatives,
        "memories": memories_out,
        "next_cursor": next_cursor,
    }


async def list_clusters(
    db: AsyncSession,
    *,
    workspace_id: UUID,
    run_id: UUID,
) -> list[MemoryAnalysisCluster] | None:
    """Return every cluster row for a run, ordered by ``cluster_index``.

    Used by the REST surface in #497 to render the cluster list, scatter
    centroids, and per-cluster property stats. The MCP tool
    ``get_cluster`` already paginates the *member* memories of a single
    cluster; this helper is the complementary "list all clusters of a
    run" read that the frontend needs to bootstrap the scatter view.

    Tenancy invariant matches ``get_cluster``: workspace boundary is
    enforced by checking ``MemoryAnalysis.workspace_id == workspace_id``
    BEFORE fetching cluster rows. Returns:

    - ``None`` when the run does not exist or belongs to a foreign
      workspace (callers map to 404 — same disclosure shape as the
      run-level read).
    - ``[]`` when the run exists but has no cluster rows yet (a still-
      running or failed run that never completed the labeler stage).
    - ``list[MemoryAnalysisCluster]`` ordered by ``cluster_index ASC``
      otherwise. Cluster count is bounded by
      ``ceil(sqrt(memory_count))`` ≈ 90 clusters on an 8000-memory run,
      so no pagination is offered.
    """
    boundary = (
        await db.execute(_live_run_boundary_stmt(run_id, workspace_id))
    ).scalar_one_or_none()
    if boundary is None:
        return None

    stmt = (
        select(MemoryAnalysisCluster)
        .where(MemoryAnalysisCluster.analysis_id == run_id)
        .order_by(MemoryAnalysisCluster.cluster_index.asc())
    )
    return list((await db.execute(stmt)).scalars().all())


async def list_positions(
    db: AsyncSession,
    *,
    workspace_id: UUID,
    run_id: UUID,
) -> list[dict[str, Any]] | None:
    """Return every ``(memory_id, x, y, cluster_index)`` for the scatter plot.

    Joins ``memory_analysis_assignments`` → ``memory_analysis_clusters``
    so the frontend can render dots colored by ``cluster_index`` without
    a second round-trip per cluster. Sorted by
    ``(cluster_index, memory_id)`` for deterministic z-order.

    Tenancy invariant: ``workspace_id`` is verified via the
    ``memory_analyses`` row before any assignment SELECT. A stolen
    run UUID from another workspace returns ``None``.

    Payload sizing: each row is ~80 bytes JSON; an 8000-memory run
    produces ~640 KB which is acceptable for an analyses UI page that
    the operator explicitly opens (not on hover or background poll).
    No pagination is offered for v1 — split if a run ever exceeds 50k
    memories.

    Returns:

    - ``None`` if the run is foreign / unknown (callers map to 404).
    - ``[]`` if the run exists but has no assignments (still running).
    - ``list[dict]`` of ``{memory_id, x, y, cluster_index}`` otherwise.
    """
    boundary = (
        await db.execute(_live_run_boundary_stmt(run_id, workspace_id))
    ).scalar_one_or_none()
    if boundary is None:
        return None

    # Filter out soft-deleted memories so the scatter does not render
    # orphan dots for memories the user has since forgotten — matches
    # ``get_cluster``'s ``Memory.deleted_at.is_(None)`` discipline so
    # the two read paths agree on which assignments are visible.
    # ``Memory.workspace_id == workspace_id`` is defense-in-depth: the
    # boundary was already verified above, but if a future repair
    # script ever inserts a foreign-workspace ``memory_id`` into an
    # assignment row this predicate would still keep tenancy intact.
    stmt = (
        select(
            MemoryAnalysisAssignment.memory_id,
            MemoryAnalysisAssignment.x,
            MemoryAnalysisAssignment.y,
            MemoryAnalysisCluster.cluster_index,
        )
        .join(
            MemoryAnalysisCluster,
            MemoryAnalysisCluster.id == MemoryAnalysisAssignment.cluster_id,
        )
        .join(
            Memory,
            Memory.id == MemoryAnalysisAssignment.memory_id,
        )
        .where(
            and_(
                MemoryAnalysisAssignment.analysis_id == run_id,
                Memory.deleted_at.is_(None),
                Memory.workspace_id == workspace_id,
            )
        )
        .order_by(
            MemoryAnalysisCluster.cluster_index.asc(),
            MemoryAnalysisAssignment.memory_id.asc(),
        )
    )
    rows = (await db.execute(stmt)).all()
    return [
        {
            "memory_id": str(memory_id),
            "x": float(x),
            "y": float(y),
            "cluster_index": int(cluster_index),
        }
        for (memory_id, x, y, cluster_index) in rows
    ]


async def get_memory_ids_in_cluster(
    db: AsyncSession,
    *,
    workspace_id: UUID,
    run_id: UUID,
    cluster_index: int,
) -> list[UUID] | None:
    """Return the memory_ids assigned to ``(run_id, cluster_index)``.

    Used by the ``recall(filters={"analysis_cluster": ...})`` filter
    chain in ``services/memory_service.py``.

    **Tenancy invariant**: ``workspace_id`` is required and verified
    via ``MemoryAnalysis.workspace_id == workspace_id``. A run UUID
    that belongs to a foreign workspace returns ``None`` — same shape
    as "cluster not found" so the recall filter degrades to "0
    results" without leaking that the run exists somewhere else.
    Closes the cross-workspace cluster lookup vector flagged in the
    Issue #496 security review.

    Returns:

    - ``None`` if the run is in a foreign workspace, the run is
      unknown, OR the cluster_index is unknown for that run.
    - ``[]`` (empty list) if the cluster exists but contains no
      memories — distinct from None for unit-test clarity.
    - ``list[UUID]`` of all assignments otherwise, no pagination.
      Cluster sizes are bounded by ``ceil(sqrt(memory_count))`` ≈
      90 memories per cluster on an 8000-memory run, well under any
      practical IN-list limit.
    """
    # Tenant boundary: ensure the run belongs to caller's workspace
    # BEFORE resolving the cluster_index (so a stolen run_id from a
    # foreign workspace is indistinguishable from a typo).
    boundary = (
        await db.execute(_live_run_boundary_stmt(run_id, workspace_id))
    ).scalar_one_or_none()
    if boundary is None:
        return None

    cluster = await _resolve_cluster(db, run_id=run_id, cluster_index=cluster_index)
    if cluster is None:
        return None

    stmt = select(MemoryAnalysisAssignment.memory_id).where(
        and_(
            MemoryAnalysisAssignment.analysis_id == run_id,
            MemoryAnalysisAssignment.cluster_id == cluster.id,
        )
    )
    return [row for (row,) in (await db.execute(stmt)).all()]


# ============================================================================
# /usage support
# ============================================================================


async def get_today_analysis_count(
    db: AsyncSession,
    *,
    workspace_id: UUID,
    user_timezone: str,
) -> int:
    """Count analyses started today (in caller's tz).

    Mirrors the count semantics of
    ``auth.analysis_gates.check_memory_analysis_quota`` (cancelled
    rows count, day boundary follows caller's tz). The quota gate
    raises 429; this helper just returns the integer for the
    ``/usage/current`` ``analysis.used_today`` field.
    """
    day_start_utc, day_end_utc = day_window_utc(user_timezone)
    stmt = select(func.count(MemoryAnalysis.id)).where(
        and_(
            MemoryAnalysis.workspace_id == workspace_id,
            MemoryAnalysis.started_at >= day_start_utc,
            MemoryAnalysis.started_at < day_end_utc,
        )
    )
    return int((await db.execute(stmt)).scalar() or 0)
