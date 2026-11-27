"""Sleep Reports API Routes.

Two endpoints share one ``SleepReporterService`` instance:

- ``GET /api/v1/admin/sleep-reports`` — admin-only, sees every workspace.
  Its detail route can leave the actions out (``include_actions=false``) and
  ``GET /api/v1/admin/sleep-reports/{report_id}/actions`` pages them (#2842).
- ``GET /api/v1/workspaces/{workspace_id}/sleep-reports`` —
  workspace owner / admin scoped via
  ``PermissionService.check_workspace_admin``. The path-bound
  ``workspace_id`` is the only allowed scope.

Both endpoints accept the same status / source / limit / offset / user_id /
context_id filters; the workspace-scoped one omits ``workspace_id``
from the query string because it comes from the path.

Issue #526: workspace-scoped sleep reports view.
Issue #179: Sleep Report admin UI (split from #104).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import get_current_user, require_admin
from db.base import get_db
from models.api_base import TZAwareBaseModel
from models.auth import Workspace
from models.sleep import SLEEP_REPORT_SOURCE_ALL, SleepReportSourceFilter
from services.capacity_lock import ensure_not_capacity_locked
from services.permission_service import PermissionService
from services.sleep_reporter_service import SleepReporterService
from utils.logger import get_logger
from utils.query_bounds import MAX_OFFSET, NO_NUL_PATTERN

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
    # #2844: the workspace's name, resolved per response like ``context_name``.
    # None for a report without a workspace id and for a workspace that is
    # soft-deleted or gone (the cost aggregation rows have the same rule).
    workspace_name: str | None = None
    context_id: UUID | None
    context_name: str | None = None
    # #1201: email of the user whose partition this run belongs to. Sleep runs
    # per (user_id, workspace_id, context_id), so a workspace-scoped list can
    # show the same context on multiple rows; this disambiguates them. None
    # when the user_id is a non-human/connector identity absent from ``users``,
    # or — on the workspace-scoped routes (#1882) — an account that is not a
    # current member of that workspace (the frontend falls back to a shortened
    # user_id).
    user_email: str | None = None
    status: str
    # #2219: 'sleep' (a Sleep Maintenance run) or 'analysis' (a Memory Analysis
    # run's spend row, written since #2088 also when it failed or was cancelled).
    source: str
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
    # #3019: the phases this run's budget limited, in run order: 'skipped'
    # (not run, the budget was used up at entry) or 'cut' (stopped with work
    # left). Derived from the stored phase results (``SleepReport``'s
    # property); {} when none was limited.
    budget_limited_phases: dict[str, Literal["skipped", "cut"]] = Field(default_factory=dict)

    @field_validator("budget_limited_phases", mode="before")
    @classmethod
    def _budget_limited_phases_map(cls, value: Any) -> Any:
        """Anything that is not a dict (a row without results) reads as {}."""
        return value if isinstance(value, dict) else {}


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


class SleepActionListResponse(BaseModel):
    """One page of a sleep report's actions (#2842)."""

    actions: list[SleepActionItem]
    total: int
    limit: int
    offset: int


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


async def _attach_workspace_names(service: SleepReporterService, reports: list[Any]) -> None:
    """Set ``workspace_name`` on each report of one response (#2844).

    One query for the whole response, none when no report has a workspace id.
    Only live workspaces resolve: the others keep ``None``.
    """
    workspace_ids = {r.workspace_id for r in reports if r.workspace_id is not None}
    names = await service.resolve_workspace_names(workspace_ids)
    for r in reports:
        r.workspace_name = names.get(r.workspace_id) if r.workspace_id is not None else None


def _raise_report_not_found(report_id: UUID) -> NoReturn:
    """The admin routes' one answer for a report id with no row."""
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Sleep report {report_id} not found.",
    )


# #2219: shared by both list endpoints.
_SOURCE_QUERY_DESCRIPTION = (
    "Which rows to list: 'sleep' (Sleep Maintenance runs), 'analysis' "
    "(Memory Analysis spend rows) or 'all' (default). Unknown values are a 422."
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
    offset: int = Query(0, ge=0, le=MAX_OFFSET),
    status_filter: str | None = Query(None, alias="status"),
    source: SleepReportSourceFilter = Query(
        SLEEP_REPORT_SOURCE_ALL, description=_SOURCE_QUERY_DESCRIPTION
    ),
    context_id: UUID | None = Query(None),
    user_id: str | None = Query(None, pattern=NO_NUL_PATTERN),
    workspace_id: UUID | None = Query(
        None,
        description=(
            "Only reports whose stored ``workspace_id`` is this id (#2844). "
            "ANDed with the other filters; a report without a workspace id never matches."
        ),
    ),
) -> SleepReportListResponse:
    """List Sleep Maintenance reports with filters and pagination.

    Admin-only. Filterable by status, source, context_id, user_id,
    workspace_id. Sorted by started_at DESC.
    """
    _validate_status(status_filter)

    service = SleepReporterService(db)
    reports, total = await service.list_reports(
        workspace_id=workspace_id,
        status_filter=status_filter,
        context_id=context_id,
        user_id=user_id,
        source=source,
        limit=limit,
        offset=offset,
    )

    # Batch-load referenced contexts in one query to avoid N+1.
    context_ids = {r.context_id for r in reports if r.context_id}  # type: ignore[misc]
    ctx_map = await service.resolve_context_names(context_ids)  # type: ignore[arg-type]
    for r in reports:
        r.context_name = ctx_map.get(r.context_id) if r.context_id else None  # type: ignore[attr-defined]

    # #2844: one query names the workspaces of the page.
    await _attach_workspace_names(service, reports)

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
    include_actions: bool = Query(
        True,
        description=(
            "``false`` answers ``actions: []`` and reads no action row; "
            "``action_count`` is still the run's total. Page the actions with "
            "``GET /admin/sleep-reports/{report_id}/actions``."
        ),
    ),
) -> SleepReportDetailResponse:
    """Get a single Sleep Maintenance report with its full action audit log.

    Admin-only. With ``include_actions=false`` (#2842) the answer holds the
    report's own row and the number of actions, and no action.
    """
    service = SleepReporterService(db)
    actions: list[SleepActionItem]
    if include_actions:
        result = await service.get_report_detail(report_id)
        if not result:
            _raise_report_not_found(report_id)
        report, action_rows = result
        actions = [SleepActionItem.model_validate(a, from_attributes=True) for a in action_rows]
        action_count = len(actions)
    else:
        found = await service.get_report(report_id)
        if found is None:
            _raise_report_not_found(report_id)
        report = found
        actions = []
        action_count = await service.count_actions(report_id)

    context_name, context_deleted = await service.resolve_context_name(
        report.context_id  # type: ignore[arg-type]
    )

    report.context_name = context_name  # type: ignore[attr-defined]
    report.context_deleted = context_deleted  # type: ignore[attr-defined]
    await _attach_workspace_names(service, [report])  # #2844
    # #1201: resolve the owning user's email (None → frontend fallback).
    uid_map = await service.resolve_user_labels({report.user_id})
    report.user_email = uid_map.get(report.user_id)  # type: ignore[attr-defined]
    report_detail = SleepReportDetail.model_validate(report, from_attributes=True)

    return SleepReportDetailResponse(
        report=report_detail,
        actions=actions,
        action_count=action_count,
    )


