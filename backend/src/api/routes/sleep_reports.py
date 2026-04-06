"""Sleep Reports Admin API Routes.

Admin-only endpoints for inspecting Sleep Maintenance reports and actions.
Issue #179: Sleep Report admin UI (split from #104).
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, field_serializer
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import require_admin
from db.base import get_db
from models.sleep import SleepAction, SleepReport
from utils.datetime import to_utc_iso
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/admin/sleep-reports", tags=["sleep-reports"])


# ============================================================================
# Schemas
# ============================================================================


class SleepReportSummary(BaseModel):
    """Sleep report summary for list view."""

    id: UUID
    user_id: str
    workspace_id: UUID | None
    context_id: UUID | None
    status: str
    started_at: datetime
    completed_at: datetime | None
    memories_processed: int
    edges_created: int
    memories_merged: int
    memories_promoted: int
    memories_flagged: int
    llm_calls_made: int
    llm_tokens_used: int

    @field_serializer("started_at", "completed_at")
    def _serialize_dt(self, dt: datetime | None) -> str | None:
        return to_utc_iso(dt)


class SleepReportDetail(SleepReportSummary):
    """Sleep report full detail."""

    embedding_calls_made: int
    error_message: str | None
    edge_discovery_result: dict[str, Any] | None
    dedup_result: dict[str, Any] | None
    importance_result: dict[str, Any] | None
    consolidation_result: dict[str, Any] | None
    reindex_result: dict[str, Any] | None


class SleepActionItem(BaseModel):
    """Sleep action audit log entry."""

    id: int
    phase: str
    action_type: str
    memory_id: UUID | None
    target_id: UUID | None
    details: dict[str, Any] | None
    created_at: datetime

    @field_serializer("created_at")
    def _serialize_dt(self, dt: datetime) -> str:
        return to_utc_iso(dt) or ""


class SleepReportListResponse(BaseModel):
    """List of sleep reports with pagination."""

    reports: list[SleepReportSummary]
    total: int
    limit: int
    offset: int


class SleepReportDetailResponse(BaseModel):
    """Sleep report detail with all actions."""

    report: SleepReportDetail
    actions: list[SleepActionItem]
    action_count: int


# ============================================================================
# Endpoints
# ============================================================================


_VALID_STATUSES = {"running", "completed", "failed", "cancelled", "rolled_back"}


@router.get("", response_model=SleepReportListResponse)
async def list_sleep_reports(
    _admin: dict = Depends(require_admin),  # noqa: ARG001 - FastAPI dep, access guard only
    db: AsyncSession = Depends(get_db),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    status_filter: str | None = Query(None, alias="status"),
    context_id: UUID | None = Query(None),
    user_id: str | None = Query(None),
) -> SleepReportListResponse:
    """List Sleep Maintenance reports with filters and pagination.

    Admin-only. Filterable by status, context_id, user_id.
    Sorted by started_at DESC.
    """
    if status_filter is not None and status_filter not in _VALID_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid status. Must be one of: {sorted(_VALID_STATUSES)}",
        )

    conditions = []
    if status_filter is not None:
        conditions.append(SleepReport.status == status_filter)
    if context_id is not None:
        conditions.append(SleepReport.context_id == context_id)
    if user_id is not None:
        conditions.append(SleepReport.user_id == user_id)

    count_stmt = select(func.count()).select_from(SleepReport)
    if conditions:
        count_stmt = count_stmt.where(*conditions)
    count_result = await db.execute(count_stmt)
    total = count_result.scalar() or 0

    stmt = select(SleepReport)
    if conditions:
        stmt = stmt.where(*conditions)
    result = await db.execute(
        stmt.order_by(SleepReport.started_at.desc()).limit(limit).offset(offset)
    )
    reports = list(result.scalars().all())

    return SleepReportListResponse(
        reports=[SleepReportSummary.model_validate(r, from_attributes=True) for r in reports],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{report_id}", response_model=SleepReportDetailResponse)
async def get_sleep_report_detail(
    report_id: UUID,
    _admin: dict = Depends(require_admin),  # noqa: ARG001 - FastAPI dep, access guard only
    db: AsyncSession = Depends(get_db),
) -> SleepReportDetailResponse:
    """Get a single Sleep Maintenance report with its full action audit log.

    Admin-only.
    """
    report_result = await db.execute(select(SleepReport).where(SleepReport.id == report_id))
    report = report_result.scalar_one_or_none()
    if not report:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Sleep report {report_id} not found.",
        )

    actions_result = await db.execute(
        select(SleepAction).where(SleepAction.report_id == report_id).order_by(SleepAction.id)
    )
    actions = list(actions_result.scalars().all())

    return SleepReportDetailResponse(
        report=SleepReportDetail.model_validate(report, from_attributes=True),
        actions=[SleepActionItem.model_validate(a, from_attributes=True) for a in actions],
        action_count=len(actions),
    )
