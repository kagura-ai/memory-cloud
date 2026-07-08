"""Admin Sleep Maintenance Trigger API.

Admin-only endpoint for manually triggering Sleep Maintenance runs, used
for dogfooding and verification loops after config changes (issue #247).

Phase 1 scope: self-scoped (runs only for the calling admin's own
user_id). Phase 2 will expand to cross-user/workspace runs with audit log.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse

from auth.dependencies import AdminUser
from db.base import get_db
from models.auth import Context
from models.sleep import SleepReport
from services.sleep.orchestrator import SleepOrchestrator
from services.sleep.reporter import SleepReporter
from utils.datetime import to_utc_iso, utcnow
from utils.exceptions import MemoryCloudException
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/admin/sleep", tags=["admin-sleep"])


# ============================================================================
# Schemas
# ============================================================================


class SleepRunRequest(BaseModel):
    """Body for POST /admin/sleep/run.

    Attributes:
        context_id: Target context. When null, runs against every context
            owned by the calling admin (self-scoped).
    """

    context_id: UUID | None = None


class SleepRunResponse(BaseModel):
    """202 response for POST /admin/sleep/run.

    Attributes:
        report_ids: IDs of the newly-created sleep reports, one per
            scheduled context. For a single-context request this list
            always has exactly one element.
    """

    report_ids: list[UUID]


# ============================================================================
# Background execution
# ============================================================================


async def _run_sleep_job(
    report_id: UUID,
    user_id: str,
    workspace_id: str,
    context_id: str,
) -> None:
    """Execute sleep phases against a pre-created report.

    Opens its own DB session so the HTTP request handler is free to return
    as soon as the reports are committed. On exception, marks the report
    as ``failed`` so the UI never sees a perpetually-running row.
    """
    from db.base import get_db as _get_db

    async for db in _get_db():
        try:
            report = await db.get(SleepReport, report_id)
            if report is None:
                logger.error(
                    "manual_sleep_report_missing",
                    report_id=str(report_id),
                )
                return

            # The endpoint pre-creates every report up front so the 202
            # response can return their ids, which means queued jobs in a
            # multi-context batch would otherwise all share a stale
            # creation timestamp. Reset to the actual execution start so
            # duration analytics and list ordering reflect reality.
            report.started_at = utcnow()
            await db.flush()

            orchestrator = SleepOrchestrator(db)
            await orchestrator.run(
                user_id,
                workspace_id,
                context_id,
                report=report,
            )
            await db.commit()
            logger.info(
                "manual_sleep_run_completed",
                report_id=str(report_id),
                user_id=user_id,
                context_id=context_id,
            )
        except Exception as e:
            await db.rollback()
            logger.error(
                "manual_sleep_run_failed",
                report_id=str(report_id),
                user_id=user_id,
                context_id=context_id,
                error=str(e),
                exc_info=True,
            )
            try:
                report = await db.get(SleepReport, report_id)
                if report is not None and report.status == "running":
                    reporter = SleepReporter(db)
                    await reporter.fail_report(report, str(e))
                    await db.commit()
            except Exception:
                await db.rollback()
                logger.error(
                    "manual_sleep_fail_report_failed",
                    report_id=str(report_id),
                    exc_info=True,
                )
        return


async def _run_sleep_batch(
    jobs: list[tuple[UUID, str, str]],
    user_id: str,
) -> None:
    """Execute a list of sleep jobs sequentially.

    Serialized on purpose: each job opens its own DB session, and running
    jobs in parallel when an admin triggers "run all" would let a handful
    of contexts saturate the connection pool (pool_size=5, max_overflow=10
    → 15 connections total) and starve the rest of the API.
    """
    for report_id, workspace_id, context_id in jobs:
        await _run_sleep_job(report_id, user_id, workspace_id, context_id)


def _log_background_task_result(task: asyncio.Task[Any]) -> None:
    """Surface any exception that escaped the sleep batch task."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            "manual_sleep_background_task_crashed",
            error=str(exc),
            exc_info=exc,
        )


# ============================================================================
# Endpoint
# ============================================================================


