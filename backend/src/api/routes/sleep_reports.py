"""Sleep Reports API Routes.

Two endpoints share one ``SleepReporterService`` instance:

- ``GET /api/v1/admin/sleep-reports`` — admin-only, sees every workspace.
- ``GET /api/v1/workspaces/{workspace_id}/sleep-reports`` —
  workspace owner / admin scoped via
  ``PermissionService.check_workspace_admin``. The path-bound
  ``workspace_id`` is the only allowed scope.

Both endpoints accept the same status / limit / offset / user_id /
context_id filters; the workspace-scoped one omits ``workspace_id``
from the query string because it comes from the path.

Issue #526: workspace-scoped sleep reports view.
Issue #179: Sleep Report admin UI (split from #104).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import get_current_user, require_admin
from db.base import get_db
from models.api_base import TZAwareBaseModel
from models.auth import Workspace
from services.permission_service import PermissionService
from services.sleep_reporter_service import SleepReporterService
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter()


# ============================================================================
# Schemas
# ============================================================================


class SleepReportSummary(TZAwareBaseModel):
    """Sleep report summary for list view."""

    id: UUID
    user_id: str
    workspace_id: UUID | None
    context_id: UUID | None
    context_name: str | None = None
    # #1201: email of the user whose partition this run belongs to. Sleep runs
    # per (user_id, workspace_id, context_id), so a workspace-scoped list can
    # show the same context on multiple rows; this disambiguates them. None
    # when the user_id is a non-human/connector identity absent from ``users``
    # (the frontend falls back to a shortened user_id).
    user_email: str | None = None
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
    # #1183: judge-LLM calls that raised across all phases — the magnitude
    # behind a 'degraded'/'failed' grading, exposed top-level so dashboards
    # don't have to parse per-phase JSON blobs.
    llm_call_failures: int = 0


class SleepReportDetail(SleepReportSummary):
    """Sleep report full detail."""

    context_deleted: bool = False
    embedding_calls_made: int
    error_message: str | None
    edge_discovery_result: dict[str, Any] | None
    dedup_result: dict[str, Any] | None
    # #1209: merge_retention phase (purge window); None on pre-#1209 reports.
    merge_retention_result: dict[str, Any] | None = None
    importance_result: dict[str, Any] | None
    consolidation_result: dict[str, Any] | None
    reindex_result: dict[str, Any] | None


class SleepActionItem(TZAwareBaseModel):
    """Sleep action audit log entry."""

    id: int
    phase: str
    action_type: str
    memory_id: UUID | None
    target_id: UUID | None
    details: dict[str, Any] | None
    created_at: datetime


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
# Shared validation
# ============================================================================

_VALID_STATUSES = {"running", "completed", "degraded", "failed", "cancelled", "rolled_back"}


def _validate_status(status_filter: str | None) -> None:
    """Reject unknown status values with a 400 (not a 500)."""
    if status_filter is not None and status_filter not in _VALID_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid status. Must be one of: {sorted(_VALID_STATUSES)}",
        )


# ============================================================================
# Admin (cross-workspace) endpoints
# ============================================================================


@router.get(
    "/admin/sleep-reports",
    response_model=SleepReportListResponse,
    summary="List Sleep Maintenance reports (admin)",
    tags=["sleep-reports"],
)
async def list_sleep_reports(
    _admin: dict = Depends(require_admin),  # noqa: ARG001 - FastAPI dep
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
    _validate_status(status_filter)

    service = SleepReporterService(db)
    reports, total = await service.list_reports(
        status_filter=status_filter,
        context_id=context_id,
        user_id=user_id,
        limit=limit,
        offset=offset,
    )

    # Batch-load referenced contexts in one query to avoid N+1.
    context_ids = {r.context_id for r in reports if r.context_id}  # type: ignore[misc]
    ctx_map = await service.resolve_context_names(context_ids)  # type: ignore[arg-type]
    for r in reports:
        r.context_name = ctx_map.get(r.context_id) if r.context_id else None  # type: ignore[attr-defined]

    # #1201: batch-resolve user_id → email so same-named contexts on different
    # partitions are distinguishable. None → the frontend falls back to a
    # shortened user_id.
    user_ids = {r.user_id for r in reports}  # type: ignore[misc]
    uid_map = await service.resolve_user_labels(user_ids)
    for r in reports:
        r.user_email = uid_map.get(r.user_id)  # type: ignore[attr-defined]

    return SleepReportListResponse(
        reports=[SleepReportSummary.model_validate(r, from_attributes=True) for r in reports],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/admin/sleep-reports/{report_id}",
    response_model=SleepReportDetailResponse,
    summary="Get a single Sleep Maintenance report (admin)",
    tags=["sleep-reports"],
)
async def get_sleep_report_detail(
    report_id: UUID,
    _admin: dict = Depends(require_admin),  # noqa: ARG001 - FastAPI dep
    db: AsyncSession = Depends(get_db),
) -> SleepReportDetailResponse:
    """Get a single Sleep Maintenance report with its full action audit log.

    Admin-only.
    """
    service = SleepReporterService(db)
    result = await service.get_report_detail(report_id)
    if not result:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Sleep report {report_id} not found.",
        )

    report, actions = result

    context_name, context_deleted = await service.resolve_context_name(
        report.context_id  # type: ignore[arg-type]
    )

    report.context_name = context_name  # type: ignore[attr-defined]
    report.context_deleted = context_deleted  # type: ignore[attr-defined]
    # #1201: resolve the owning user's email (None → frontend fallback).
    uid_map = await service.resolve_user_labels({report.user_id})
    report.user_email = uid_map.get(report.user_id)  # type: ignore[attr-defined]
    report_detail = SleepReportDetail.model_validate(report, from_attributes=True)

    return SleepReportDetailResponse(
        report=report_detail,
        actions=[SleepActionItem.model_validate(a, from_attributes=True) for a in actions],
        action_count=len(actions),
    )


# ============================================================================
# Workspace-scoped endpoints
# ============================================================================


async def _enforce_sleep_plan(db: AsyncSession, workspace_id: UUID) -> None:
    """Gate the Sleep-report view to plans that support Sleep Maintenance.

    Sleep is Pro-only (``sleep_enabled_contexts_limit`` is 0 on free/basic;
    addons cannot lift a zero-base tier per the #560/#569 zero-floor rule), so
    a free/basic workspace can never produce a report. Block the view too —
    mirroring Memory Analysis — rather than relying on an always-empty list
    (defense-in-depth, #1137).
    """
    result = await db.execute(select(Workspace).where(Workspace.id == workspace_id))
    workspace = result.scalar_one_or_none()
    if workspace is None or workspace.effective_sleep_enabled_contexts_limit <= 0:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Sleep Maintenance reports require the Pro plan. "
                "Upgrade your plan to access Sleep Maintenance reports."
            ),
        )


@router.get(
    "/workspaces/{workspace_id}/sleep-reports",
    response_model=SleepReportListResponse,
    summary="List Sleep Maintenance reports scoped to one workspace",
    tags=["workspaces"],
)
async def workspace_list_sleep_reports(
    workspace_id: UUID,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    status_filter: str | None = Query(None, alias="status"),
    context_id: UUID | None = Query(None),
    user_id: str | None = Query(None),
) -> SleepReportListResponse:
    """List Sleep Maintenance reports for a single workspace.

    Requires workspace **owner** or **admin** role.

    Cross-workspace probing is impossible here: the path-bound
    ``workspace_id`` is the only scope passed to the service, and
    ``check_workspace_admin`` rejects callers without admin/owner
    membership in *that specific* workspace before the query runs.
    """
    _validate_status(status_filter)

    perm_service = PermissionService(db)
    await perm_service.check_workspace_admin(user["user_id"], workspace_id)
    await _enforce_sleep_plan(db, workspace_id)

    service = SleepReporterService(db)
    reports, total = await service.list_reports(
        workspace_id=workspace_id,
        status_filter=status_filter,
        context_id=context_id,
        user_id=user_id,
        limit=limit,
        offset=offset,
    )

    # Batch-load referenced contexts in one query to avoid N+1.
    context_ids = {r.context_id for r in reports if r.context_id}  # type: ignore[misc]
    ctx_map = await service.resolve_context_names(context_ids)  # type: ignore[arg-type]
    for r in reports:
        r.context_name = ctx_map.get(r.context_id) if r.context_id else None  # type: ignore[attr-defined]

    # #1201: batch-resolve user_id → email so same-named contexts on different
    # partitions are distinguishable. None → the frontend falls back to a
    # shortened user_id.
    user_ids = {r.user_id for r in reports}  # type: ignore[misc]
    uid_map = await service.resolve_user_labels(user_ids)
    for r in reports:
        r.user_email = uid_map.get(r.user_id)  # type: ignore[attr-defined]

    return SleepReportListResponse(
        reports=[SleepReportSummary.model_validate(r, from_attributes=True) for r in reports],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/workspaces/{workspace_id}/sleep-reports/{report_id}",
    response_model=SleepReportDetailResponse,
    summary="Get a single Sleep Maintenance report scoped to one workspace",
    tags=["workspaces"],
)
async def workspace_get_sleep_report_detail(
    workspace_id: UUID,
    report_id: UUID,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SleepReportDetailResponse:
    """Get a single Sleep Maintenance report with its full action audit log.

    Requires workspace **owner** or **admin** role.

    Returns 404 (not 403) when the report exists but belongs to a
    different workspace — uniform disclosure policy per #389 / CWE-639.
    """
    perm_service = PermissionService(db)
    await perm_service.check_workspace_admin(user["user_id"], workspace_id)
    await _enforce_sleep_plan(db, workspace_id)

    service = SleepReporterService(db)
    result = await service.get_report_detail(report_id, workspace_id=workspace_id)
    if not result:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Sleep report {report_id} not found.",
        )

    report, actions = result

    context_name, context_deleted = await service.resolve_context_name(
        report.context_id  # type: ignore[arg-type]
    )

    report.context_name = context_name  # type: ignore[attr-defined]
    report.context_deleted = context_deleted  # type: ignore[attr-defined]
    # #1201: resolve the owning user's email (None → frontend fallback).
    uid_map = await service.resolve_user_labels({report.user_id})
    report.user_email = uid_map.get(report.user_id)  # type: ignore[attr-defined]
    report_detail = SleepReportDetail.model_validate(report, from_attributes=True)

    return SleepReportDetailResponse(
        report=report_detail,
        actions=[SleepActionItem.model_validate(a, from_attributes=True) for a in actions],
        action_count=len(actions),
    )
