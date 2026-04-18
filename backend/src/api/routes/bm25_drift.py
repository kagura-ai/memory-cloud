"""BM25 IDF drift admin API.

Issue #343: read-only list/detail of bm25_idf_drift_log rows + manual
trigger for dev/staging. All endpoints are admin-only and tagged
`(preview)` because the underlying cron is disabled in production until
v0.14.0.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, field_serializer
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import require_admin
from db.base import get_db
from models.auth import Context
from models.bm25_drift import Bm25IdfDriftLog
from services.bm25_drift.orchestrator import Bm25DriftOrchestrator
from services.bm25_drift.psi_calculator import PsiStatus
from utils.datetime import to_utc_iso
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(
    prefix="/admin/bm25-drift",
    tags=["bm25-drift (preview)"],
)


# ============================================================================
# Schemas
# ============================================================================


class Bm25DriftSummary(BaseModel):
    """Drift log row, list-view shape (no top_divergent_terms)."""

    id: int
    context_id: UUID
    context_name: str | None = None
    measured_at: datetime
    psi: float | None
    psi_status: str
    m_memory_points: int
    r_resource_points: int
    num_terms: int

    @field_serializer("measured_at")
    def _serialize_dt(self, dt: datetime) -> str:
        return to_utc_iso(dt) or ""


class Bm25DriftDetail(Bm25DriftSummary):
    """Drift log row, detail-view shape (includes top_divergent_terms)."""

    context_deleted: bool = False
    top_divergent_terms: list[dict[str, Any]] | None = None


class Bm25DriftListResponse(BaseModel):
    rows: list[Bm25DriftSummary]
    total: int
    limit: int
    offset: int


class Bm25DriftDetailResponse(BaseModel):
    row: Bm25DriftDetail


class Bm25DriftRunRequest(BaseModel):
    """Body for POST /admin/bm25-drift/run.

    Attributes:
        context_id: Target context. When null, runs against every active
            context (matches the cron behaviour). For dev/staging
            verification — production cron remains disabled until v0.14.0.
    """

    context_id: UUID | None = None


class Bm25DriftRunResponse(BaseModel):
    scheduled_context_count: int


# Single source of truth for the status enum: derive from the Literal in
# psi_calculator. The DB CHECK constraint and ORM model already pin the
# same set; if a status is added there, it shows up here automatically.
_VALID_STATUSES = set(PsiStatus.__args__)


# ============================================================================
# Endpoints
# ============================================================================


@router.get("", response_model=Bm25DriftListResponse)
async def list_drift_logs(
    _admin: dict = Depends(require_admin),  # noqa: ARG001 - access guard only
    db: AsyncSession = Depends(get_db),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    status_filter: str | None = Query(None, alias="status"),
    context_id: UUID | None = Query(None),
) -> Bm25DriftListResponse:
    """List BM25 IDF drift log rows. Admin-only.

    Sorted by measured_at DESC. Filterable by psi_status and context_id.
    """
    if status_filter is not None and status_filter not in _VALID_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid status. Must be one of: {sorted(_VALID_STATUSES)}",
        )

    conditions = []
    if status_filter is not None:
        conditions.append(Bm25IdfDriftLog.psi_status == status_filter)
    if context_id is not None:
        conditions.append(Bm25IdfDriftLog.context_id == context_id)

    count_stmt = select(func.count()).select_from(Bm25IdfDriftLog)
    if conditions:
        count_stmt = count_stmt.where(*conditions)
    total = (await db.execute(count_stmt)).scalar() or 0

    stmt = select(Bm25IdfDriftLog)
    if conditions:
        stmt = stmt.where(*conditions)
    rows_result = await db.execute(
        stmt.order_by(Bm25IdfDriftLog.measured_at.desc()).limit(limit).offset(offset)
    )
    rows = list(rows_result.scalars().all())

    # Batch-resolve context display names to avoid N+1.
    ctx_ids = {r.context_id for r in rows}
    ctx_map: dict[UUID, str | None] = {}
    if ctx_ids:
        ctx_result = await db.execute(
            select(Context.id, Context.name, Context.display_name, Context.deleted_at).where(
                Context.id.in_(ctx_ids)
            )
        )
        for cid, cname, cdisplay, cdeleted in ctx_result.all():
            ctx_map[cid] = None if cdeleted is not None else (cdisplay or cname)

    for r in rows:
        r.context_name = ctx_map.get(r.context_id)

    return Bm25DriftListResponse(
        rows=[Bm25DriftSummary.model_validate(r, from_attributes=True) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{row_id}", response_model=Bm25DriftDetailResponse)
async def get_drift_log_detail(
    row_id: int,
    _admin: dict = Depends(require_admin),  # noqa: ARG001 - access guard only
    db: AsyncSession = Depends(get_db),
) -> Bm25DriftDetailResponse:
    """Detail view including top_divergent_terms. Admin-only.

    Emits an audit log event on every read so historical access is
    reconstructable from logs alone.
    """
    row_result = await db.execute(select(Bm25IdfDriftLog).where(Bm25IdfDriftLog.id == row_id))
    row = row_result.scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Drift log {row_id} not found.",
        )

    ctx_result = await db.execute(
        select(Context.id, Context.name, Context.display_name, Context.deleted_at).where(
            Context.id == row.context_id
        )
    )
    ctx_row = ctx_result.first()
    context_deleted = False
    if ctx_row is not None:
        _cid, cname, cdisplay, cdeleted = ctx_row
        if cdeleted is not None:
            row.context_name = None
            context_deleted = True
        else:
            row.context_name = cdisplay or cname
    else:
        row.context_name = None
        context_deleted = True

    row.context_deleted = context_deleted

    # Audit log — terms count only, never term content.
    logger.info(
        "drift_detail_accessed",
        row_id=row_id,
        context_id=str(row.context_id),
        psi=float(row.psi) if row.psi is not None else None,
        status=row.psi_status,
        num_terms=row.num_terms,
    )

    return Bm25DriftDetailResponse(
        row=Bm25DriftDetail.model_validate(row, from_attributes=True),
    )


@router.post(
    "/run",
    response_model=Bm25DriftRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def run_drift_now(
    body: Bm25DriftRunRequest,
    _admin: dict = Depends(require_admin),  # noqa: ARG001 - access guard only
    db: AsyncSession = Depends(get_db),
) -> Bm25DriftRunResponse:
    """Manually trigger a drift measurement. Admin-only.

    For dev/staging verification while the production cron is disabled
    (v0.12.1 ships infrastructure-only; production enablement = v0.14.0).
    Each requested context is enqueued as an `asyncio.create_task` and
    runs against an independent DB session.
    """
    if body.context_id is not None:
        target_context_ids: list[UUID] = [body.context_id]
    else:
        from models.memory import Memory

        result = await db.execute(
            select(Memory.context_id)
            .distinct()
            .where(Memory.deleted_at.is_(None))
            .where(Memory.context_id.isnot(None))
        )
        target_context_ids = [r[0] for r in result.all() if r[0] is not None]

    # Keep strong references to the spawned tasks until the response
    # is built. asyncio.create_task returns a Task that is GC-eligible
    # if the only reference is dropped before the loop schedules it
    # (cpython#90887). The list survives until function return, which
    # gives the event loop time to pick the tasks up.
    _spawned: list[asyncio.Task] = [
        asyncio.create_task(_run_drift_job(cid)) for cid in target_context_ids
    ]

    logger.info(
        "drift_manual_trigger_scheduled",
        scheduled=len(_spawned),
    )
    return Bm25DriftRunResponse(scheduled_context_count=len(_spawned))


async def _run_drift_job(context_id: UUID) -> None:
    """Background task: open an independent session and run one drift cycle.

    Mirrors admin_sleep._run_sleep_job — own session per job avoids
    request-session lifetime entanglement. The rollback itself can fail
    (e.g. connection loss); we guard it so the structured error log still
    reaches the operator.
    """
    from db.base import get_db as _get_db

    async for db in _get_db():
        try:
            orchestrator = Bm25DriftOrchestrator(db)
            await orchestrator.run(context_id)
            await db.commit()
        except Exception as e:
            try:
                await db.rollback()
            except Exception as rollback_exc:
                logger.error(
                    "drift_manual_trigger_rollback_failed",
                    context_id=str(context_id),
                    error=str(rollback_exc),
                )
            logger.error(
                "drift_manual_trigger_failed",
                context_id=str(context_id),
                error=str(e),
                exc_info=True,
            )
        return