@router.post(
    "/run",
    response_model=SleepRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Trigger Sleep Maintenance (admin manual run)",
    responses={
        409: {
            "description": "A sleep run is already in progress for this user.",
            "content": {
                "application/json": {
                    "example": {
                        "error": "SLEEP-002",
                        "message": "A sleep run is already in progress for this user.",
                        "details": {
                            "running_report_id": "00000000-0000-0000-0000-000000000000",
                            "started_at": "2026-04-09T12:00:00Z",
                        },
                    }
                }
            },
        },
    },
)
async def trigger_sleep_run(
    request: SleepRunRequest,
    admin: AdminUser,
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Manually trigger a Sleep Maintenance run for the calling admin.

    Self-scoped: the run targets only contexts the admin created (matched
    on ``Context.created_by``), excluding soft-deleted contexts and
    contexts with ``sleep_mode='skip'``. When ``context_id`` is provided a
    single run is scheduled for that context; otherwise one run is
    scheduled per eligible context.

    Returns:
        202 with ``{"report_ids": [...]}``. Phase execution happens in the
        background; poll ``GET /admin/sleep-reports/{id}`` for progress.

    Raises:
        409 ``SLEEP-002`` if this user already has a sleep run
        with ``status='running'`` — the response body includes
        ``running_report_id`` and ``started_at`` so the UI can link to it.
        404 ``SLEEP-003`` if no eligible contexts match.

    Phase 1 limitation:
        The 409 guard is best-effort under concurrent requests. Two
        simultaneous POSTs could each pass the check before either commits,
        resulting in overlapping batches. Acceptable for single-admin
        dogfooding; Phase 2 will enforce exactly-once via a unique partial
        index on ``sleep_reports(user_id) WHERE status='running'``.
    """
    user_id = admin.get("user_id")
    if not user_id:
        # require_admin already guarantees authentication, but be defensive.
        raise MemoryCloudException(
            message="Admin user id missing from session.",
            status_code=500,
            error_code="SLEEP-001",
        )

    # ---- Concurrency guard --------------------------------------------------
    # Oldest-first so the returned running_report_id is deterministic: in a
    # multi-context batch several reports can share status='running', and
    # the oldest is the most likely to be currently executing — which is
    # the most useful target for the UI's "view running report" link.
    running_stmt = (
        select(SleepReport.id, SleepReport.started_at)
        .where(SleepReport.user_id == user_id)
        .where(SleepReport.status == "running")
        .order_by(SleepReport.started_at.asc())
        .limit(1)
    )
    running_row = (await db.execute(running_stmt)).first()
    if running_row is not None:
        running_report_id, started_at = running_row
        raise MemoryCloudException(
            message="A sleep run is already in progress for this user.",
            status_code=409,
            error_code="SLEEP-002",
            running_report_id=str(running_report_id),
            started_at=to_utc_iso(started_at),
        )

    # ---- Resolve target contexts --------------------------------------------
    # Self-scoped: only contexts the admin created. Skipping soft-deleted
    # rows and any context whose sleep_mode is "skip" so we never enqueue
    # an orphan report.
    target_stmt = (
        select(Context.workspace_id, Context.id)
        .where(Context.created_by == user_id)
        .where(Context.deleted_at.is_(None))
        .where(Context.sleep_mode != "skip")
    )
    if request.context_id is not None:
        target_stmt = target_stmt.where(Context.id == request.context_id)

    target_rows = (await db.execute(target_stmt)).all()
    if not target_rows:
        raise MemoryCloudException(
            message=("No eligible contexts found for this admin to run sleep maintenance on."),
            status_code=404,
            error_code="SLEEP-003",
            context_id=str(request.context_id) if request.context_id else None,
        )

    # ---- Create reports + schedule background runs --------------------------
    reporter = SleepReporter(db)
    created: list[tuple[UUID, str, str]] = []  # (report_id, workspace_id, context_id)
    for workspace_uuid, context_uuid in target_rows:
        workspace_id = str(workspace_uuid)
        context_id = str(context_uuid)
        report = await reporter.create_report(user_id, workspace_id, context_id)
        created.append((report.id, workspace_id, context_id))

    # Commit so the reports become visible to concurrent requests and to the
    # batch task (which opens its own DB session).
    await db.commit()

    task = asyncio.create_task(_run_sleep_batch(created, user_id))
    task.add_done_callback(_log_background_task_result)

    logger.info(
        "manual_sleep_run_scheduled",
        user_id=user_id,
        report_count=len(created),
        explicit_context=request.context_id is not None,
    )

    # Use JSONResponse directly so the 202 status_code from the decorator is
    # preserved alongside the serialized body.
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content=SleepRunResponse(report_ids=[report_id for report_id, _, _ in created]).model_dump(
            mode="json"
        ),
    )


# ============================================================================
# Per-merge undo (#1209)
# ============================================================================


class UndoMergeResponse(BaseModel):
    """200 response for POST /admin/sleep/actions/{action_id}/undo-merge.

    Attributes:
        restored_memory_id: The merge loser brought back into recall.
        winner_id: The merge winner (unchanged by the undo).
        report_id: The sleep report whose audit log records both the merge
            and this undo.
        undone_action_id: The original merge action that was undone.
    """

    restored_memory_id: str
    winner_id: str | None
    report_id: str
    undone_action_id: int


_UNDO_ERROR_STATUS = {
    "action_not_found": 404,
    "memory_purged": 410,
    "already_restored": 409,
    "not_a_merge": 400,
    "not_merge_deleted": 409,
}


@router.post(
    "/actions/{action_id}/undo-merge",
    response_model=UndoMergeResponse,
    summary="Undo one dedup merge (restore the merged-away memory)",
    responses={
        404: {"description": "Action not found or not owned by the caller."},
        409: {"description": "Memory already restored, or deleted by something else."},
        410: {
            "description": "The merge loser was hard-deleted by the retention "
            "policy (sleep_merge_retention_days) — no longer restorable."
        },
    },
)
async def undo_merge(
    action_id: int,
    admin: AdminUser,
    db: AsyncSession = Depends(get_db),
) -> UndoMergeResponse:
    """Restore the loser of ONE dedup merge — row and Qdrant vector (#1209).

    Self-scoped like the manual sleep trigger: the merge's report must belong
    to the calling admin. The undo is itself audited as an ``undo_merge``
    action on the same sleep report, so the merge's full history reads out of
    one audit log. Reversibility is bounded by the declared retention window;
    a purged loser returns 410 with the setting named.
    """
    from services.sleep.undo import UndoMergeError, undo_merge_action

    user_id = admin.get("user_id")
    if not user_id:
        raise MemoryCloudException(
            message="Admin user id missing from session.",
            status_code=500,
            error_code="SLEEP-001",
        )

    try:
        summary = await undo_merge_action(db, action_id, acting_user_id=user_id)
    except UndoMergeError as exc:
        raise MemoryCloudException(
            message=exc.message,
            status_code=_UNDO_ERROR_STATUS.get(exc.code, 400),
            error_code=f"SLEEP-UNDO-{exc.code}",
        ) from exc

    return UndoMergeResponse(**summary)