@router.get(
    "/admin/sleep-reports/{report_id}/actions",
    response_model=SleepActionListResponse,
    summary="Page the actions of a Sleep Maintenance report (admin)",
    tags=["sleep-reports"],
)
async def list_sleep_report_actions(
    report_id: UUID,
    _admin: dict = Depends(require_admin),  # noqa: ARG001 - FastAPI dep
    db: AsyncSession = Depends(get_db),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0, le=MAX_OFFSET),
) -> SleepActionListResponse:
    """Read one page of a report's action audit log (#2842).

    Admin-only, read only. Actions come in ``id`` order, the order the run
    recorded them: a running report's new actions land on later pages.
    ``total`` is the run's action count, the detail route's ``action_count``.
    An unknown report answers 404, as the detail route.
    """
    service = SleepReporterService(db)
    if not await service.report_exists(report_id):
        _raise_report_not_found(report_id)

    total = await service.count_actions(report_id)
    rows = await service.list_actions(report_id, limit=limit, offset=offset)
    return SleepActionListResponse(
        actions=[
            SleepActionItem(
                id=row.id,
                phase=row.phase,
                action_type=row.action_type,
                memory_id=row.memory_id,
                target_id=row.target_id,
                details=row.details,
                created_at=row.created_at,
            )
            for row in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
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
    offset: int = Query(0, ge=0, le=MAX_OFFSET),
    status_filter: str | None = Query(None, alias="status"),
    source: SleepReportSourceFilter = Query(
        SLEEP_REPORT_SOURCE_ALL, description=_SOURCE_QUERY_DESCRIPTION
    ),
    context_id: UUID | None = Query(None),
    user_id: str | None = Query(None, pattern=NO_NUL_PATTERN),
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
    # #1941: defence in depth — Free has no Sleep today, but a plan override
    # could grant it; a report describes memory content (MCP blocks it too).
    await ensure_not_capacity_locked(db, workspace_id)

    service = SleepReporterService(db)
    reports, total = await service.list_reports(
        workspace_id=workspace_id,
        status_filter=status_filter,
        context_id=context_id,
        user_id=user_id,
        source=source,
        limit=limit,
        offset=offset,
    )

    # Batch-load referenced contexts in one query to avoid N+1.
    context_ids = {r.context_id for r in reports if r.context_id}  # type: ignore[misc]
    ctx_map = await service.resolve_context_names(context_ids)  # type: ignore[arg-type]
    for r in reports:
        r.context_name = ctx_map.get(r.context_id) if r.context_id else None  # type: ignore[attr-defined]

    # #2844: one query names the workspaces of the page.
    await _attach_workspace_names(service, reports)

    # #1201: batch-resolve user_id → email so same-named contexts on different
    # partitions are distinguishable. None → the frontend falls back to a
    # shortened user_id. #1882: current members of this workspace only — a
    # report outlives the membership of the account it ran for.
    user_ids = {r.user_id for r in reports}  # type: ignore[misc]
    uid_map = await service.resolve_user_labels(user_ids, member_of=workspace_id)
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
    # #1941: defence in depth — Free has no Sleep today, but a plan override
    # could grant it; a report describes memory content (MCP blocks it too).
    await ensure_not_capacity_locked(db, workspace_id)

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
    await _attach_workspace_names(service, [report])  # #2844
    # #1201: resolve the owning user's email (None → frontend fallback).
    # #1882: only while that user is still a member of this workspace.
    uid_map = await service.resolve_user_labels({report.user_id}, member_of=workspace_id)
    report.user_email = uid_map.get(report.user_id)  # type: ignore[attr-defined]
    report_detail = SleepReportDetail.model_validate(report, from_attributes=True)

    return SleepReportDetailResponse(
        report=report_detail,
        actions=[SleepActionItem.model_validate(a, from_attributes=True) for a in actions],
        action_count=len(actions),
    )
