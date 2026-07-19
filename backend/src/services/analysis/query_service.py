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

from sqlalchemy import and_, func, or_, select
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
                # Copilot review: also pin the CONTEXT's workspace — the
                # run row's workspace_id alone would treat a run whose
                # context_id drifted into another workspace (partial
                # corruption / bad backfill) as live. Matches
                # verify_context_in_workspace's predicate set.
                Context.workspace_id == workspace_id,
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
    stmt = select(func.count(Memory.id)).where(
        and_(*_live_context_count_conditions(workspace_id=workspace_id, context_id=context_id))
    )
    return int((await db.execute(stmt)).scalar() or 0)


def _live_context_count_conditions(*, workspace_id: UUID, context_id: UUID) -> list[Any]:
    """Base WHERE set shared by BOTH count lanes (#1366).

    The cap-lane count and the agent-visible count must differ ONLY by
    the binding predicate — defining the live-row membership here keeps
    that guaranteed when the base semantics evolve.
    """
    return [
        Memory.workspace_id == workspace_id,
        Memory.context_id == context_id,
        Memory.deleted_at.is_(None),
    ]


async def count_context_memories_binding_visible(
    db: AsyncSession,
    *,
    workspace_id: UUID,
    context_id: UUID,
) -> int:
    """Binding-subtracted count for agent-facing response payloads (#1366).

    ``count_context_memories`` deliberately stays the TRUE full-context
    total: the #1244 run-size cap must bound the run the pipeline would
    actually execute, and a binding-scoped cap count would let an
    enforce agent start an over-cap run. This variant applies the #1301
    SQL predicate on top of the same conditions, so the count an enforce
    agent *sees* (dry_run/preview ``memory_count`` and the estimate
    derived from it) never acts as an existence oracle over denied rows.
    Non-agent and shadow scopes get the true count (predicate is None).
    """
    from services.agent_binding_service import binding_memory_sql_predicate

    predicate = await binding_memory_sql_predicate(db)
    conditions = _live_context_count_conditions(workspace_id=workspace_id, context_id=context_id)
    if predicate is not None:
        conditions.append(predicate)
    stmt = select(func.count(Memory.id)).where(and_(*conditions))
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
                # Same context-workspace pin as _live_run_boundary_stmt.
                Context.workspace_id == workspace_id,
                Context.deleted_at.is_(None),
            )
        )
    )
    return (await db.execute(stmt)).scalar_one_or_none()


def _decode_list_cursor(cursor: str) -> tuple[datetime | None, UUID | None]:
    """Decode a ``list_analyses`` keyset cursor into ``(started_at, id)``.

    The compound cursor (#1247) is ``"<started_at_iso>|<run_id>"`` — the
    ``id`` tiebreaker makes the keyset stable when several runs share an
    identical ``started_at``, so no row straddling a page boundary is
    skipped. ``started_at`` is normalized to naive UTC (repo convention).

    Back-compat: a legacy cursor (bare ISO ``started_at``, pre-#1247, no
    ``|`` separator) decodes with ``id=None`` so tokens issued before this
    change still page (the caller falls back to the strict ``started_at``
    predicate). Returns ``(None, None)`` when the token is unparseable —
    including a compound cursor whose ``|`` separator is present but whose
    UUID tail is malformed (a corrupt/tampered token, NOT a legacy one).
    """
    # ``to_utc_iso`` never emits ``|`` and a UUID never contains one, so
    # splitting on the LAST ``|`` cleanly separates the two components.
    head, sep, tail = cursor.rpartition("|")
    dt_str = head if sep else cursor
    id_str = tail if sep else ""

    try:
        cursor_dt = datetime.fromisoformat(dt_str)
    except ValueError:
        return None, None
    if cursor_dt.tzinfo is not None:
        cursor_dt = cursor_dt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)

    cursor_id: UUID | None = None
    if sep:
        # A compound cursor MUST carry a valid UUID tail. A malformed
        # suffix means a corrupt/tampered token — flag the whole cursor
        # invalid (same as an unparseable datetime) rather than silently
        # downgrading to the started_at-only predicate, which would
        # reintroduce boundary-skipped rows for tied ``started_at`` values.
        try:
            cursor_id = UUID(id_str)
        except ValueError:
            return None, None
    return cursor_dt, cursor_id


