"""Memory Analysis API routes (Issue #496).

REST surface for the analysis pipeline (#495 backend) sitting behind the
4-stage gate composed in ``auth.analysis_gates``:

    POST   /api/v1/contexts/{context_id}/analyses/preview
    POST   /api/v1/contexts/{context_id}/analyses          (202)
    GET    /api/v1/contexts/{context_id}/analyses
    GET    /api/v1/contexts/{context_id}/analyses/active
    GET    /api/v1/contexts/{context_id}/analyses/{run_id}
    DELETE /api/v1/contexts/{context_id}/analyses/{run_id} (soft cancel)

The route handlers are intentionally thin: gates → orchestrator/query
service → response. POST is the only side-effectful path; everything
else is a pure read or a status flip.

Context-boundary verification: the gate dependency already verified
workspace ownership, but the caller's URL ``context_id`` could belong
to another workspace. Each handler runs ``_verify_context_in_workspace``
to reject mismatches with 404 (deliberately not 403 so we don't leak
the existence of a context the caller cannot see).

Idempotency: POST /analyses raises ``ConflictError`` (409) when a prior
run for the same (workspace, context) is still ``running``. The 409
body carries the existing ``run_id`` so the client can poll instead of
spamming retries (matches the orchestrator's
``ConflictError`` contract).
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from auth.analysis_gates import AnalysisReadAccess, AnalysisWriteAccess
from db.base import get_db
from models.analysis import MEMORY_ANALYSIS_CANCELLATION_REASONS, MEMORY_ANALYSIS_STATUSES
from models.api_base import TZAwareBaseModel
from services.analysis import query_service
from services.analysis.orchestrator import AnalysisOrchestrator, AnalysisParams
from services.analysis.preview import DEFAULT_MODEL_ID, estimate_cost
from tasks.analysis_tasks import run_analysis_task
from utils.exceptions import ConflictError, ValidationError
from utils.logger import get_logger

logger = get_logger(__name__)

# Pinned status / reason constants — assert they remain members of the
# canonical tuples so a future tuple reorder fires loud at import time
# rather than silently sending an INSERT through the DB CHECK.
_STATUS_RUNNING = "running"
_STATUS_CANCELLED = "cancelled"
_CANCEL_REASON_USER = "user"
assert _STATUS_RUNNING in MEMORY_ANALYSIS_STATUSES
assert _STATUS_CANCELLED in MEMORY_ANALYSIS_STATUSES
assert _CANCEL_REASON_USER in MEMORY_ANALYSIS_CANCELLATION_REASONS

# Strong-ref set for ``asyncio.create_task`` — without this, the task
# can be garbage-collected before the event loop schedules it. Same
# pattern as ``api/routes/admin_sleep.py:_log_background_task_result``
# but module-scoped so multiple in-flight kick-offs are tracked.
_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()


def _log_background_task_result(task: asyncio.Task[Any]) -> None:
    """Surface any exception that escaped the analysis background task.

    Mirrors ``admin_sleep.py:_log_background_task_result``. Cancellation
    is normal (run status flipped to ``cancelled`` by DELETE handler);
    other exceptions are logged so the failure is observable in
    monitoring even though the request response was already 202.
    """
    _BACKGROUND_TASKS.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            "analysis_background_task_crashed",
            error=str(exc),
            exc_info=exc,
        )


router = APIRouter(
    prefix="/contexts/{context_id}/analyses",
    tags=["analyses"],
)


# ============================================================================
# Schemas
# ============================================================================


class AnalysisPreviewRequest(BaseModel):
    """Pre-flight cost-estimate request body.

    Mirrors the POST body so the modal in #497 can submit the same
    payload twice (preview, then run on confirm) without re-shaping.
    Empty body is valid — the preview falls back to the count of
    every memory in the context.
    """

    from_dt: str | None = Field(None, alias="from")
    to_dt: str | None = Field(None, alias="to")
    types: list[str] | None = None
    tags: list[str] | None = None
    min_importance: float | None = Field(None, ge=0.0, le=1.0)
    query: str | None = None
    model_id: int | None = None

    model_config = {"populate_by_name": True}

    @field_validator("from_dt", "to_dt", mode="after")
    @classmethod
    def _validate_iso8601(cls, v: str | None) -> str | None:
        """Reject non-ISO-8601 ``from``/``to`` strings at the boundary.

        Without this, an invalid datetime string flows through to
        ``AnalysisOrchestrator.run`` and raises during
        ``_params_iso_to_naive_utc`` AFTER the ``memory_analyses`` row
        has already been INSERTed at ``status='running'``. The run
        would stay stuck in ``running`` (until a manual cancel) AND
        count toward the daily quota — silent UX failure. Catching at
        Pydantic boundary returns a clean 422 BEFORE the row is
        created, so the quota window is preserved. Issue #496 Copilot
        review.
        """
        if v is None:
            return v
        try:
            datetime.fromisoformat(v)
        except ValueError as e:
            raise ValueError(
                f"Invalid ISO-8601 datetime: {v!r}. "
                "Expected forms: '2026-05-02T00:00:00Z', "
                "'2026-05-02T09:00:00+09:00', or naive 'YYYY-MM-DDTHH:MM:SS'."
            ) from e
        return v


class AnalysisPreviewResponse(BaseModel):
    """Cost-estimate output (Stage [A] from preview.py)."""

    memory_count: int
    cluster_count_estimate: int
    estimated_cost_cents: int
    model_id: str
    breakdown: dict[str, int]


class AnalysisStartRequest(AnalysisPreviewRequest):
    """POST /analyses body — same shape as preview (intentional)."""


class AnalysisStartResponse(TZAwareBaseModel):
    """202 Accepted body.

    Constructed via ``model_validate(analysis)`` so the ORM row's
    ``id`` column maps to ``run_id`` (matches the issue spec naming)
    and SQLAlchemy ``Column[datetime]`` ↔ Python ``datetime`` is
    bridged by Pydantic's ``from_attributes`` rather than manual
    cast at every call site.
    """

    run_id: UUID = Field(validation_alias="id")
    status: str
    started_at: datetime

    model_config = {"populate_by_name": True, "from_attributes": True}


class AnalysisRow(TZAwareBaseModel):
    """One row in list / single-fetch responses.

    Fields mirror ``MemoryAnalysis`` columns the API contract surfaces
    (#496 spec). Internal columns like ``model_snapshot`` are omitted
    on purpose — the snapshot is a debugging artifact, not part of
    the public contract, and adds ~500B per row to the list payload.

    ``validation_alias="id"`` (input-only) maps the ORM column ``id``
    → ``run_id`` field while keeping the wire-format key as ``run_id``
    for clients (matches the issue spec naming).
    """

    run_id: UUID = Field(validation_alias="id")
    workspace_id: UUID
    context_id: UUID
    status: str
    triggered_by: str
    started_at: datetime
    finished_at: datetime | None
    input_count: int
    cost_estimated_cents: int | None
    cost_actual_cents: int | None
    error: str | None
    cancellation_reason: str | None

    model_config = {"populate_by_name": True, "from_attributes": True}


class AnalysisListResponse(BaseModel):
    """Paginated list of runs."""

    items: list[AnalysisRow]
    next_cursor: str | None


class ClusterRow(BaseModel):
    """One cluster within an analysis run.

    Mirrors ``MemoryAnalysisCluster`` columns the API contract surfaces
    (#497 frontend). Internal columns (``analysis_id``, ``parent_id``,
    cluster ``id`` UUID PK) are omitted: the wire identifier is the
    user-visible ``cluster_index`` ordinal, not the DB UUID.

    ``representative_memory_ids`` is the raw list as stored — the
    frontend resolves these to memory summaries via a follow-up
    ``recall(filters={"id": [...]})`` call rather than embedding the
    summaries in this response (keeps the cluster list payload small
    and lets the recall layer apply its own importance / freshness
    sorting).
    """

    cluster_index: int
    label: str
    description: str | None
    count: int
    centroid_2d: list[float]
    representative_memory_ids: list[UUID]
    property_stats: dict[str, Any]
    label_confidence: float

    model_config = {"from_attributes": True}


class ClusterListResponse(BaseModel):
    """All clusters for a run, ordered by ``cluster_index``."""

    items: list[ClusterRow]


class PositionRow(BaseModel):
    """One ``(memory_id, x, y, cluster_index)`` row for scatter rendering."""

    memory_id: UUID
    x: float
    y: float
    cluster_index: int


class PositionListResponse(BaseModel):
    """All scatter positions for a run, ordered by ``cluster_index``."""

    items: list[PositionRow]


class AnalysisCancelResponse(TZAwareBaseModel):
    """DELETE /{run_id} response — confirms the soft-cancel.

    Same ``model_validate(row)`` pattern as AnalysisStartResponse so
    SQLAlchemy column types are bridged via Pydantic ``from_attributes``.
    """

    run_id: UUID = Field(validation_alias="id")
    status: str
    cancellation_reason: str | None
    finished_at: datetime | None

    model_config = {"populate_by_name": True, "from_attributes": True}


# ============================================================================
# Internal helpers
# ============================================================================


async def _verify_context_in_workspace(
    db: AsyncSession,
    *,
    workspace_id: UUID,
    context_id: UUID,
) -> None:
    """Raise 404 if the context does not belong to the caller's workspace.

    Thin wrapper over ``query_service.verify_context_in_workspace`` —
    the SELECT is shared with the MCP-side variant in
    ``mcp_server/tools/analysis.py`` so the boundary check lives in
    one place. 404 (not 403) so context existence is not leaked.
    """
    if not await query_service.verify_context_in_workspace(
        db, workspace_id=workspace_id, context_id=context_id
    ):
        raise HTTPException(status_code=404, detail=f"Context {context_id} not found")


def _params_from_body(body: AnalysisPreviewRequest) -> AnalysisParams:
    """Convert a request body to the orchestrator's ``AnalysisParams``.

    The from/to fields in ``AnalysisParams`` are ``datetime``; we leave
    the body as ISO strings and let the orchestrator's
    ``_params_iso_to_naive_utc`` handle tz normalization at run() time.
    Storing strings on params keeps the ``params`` JSONB stable across
    serialization round-trips.
    """
    return AnalysisParams(
        from_dt=None,
        to_dt=None,
        types=body.types,
        tags=body.tags,
        min_importance=body.min_importance,
        query=body.query,
        model_id=body.model_id,
        extra={
            # Persist the raw ISO strings so orchestrator.run() can
            # re-parse them with the timezone normalization rules.
            "from": body.from_dt,
            "to": body.to_dt,
        },
    )


# ============================================================================
# POST /preview — pre-flight cost estimate (no row created)
# ============================================================================


@router.post(
    "/preview",
    response_model=AnalysisPreviewResponse,
    summary="Estimate analysis cost without starting a run",
)
async def preview_analysis(
    context_id: UUID,
    body: AnalysisPreviewRequest,
    access: AnalysisWriteAccess,
    db: AsyncSession = Depends(get_db),
) -> AnalysisPreviewResponse:
    """Returns the cost estimate for a hypothetical run.

    Goes through the full 4-gate chain so a non-Pro / quota-exhausted
    / non-allowlisted user gets the same 403/429 they would on POST,
    rather than seeing the price and then being blocked at confirm.
    """
    _user_id, workspace_id, _tz = access
    await _verify_context_in_workspace(db, workspace_id=workspace_id, context_id=context_id)

    # v1 ignores body filters in the count to keep the modal under
    # 200ms — the actual run will apply filters. The user-facing modal
    # text in #497 says "estimate based on full context size; actual
    # cost may be lower if filters apply".
    memory_count = await query_service.count_context_memories(
        db,
        workspace_id=workspace_id,
        context_id=context_id,
    )
    # v1 only supports the default model in the cost estimator;
    # body.model_id is forward-compat scaffolding (preview.py:73-77).
    estimate = estimate_cost(memory_count, model_id=DEFAULT_MODEL_ID)
    return AnalysisPreviewResponse(
        memory_count=estimate.memory_count,
        cluster_count_estimate=estimate.cluster_count_estimate,
        estimated_cost_cents=estimate.estimated_cost_cents,
        model_id=estimate.model_id,
        breakdown=estimate.breakdown,
    )


# ============================================================================
# POST / — start a run (202)
# ============================================================================


@router.post(
    "",
    response_model=AnalysisStartResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start a Memory Analysis run",
)
async def start_analysis(
    context_id: UUID,
    body: AnalysisStartRequest,
    access: AnalysisWriteAccess,
    db: AsyncSession = Depends(get_db),
) -> AnalysisStartResponse:
    """Kick off a background analysis run.

    Returns 202 with the new run_id. The pipeline runs in
    ``asyncio.create_task(run_analysis_task)``; clients poll
    ``GET /analyses/{run_id}`` for status transitions.

    Error mapping (delegated to global handler via custom exceptions):

    - 409 ConflictError  — a prior run is still ``running`` for the
      same (workspace, context). Body carries the existing run_id.
    - 422 ValidationError — BYOK key missing (orchestrator's
      pre-flight) OR caller-supplied ``model_id`` does not exist.
    """
    user_id, workspace_id, _tz = access
    await _verify_context_in_workspace(db, workspace_id=workspace_id, context_id=context_id)

    params = _params_from_body(body)
    orchestrator = AnalysisOrchestrator(db)
    try:
        analysis = await orchestrator.start(
            workspace_id=workspace_id,
            context_id=context_id,
            user_id=user_id,
            params=params,
        )
    except ConflictError:
        # Re-raise — global handler at api/main.py converts to 409 JSON.
        raise
    except ValidationError:
        # BYOK / model_id missing → 422 with VAL-001 + provider hint.
        raise

    await db.commit()

    # Background pipeline. The task opens its own AsyncSession (see
    # tasks/analysis_tasks.py). Strong-ref + done callback so the task
    # is not GC'd before the loop schedules it AND any exception is
    # surfaced in logs.
    task = asyncio.create_task(run_analysis_task(analysis.id))
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_log_background_task_result)

    logger.info(
        "analysis_run_kicked_off",
        run_id=str(analysis.id),
        workspace_id=str(workspace_id),
        context_id=str(context_id),
        triggered_by=user_id,
    )
    return AnalysisStartResponse.model_validate(analysis)


# ============================================================================
# GET / — list runs (paginated, newest first)
# ============================================================================


@router.get(
    "",
    response_model=AnalysisListResponse,
    summary="List analysis runs for a context",
)
async def list_runs(
    context_id: UUID,
    access: AnalysisReadAccess,
    cursor: str | None = Query(None, description="Opaque pagination cursor"),
    limit: int | None = Query(None, ge=1, le=query_service.MAX_LIST_PAGE_SIZE),
    db: AsyncSession = Depends(get_db),
) -> AnalysisListResponse:
    """Return runs for the context, sorted by ``started_at DESC``."""
    _user_id, workspace_id, _tz = access
    await _verify_context_in_workspace(db, workspace_id=workspace_id, context_id=context_id)

    rows, next_cursor = await query_service.list_analyses(
        db,
        workspace_id=workspace_id,
        context_id=context_id,
        limit=limit,
        cursor=cursor,
    )
    return AnalysisListResponse(
        items=[AnalysisRow.model_validate(row) for row in rows],
        next_cursor=next_cursor,
    )


# ============================================================================
# GET /active — most recent succeeded run
# ============================================================================


@router.get(
    "/active",
    response_model=AnalysisRow,
    summary="Most recent succeeded run for a context",
)
async def get_active(
    context_id: UUID,
    access: AnalysisReadAccess,
    db: AsyncSession = Depends(get_db),
) -> AnalysisRow:
    """Returns 404 when the context has no succeeded run yet."""
    _user_id, workspace_id, _tz = access
    await _verify_context_in_workspace(db, workspace_id=workspace_id, context_id=context_id)

    row = await query_service.get_active_analysis(
        db,
        workspace_id=workspace_id,
        context_id=context_id,
    )
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"No succeeded analysis run found for context {context_id}",
        )
    return AnalysisRow.model_validate(row)


# ============================================================================
# GET /{run_id} — single run
# ============================================================================


@router.get(
    "/{run_id}",
    response_model=AnalysisRow,
    summary="Fetch one analysis run by id",
)
async def get_run(
    context_id: UUID,
    run_id: UUID,
    access: AnalysisReadAccess,
    db: AsyncSession = Depends(get_db),
) -> AnalysisRow:
    _user_id, workspace_id, _tz = access
    await _verify_context_in_workspace(db, workspace_id=workspace_id, context_id=context_id)

    row = await query_service.get_analysis(db, workspace_id=workspace_id, run_id=run_id)
    # 404 also fires when the run belongs to a different context within
    # the same workspace — clients should not be able to read another
    # context's runs even if the workspace gate passed.
    if row is None or row.context_id != context_id:
        raise HTTPException(status_code=404, detail=f"Analysis run {run_id} not found")
    return AnalysisRow.model_validate(row)


# ============================================================================
# GET /{run_id}/clusters — flat cluster list for scatter / drill-down (#497)
# ============================================================================


@router.get(
    "/{run_id}/clusters",
    response_model=ClusterListResponse,
    summary="List all clusters for an analysis run",
)
async def list_run_clusters(
    context_id: UUID,
    run_id: UUID,
    access: AnalysisReadAccess,
    db: AsyncSession = Depends(get_db),
) -> ClusterListResponse:
    """Returns every cluster row for the run, ordered by ``cluster_index``.

    Powers the #497 frontend cluster list, scatter centroids, and the
    per-cluster property-stats panel. Cluster count is bounded
    server-side by ``ceil(sqrt(memory_count))`` (≈ 90 on an 8000-memory
    run), so the endpoint returns the full set with no pagination — the
    frontend would otherwise have to make up-to-N round-trips to render
    the cluster list which dominates the latency budget on this page.

    A run that is still ``running`` or that ``failed`` before the
    labeler stage may have zero cluster rows; the response is then
    ``items: []`` rather than 404 (matches the running-status semantics
    of the run-level read).
    """
    _user_id, workspace_id, _tz = access
    await _verify_context_in_workspace(db, workspace_id=workspace_id, context_id=context_id)

    # Verify the run belongs to *this* context (not just this workspace).
    # The cluster list itself is bound by analysis_id, but the URL
    # advertises a context_id — silently returning another context's
    # clusters when the URLs cross would violate the principle of least
    # surprise even if no tenancy boundary was crossed.
    row = await query_service.get_analysis(db, workspace_id=workspace_id, run_id=run_id)
    if row is None or row.context_id != context_id:
        raise HTTPException(status_code=404, detail=f"Analysis run {run_id} not found")

    clusters = await query_service.list_clusters(db, workspace_id=workspace_id, run_id=run_id)
    if clusters is None:
        # Defense-in-depth: ``get_analysis`` already established the run
        # belongs to this workspace, so list_clusters' boundary check
        # cannot return None at this point. Treat as 404 just in case
        # the row was deleted between the two SELECTs.
        raise HTTPException(status_code=404, detail=f"Analysis run {run_id} not found")

    return ClusterListResponse(items=[ClusterRow.model_validate(c) for c in clusters])


# ============================================================================
# GET /{run_id}/positions — per-memory 2D coords for scatter rendering (#497)
# ============================================================================


@router.get(
    "/{run_id}/positions",
    response_model=PositionListResponse,
    summary="List per-memory 2D positions for an analysis run",
)
async def list_run_positions(
    context_id: UUID,
    run_id: UUID,
    access: AnalysisReadAccess,
    db: AsyncSession = Depends(get_db),
) -> PositionListResponse:
    """Returns every ``(memory_id, x, y, cluster_index)`` for the scatter dots.

    Bounded by ``MemoryAnalysis.input_count`` (capped at 10k memories
    in the orchestrator). Returns ~640 KB worst-case JSON which is
    acceptable on an explicit page open. No pagination — splitting
    forces the frontend to coordinate progressive renders for very
    little user-visible benefit, and the tab is gated by 4-stage
    auth so the audience is small.

    Same context-boundary check as ``/clusters``: the URL's
    ``context_id`` must match the run's stored ``context_id``.
    """
    _user_id, workspace_id, _tz = access
    await _verify_context_in_workspace(db, workspace_id=workspace_id, context_id=context_id)

    row = await query_service.get_analysis(db, workspace_id=workspace_id, run_id=run_id)
    if row is None or row.context_id != context_id:
        raise HTTPException(status_code=404, detail=f"Analysis run {run_id} not found")

    positions = await query_service.list_positions(db, workspace_id=workspace_id, run_id=run_id)
    if positions is None:
        raise HTTPException(status_code=404, detail=f"Analysis run {run_id} not found")

    return PositionListResponse(items=[PositionRow(**p) for p in positions])


# ============================================================================
# DELETE /{run_id} — soft cancel
# ============================================================================


@router.delete(
    "/{run_id}",
    response_model=AnalysisCancelResponse,
    summary="Soft-cancel a running analysis (status → cancelled)",
)
async def cancel_run(
    context_id: UUID,
    run_id: UUID,
    access: AnalysisWriteAccess,
    db: AsyncSession = Depends(get_db),
) -> AnalysisCancelResponse:
    """Mark a running run as ``cancelled``.

    No-op (idempotent 200) when the run is already in a terminal
    state — not all clients will ship a "stop polling" hook to detect
    that the run finished between their last poll and the cancel
    click. 409 only for the genuinely contradictory case (status not
    in {running, succeeded, failed, cancelled}).

    Cancellation does NOT decrement the daily quota — the run row
    has already been INSERTed by orchestrator.start, and BYOK /
    partial LLM cost may have been incurred (#496 quota AC).
    """
    _user_id, workspace_id, _tz = access
    await _verify_context_in_workspace(db, workspace_id=workspace_id, context_id=context_id)

    row = await query_service.get_analysis(db, workspace_id=workspace_id, run_id=run_id)
    if row is None or row.context_id != context_id:
        raise HTTPException(status_code=404, detail=f"Analysis run {run_id} not found")

    if row.status == _STATUS_RUNNING:
        # Mark cancelled. Background task may still be mid-stage; it
        # will complete and persist its own status update which the
        # reporter will treat as a no-op since cancellation_reason is
        # already set (status='cancelled' wins over the late update).
        row.status = _STATUS_CANCELLED
        row.cancellation_reason = _CANCEL_REASON_USER
        # finished_at is naive UTC — utcnow() returns naive UTC by
        # repo convention.
        from utils.datetime import utcnow

        row.finished_at = utcnow()
        await db.commit()
        logger.info(
            "analysis_run_cancelled",
            run_id=str(run_id),
            workspace_id=str(workspace_id),
            context_id=str(context_id),
        )
    # else: already terminal → return current state without flipping anything.

    return AnalysisCancelResponse.model_validate(row)