async def list_analyses(
    db: AsyncSession,
    *,
    workspace_id: UUID,
    context_id: UUID,
    limit: int | None = None,
    cursor: str | None = None,
) -> tuple[list[MemoryAnalysis], str | None]:
    """List runs for a context, newest first, with cursor pagination.

    Cursor is the compound ``"<started_at_iso>|<run_id>"`` of the last
    item on the previous page (#1247). ``started_at`` alone is NOT unique
    — several runs (e.g. a burst of scheduled analyses) can share the
    same second — so a strict ``started_at`` cursor would silently skip
    runs that straddle a page boundary. Ordering and the cursor predicate
    are therefore a compound key ``(started_at, id)`` DESC.

    Newer runs that arrive between requests appear at the top of page 1;
    the cursor walks backward through ``(started_at, id)`` so missing them
    on a subsequent page is intentional (poll page 1 for the freshest
    list).
    """
    page_size = _clamp_limit(limit, DEFAULT_LIST_PAGE_SIZE, MAX_LIST_PAGE_SIZE)

    conditions = [
        MemoryAnalysis.workspace_id == workspace_id,
        MemoryAnalysis.context_id == context_id,
    ]
    if cursor:
        cursor_dt, cursor_id = _decode_list_cursor(cursor)
        if cursor_dt is None:
            logger.warning("list_analyses_invalid_cursor", cursor=cursor)
        elif cursor_id is None:
            # Legacy started_at-only cursor (pre-#1247): strict less-than
            # so the cursor row itself is not duplicated on the next page.
            conditions.append(MemoryAnalysis.started_at < cursor_dt)
        else:
            # Compound keyset on (started_at, id) DESC: everything strictly
            # "older" than the cursor tuple. The id tiebreaker keeps runs
            # sharing ``cursor_dt`` from being skipped across the boundary.
            conditions.append(
                or_(
                    MemoryAnalysis.started_at < cursor_dt,
                    and_(
                        MemoryAnalysis.started_at == cursor_dt,
                        MemoryAnalysis.id < cursor_id,
                    ),
                )
            )

    stmt = (
        select(MemoryAnalysis)
        .where(and_(*conditions))
        .order_by(MemoryAnalysis.started_at.desc(), MemoryAnalysis.id.desc())
        .limit(page_size + 1)  # peek-one to detect last page
    )
    rows = list((await db.execute(stmt)).scalars().all())

    next_cursor: str | None = None
    if len(rows) > page_size:
        rows = rows[:page_size]
        # Cursor for next page is the compound (started_at, id) of the LAST
        # row we returned. ``started_at`` uses the project's standard ``Z``
        # suffix so JS clients that round-trip the cursor through their own
        # parser don't drop the timezone info (#489 wire-format rule).
        from utils.datetime import to_utc_iso

        last = rows[-1]
        next_cursor = f"{to_utc_iso(last.started_at)}|{last.id}"

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

    # #1301/#1357: one bulk binding SELECT per request; None for non-agent
    # credentials and shadow scopes. Encoding the subtraction in the page
    # WHERE (rather than post-filtering fetched rows) is what keeps
    # ``next_cursor`` from ever naming a denied row (existence oracle,
    # CWE-639) and keeps pagination advancing without fetching denied rows.
    from services.agent_binding_service import binding_memory_sql_predicate

    binding_predicate = await binding_memory_sql_predicate(db)

    # Page through the cluster's memories. Keyset on memory_id (UUID
    # textual order) — opaque cursor that the client should not parse.
    mem_conditions = [
        MemoryAnalysisAssignment.analysis_id == run_id,
        MemoryAnalysisAssignment.cluster_id == cluster.id,
    ]
    if binding_predicate is not None:
        mem_conditions.append(binding_predicate)
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
            # #1301: the row-filter lever below needs context_id/type/
            # source_type on each row; they never reach the response.
            Memory.context_id,
            Memory.type,
            Memory.source_type,
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

    # #1301: enforce-mode subtraction now happens in the page WHERE above
    # (#1357) — running the row filter too would only re-fetch the bindings
    # to drop nothing, so it is skipped for enforce (#1360 item 7). It
    # stays for SHADOW scopes (predicate is None there): rows are kept
    # unchanged and the would-deny volume is logged (outside the MAE
    # vocabulary, so shadow is log-only); non-agent calls are a no-query
    # no-op inside the filter.
    from services.agent_binding_service import filter_memory_rows_by_binding

    if binding_predicate is None:
        rows, _ = await filter_memory_rows_by_binding(db, rows, operation=None, user_id=None)

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
        # #1357: same SQL predicate as the member page — the post-fetch row
        # filter below only subtracts type/source for BOUND contexts (an
        # unbound context has no filter row → kept), so a representative
        # moved to an unbound context after the analysis ran would leak its
        # summary through the membership gap. All three enforce-mode
        # subtraction points (page, reps, list_clusters) use the one lever.
        rep_conditions = [
            Memory.id.in_(rep_ids),
            Memory.deleted_at.is_(None),
            # Same defense-in-depth predicate as the page query
            # above — Issue #496 Copilot review.
            Memory.workspace_id == workspace_id,
        ]
        if binding_predicate is not None:
            rep_conditions.append(binding_predicate)
        rep_rows = (
            await db.execute(
                select(
                    Memory.id,
                    Memory.summary,
                    Memory.tags,
                    Memory.importance,
                    Memory.context_id,
                    Memory.type,
                    Memory.source_type,
                ).where(and_(*rep_conditions))
            )
        ).all()
        # Same enforce-skip as the page rows (#1360 item 7): the SQL
        # predicate above already subtracted for enforce agents.
        if binding_predicate is None:
            rep_rows, _ = await filter_memory_rows_by_binding(
                db, list(rep_rows), operation=None, user_id=None
            )
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

    # #1301: ``count`` and ``property_stats`` are whole-cluster stored
    # aggregates — ``types`` is the same grouped-count existence oracle over
    # denied types that stats.by_type was, and ``tags`` carries verbatim tag
    # strings from every member row. For enforce-mode agents with an active
    # binding filter, recompute count/types over the rows the binding permits
    # (one GROUP BY); when any row was subtracted, the remaining facets
    # (tags/importance/time histograms) are dropped fail-closed rather than
    # recomputed — they would otherwise leak denied rows' content and
    # contradict the recomputed count.
    #
    # The recompute runs for EVERY enforce-mode agent (the predicate is
    # non-None even for a non-restricting binding, since it carries the
    # membership gate): deliberate — deriving "could this agent be
    # restricted for THIS cluster's context" would need the run's context
    # (not in hand here, and rows may be an empty page), and the cost is one
    # indexed GROUP BY over a single cluster's assignments, agent-only.
    count = int(cluster.count)
    property_stats = cluster.property_stats or {}
    if binding_predicate is not None:
        # #1360 item 12: one grouped query yields BOTH the live count and
        # the binding-permitted count per type (FILTER clause), so a
        # stored-count mismatch caused purely by post-analysis
        # soft-deletes is no longer mistaken for a binding subtraction —
        # facets drop fail-closed ONLY when the binding actually
        # subtracted a live row.
        type_rows = (
            await db.execute(
                select(
                    Memory.type,
                    func.count(Memory.id),
                    func.count(Memory.id).filter(binding_predicate),
                )
                .join(
                    MemoryAnalysisAssignment,
                    MemoryAnalysisAssignment.memory_id == Memory.id,
                )
                .where(
                    and_(
                        MemoryAnalysisAssignment.analysis_id == run_id,
                        MemoryAnalysisAssignment.cluster_id == cluster.id,
                        Memory.deleted_at.is_(None),
                        Memory.workspace_id == workspace_id,
                    )
                )
                .group_by(Memory.type)
            )
        ).all()
        live_count = sum(int(row[1]) for row in type_rows)
        permitted_types = {row[0]: int(row[2]) for row in type_rows if int(row[2])}
        permitted_count = sum(permitted_types.values())
        count = permitted_count
        if permitted_count != live_count:
            property_stats = {"types": permitted_types}
            # Deny-observability parity with the row-filter lever (#1360
            # review): the SQL predicate subtracts silently — emit the
            # aggregate operators previously got from
            # agent_binding_row_filter_denied. Ids/counts only.
            from auth.agent_scope import get_agent_scope

            scope = get_agent_scope()
            logger.warning(
                "agent_binding_sql_subtraction",
                surface="get_cluster",
                run_id=str(run_id),
                denied_count=live_count - permitted_count,
                agent_id=str(scope.agent_id) if scope else None,
            )
        elif "types" in property_stats:
            property_stats = {**property_stats, "types": permitted_types}

    return {
        "run_id": str(run_id),
        "cluster_index": cluster_index,
        "cluster_id": str(cluster.id),
        "label": cluster.label,
        "description": cluster.description,
        "count": count,
        "label_confidence": float(cluster.label_confidence),
        "centroid_2d": list(cluster.centroid_2d) if cluster.centroid_2d else None,
        "property_stats": property_stats,
        "representatives": representatives,
        "memories": memories_out,
        "next_cursor": next_cursor,
    }


async def list_clusters(
    db: AsyncSession,
    *,
    workspace_id: UUID,
    run_id: UUID,
) -> list[MemoryAnalysisCluster] | list[dict[str, Any]] | None:
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
    - **Enforce-mode agent scope (#1357)**: ``list[dict]`` instead — plain
      copies shaped like ``ClusterRow`` (``cluster_index`` / ``label`` /
      ``description`` / ``count`` / ``centroid_2d`` /
      ``representative_memory_ids`` / ``property_stats`` /
      ``label_confidence``; no DB ``id`` / ``analysis_id``), with
      count/types recomputed over permitted rows, other facets dropped
      fail-closed on subtraction, denied representative ids removed, and
      clusters with ZERO permitted rows omitted entirely.
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
    clusters = list((await db.execute(stmt)).scalars().all())

    # #1357: REST parity with the MCP ``get_cluster`` subtraction (#1301).
    # For enforce-mode agents the stored per-cluster ``count`` /
    # ``property_stats.types`` are an existence oracle over denied
    # type/source rows, and ``representative_memory_ids`` names denied
    # rows' UUIDs outright. Recompute count/types over permitted rows
    # (ONE grouped query for the whole run), drop the remaining facets
    # fail-closed when any row was subtracted (same rule as
    # ``get_cluster``), and subtract denied representative ids. Plain
    # dict copies — never mutate the ORM rows (they would flush).
    from services.agent_binding_service import binding_memory_sql_predicate

    binding_predicate = await binding_memory_sql_predicate(db)
    if binding_predicate is None or not clusters:
        return clusters

    # #1360 item 12: live + permitted counts in ONE grouped query (FILTER
    # clause) so post-analysis soft-delete drift is not mistaken for a
    # binding subtraction — same rule as get_cluster.
    type_rows = (
        await db.execute(
            select(
                MemoryAnalysisAssignment.cluster_id,
                Memory.type,
                func.count(Memory.id),
                func.count(Memory.id).filter(binding_predicate),
            )
            .join(Memory, Memory.id == MemoryAnalysisAssignment.memory_id)
            .where(
                and_(
                    MemoryAnalysisAssignment.analysis_id == run_id,
                    Memory.deleted_at.is_(None),
                    Memory.workspace_id == workspace_id,
                )
            )
            .group_by(MemoryAnalysisAssignment.cluster_id, Memory.type)
        )
    ).all()
    permitted_types: dict[UUID, dict[str, int]] = {}
    live_counts: dict[UUID, int] = {}
    for cluster_id, mem_type, live_cnt, permitted_cnt in type_rows:
        live_counts[cluster_id] = live_counts.get(cluster_id, 0) + int(live_cnt)
        if int(permitted_cnt):
            permitted_types.setdefault(cluster_id, {})[mem_type] = int(permitted_cnt)

    all_rep_ids = {rid for c in clusters for rid in (c.representative_memory_ids or [])}
    permitted_rep_ids: set[UUID] = set()
    if all_rep_ids:
        rep_id_rows = (
            await db.execute(
                select(Memory.id).where(
                    and_(
                        Memory.id.in_(all_rep_ids),
                        Memory.deleted_at.is_(None),
                        Memory.workspace_id == workspace_id,
                        binding_predicate,
                    )
                )
            )
        ).all()
        permitted_rep_ids = {row[0] for row in rep_id_rows}

    subtracted: list[dict[str, Any]] = []
    denied_total = 0
    for c in clusters:
        types = permitted_types.get(c.id, {})
        filtered_count = sum(types.values())
        cluster_live = live_counts.get(c.id, 0)
        denied_total += cluster_live - filtered_count
        if filtered_count == 0 and cluster_live > 0:
            # Fail-closed on the ENUMERATION surface: a cluster whose
            # every LIVE row the binding denies would still volunteer its
            # LLM label / description — content synthesized from the
            # denied members — and the count-0-with-label shape confirms
            # denied rows exist. A cluster emptied purely by post-analysis
            # soft-deletes (live == 0, nothing denied) stays listed with
            # count 0, matching the human view and the get_cluster
            # drill-down. Direct drill-down keeps the #1301 contract; the
            # list simply does not advertise denied-only clusters.
            continue
        stats = dict(c.property_stats or {})
        count = filtered_count
        # Facets drop fail-closed ONLY on a real binding subtraction —
        # a live-vs-stored mismatch from post-analysis soft-deletes keeps
        # the stored facets (types still refreshed to permitted values).
        if filtered_count != live_counts.get(c.id, 0):
            stats = {"types": types}
        elif "types" in stats:
            stats = {**stats, "types": types}
        subtracted.append(
            {
                "cluster_index": c.cluster_index,
                "label": c.label,
                "description": c.description,
                "count": count,
                "centroid_2d": list(c.centroid_2d) if c.centroid_2d else [],
                "representative_memory_ids": [
                    rid for rid in (c.representative_memory_ids or []) if rid in permitted_rep_ids
                ],
                "property_stats": stats,
                "label_confidence": float(c.label_confidence),
            }
        )
    if denied_total > 0:
        # Deny-observability parity with the row-filter lever (#1360
        # review): the SQL predicate subtracts silently, so emit the
        # aggregate the operators previously got from
        # agent_binding_row_filter_denied. Ids/counts only.
        from auth.agent_scope import get_agent_scope

        scope = get_agent_scope()
        logger.warning(
            "agent_binding_sql_subtraction",
            surface="list_clusters",
            run_id=str(run_id),
            denied_count=denied_total,
            agent_id=str(scope.agent_id) if scope else None,
        )
    return subtracted


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

    from services.agent_binding_service import binding_memory_sql_predicate

    binding_predicate = await binding_memory_sql_predicate(db)
    position_binding = [binding_predicate] if binding_predicate is not None else []

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
                # #1357: enforce-mode agents never see denied rows' memory_id
                # (or their dot positions) — same lever as get_cluster.
                *position_binding,
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
