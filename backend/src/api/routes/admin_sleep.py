"""Admin Sleep Maintenance Trigger API.

Admin-only endpoint for manually triggering Sleep Maintenance runs, used
for dogfooding and verification loops after config changes (issue #247).

``router`` is self-scoped: its two routes run Sleep and undo a merge for the
calling admin's own account, and refuse an operator principal.

``target_router`` (#2838) holds the same two actions for a target user: a run
for a named user's context and the undo of a merge of a named report. Both
take ``require_admin``, so a service operator can call them, and both write
an ``audit_logs`` row naming the caller.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from contextlib import aclosing
from datetime import timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Path, status
from pydantic import BaseModel
from sqlalchemy import Select, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse

from auth.dependencies import AdminAccountUser, AdminUser
from auth.system_admin_mode import is_operator_principal
from config.constants import TOMBSTONE_PURGER_CLAUSE
from db.base import get_db
from models.auth import AuditLog, Context, User
from models.memory import Memory
from models.sleep import SleepReport
from services.sleep.orchestrator import SleepOrchestrator
from services.sleep.reporter import SleepReporter
from utils.datetime import to_utc_iso, utcnow
from utils.exceptions import MemoryCloudException, NotFoundException
from utils.logger import get_logger
from utils.query_bounds import MAX_BIGINT, NO_NUL_PATTERN

logger = get_logger(__name__)

router = APIRouter(prefix="/admin/sleep", tags=["admin-sleep"])

# #2838: the same two actions for a target user. A second router because the
# paths sit under ``/admin/users`` and ``/admin/sleep-reports``; it stays in
# this module so the route tables classify both routers alike.
target_router = APIRouter(prefix="/admin", tags=["admin-sleep"])

# The ``audit_logs`` action of a run an admin or operator starts for a user.
SLEEP_RUN_TRIGGER_AUDIT_ACTION = "sleep_run_trigger"

# #2856: a ``running`` report that no transaction holds, that no batch of this
# process runs and that was started longer ago than this is a run whose
# process stopped. A live batch always has a report the guard of every
# process counts as live: the row of the job that runs (held from its claim
# to the commit that ends it), and, from that same commit on, the reports
# still queued, whose ``started_at`` the commit sets to its own time. So the
# bound only has to exceed the gap from a report's creation to its job's
# first statement, and the gap between two jobs (the point deletes after a
# commit, bounded, and the next session's connection), both seconds.
SLEEP_RUN_STALE_AFTER = timedelta(minutes=15)
# The ``error_message`` of a report whose run was interrupted (#2856).
SLEEP_RUN_INTERRUPTED = "interrupted: the process running this sleep run stopped before it ended"

# #2856: the batches this process runs, held strongly until they end (the loop
# keeps tasks only weakly), and the ids of their reports.
_IN_FLIGHT_BATCHES: set[asyncio.Task[None]] = set()
_LIVE_REPORT_IDS: set[UUID] = set()
# #2856: the one deadline a batch's cleanup shares, from its first step: the
# cancelled job's rollback (or the invalidation of its connection) and the
# close of the reports whose job did not return. A step the deadline cuts off
# leaves its reports to the next admitted run's guard.
SLEEP_BATCH_CLEANUP_SECONDS = 5.0
# How long the shutdown step waits for the cancelled batches: above the
# cleanup deadline, so a batch's cleanup ends before the database closes.
MANUAL_SLEEP_SHUTDOWN_WAIT_SECONDS = 10.0


class _CleanupDeadline:
    """The deadline a batch's cleanup steps share, set by the first step that asks (#2856)."""

    def __init__(self) -> None:
        self._at: float | None = None

    def at(self) -> float:
        """The loop time by which the batch's cleanup ends."""
        if self._at is None:
            self._at = asyncio.get_running_loop().time() + SLEEP_BATCH_CLEANUP_SECONDS
        return self._at


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
    *,
    queued: Sequence[UUID] = (),
    cleanup: _CleanupDeadline | None = None,
) -> None:
    """Execute sleep phases against a pre-created report.

    Opens its own DB session so the HTTP request handler is free to return
    as soon as the reports are committed. On exception, marks the report
    as ``failed`` so the UI never sees a perpetually-running row.

    The first statement locks the report row (``FOR NO KEY UPDATE``), and the
    run's one transaction holds it to the end: a held row is how the run
    guard of every process tells a live run from an interrupted one (#2856).
    The phases run in a savepoint, so a run that raises is marked ``failed``
    in that same transaction, and the commit that releases the row also sets
    ``started_at`` of the batch's ``queued`` reports to now: at no instant is
    the row released while the queued reports still carry an older time.
    A report that is no longer ``running`` (a guard closed it) is not run.
    A cancelled job rolls its session back, which releases the row, and
    re-raises; ``_run_sleep_batch`` closes its report. The rollback takes its
    time from the batch's ``cleanup`` deadline.
    """
    from db.base import get_db as _get_db

    async for db in _get_db():
        try:
            report = (
                await db.execute(
                    select(SleepReport)
                    .where(SleepReport.id == report_id)
                    .with_for_update(key_share=True)
                )
            ).scalar_one_or_none()
            if report is None:
                logger.error(
                    "manual_sleep_report_missing",
                    report_id=str(report_id),
                )
                return
            if report.status != "running":
                logger.warning(
                    "manual_sleep_report_not_running",
                    report_id=str(report_id),
                    status=report.status,
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
            phases = await db.begin_nested()
            try:
                await orchestrator.run(
                    user_id,
                    workspace_id,
                    context_id,
                    report=report,
                )
            except Exception as e:
                logger.error(
                    "manual_sleep_run_failed",
                    report_id=str(report_id),
                    user_id=user_id,
                    context_id=context_id,
                    error=str(e),
                    exc_info=True,
                )
                await phases.rollback()
                await db.refresh(report)
                await SleepReporter(db).fail_report(report, str(e))
                await _refresh_queued_reports(db, queued)
                await db.commit()
                return
            await phases.commit()
            await _refresh_queued_reports(db, queued)
            await db.commit()
            # #2565: the archived / merged rows' points, now that the
            # tombstones are committed. Never raises.
            await orchestrator.delete_committed_points()
            logger.info(
                "manual_sleep_run_completed",
                report_id=str(report_id),
                user_id=user_id,
                context_id=context_id,
            )
        except asyncio.CancelledError:
            # The session holds the report's row: release it before the
            # batch closes the report in a session of its own.
            deadline = (cleanup or _CleanupDeadline()).at()
            await _release_cancelled_session(db, report_id, deadline)
            raise
        except Exception as e:
            # The job's transaction itself failed (its commit, or the
            # savepoint's rollback): close the report in a new one.
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
                    await _refresh_queued_reports(db, queued)
                    await db.commit()
            except Exception:
                await db.rollback()
                logger.error(
                    "manual_sleep_fail_report_failed",
                    report_id=str(report_id),
                    exc_info=True,
                )
        return


async def _refresh_queued_reports(db: AsyncSession, queued: Sequence[UUID]) -> None:
    """Set ``started_at`` of a batch's still-queued reports to now, in ``db``'s transaction (#2856).

    Called just before the commit that releases the running job's row, so
    the queued reports are fresh for the run guard from that instant on. A
    report a guard already closed is left as it is.
    """
    if not queued:
        return
    await db.execute(
        update(SleepReport)
        .where(SleepReport.id.in_(queued), SleepReport.status == "running")
        .values(started_at=utcnow())
        .execution_options(synchronize_session=False)
    )


def _held_running_reports(*conditions: Any) -> Select[SleepReport]:
    """The ``running`` reports matching ``conditions`` that no other transaction holds.

    Locked ``FOR NO KEY UPDATE SKIP LOCKED`` in id order: the rows come back
    held by the caller's transaction, and a row a live job holds is left out.
    """
    return (
        select(SleepReport)
        .where(SleepReport.status == "running", *conditions)
        .order_by(SleepReport.id)
        .with_for_update(key_share=True, skip_locked=True)
    )


async def _release_cancelled_session(db: AsyncSession, report_id: UUID, deadline: float) -> None:
    """Roll back a cancelled job's session, or invalidate it, by ``deadline``; never raises an error."""
    try:
        async with asyncio.timeout_at(deadline):
            await db.rollback()
        return
    except Exception as e:  # noqa: BLE001 - the connection is dropped instead
        logger.warning(
            "manual_sleep_cancelled_rollback_failed",
            report_id=str(report_id),
            error_class=type(e).__name__,
        )
    try:
        async with asyncio.timeout_at(deadline):
            await db.invalidate()
    except Exception as e:  # noqa: BLE001 - best effort; the server ends the transaction
        logger.warning(
            "manual_sleep_cancelled_invalidate_failed",
            report_id=str(report_id),
            error_class=type(e).__name__,
        )


async def _close_interrupted_reports(report_ids: list[UUID], deadline: float) -> None:
    """Mark the ``running`` reports of jobs that did not return as interrupted (#2856).

    In a session of its own, on the rows no other transaction holds, the
    whole write ending by ``deadline``. Logs and never raises an error; a
    cancellation is re-raised. A report it misses is closed by the next
    admitted run's guard.
    """
    from db.base import get_db as _get_db

    async def _close() -> int:
        async with aclosing(_get_db()) as sessions:
            async for db in sessions:
                await db.execute(text("SET LOCAL lock_timeout = '2s'"))
                held = (
                    (await db.execute(_held_running_reports(SleepReport.id.in_(report_ids))))
                    .scalars()
                    .all()
                )
                reporter = SleepReporter(db)
                for report in held:
                    await reporter.fail_report(report, SLEEP_RUN_INTERRUPTED)
                await db.commit()
                return len(held)
        return 0

    try:
        async with asyncio.timeout_at(deadline):
            closed = await _close()
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 - the next admitted run's guard closes them
        logger.error(
            "manual_sleep_interrupted_close_failed",
            report_ids=[str(report_id) for report_id in report_ids],
            error_class=type(e).__name__,
        )
        return
    logger.warning(
        "manual_sleep_reports_interrupted",
        report_ids=[str(report_id) for report_id in report_ids],
        closed=closed,
    )


async def _run_sleep_batch(
    jobs: list[tuple[UUID, str, str]],
    user_id: str,
) -> None:
    """Execute a list of sleep jobs sequentially.

    Serialized on purpose: each job opens its own DB session, and running
    jobs in parallel when an admin triggers "run all" would let a handful
    of contexts saturate the connection pool (``DB_POOL_SIZE`` +
    ``DB_MAX_OVERFLOW``, 15 connections by default, #1980) and starve the
    rest of the API.

    The reports whose job has not returned when the batch ends (it was
    cancelled, or an error escaped a job) are closed as interrupted (#2856).
    The cancelled job's release and this close share one deadline,
    ``SLEEP_BATCH_CLEANUP_SECONDS`` from the first of them.
    """
    pending = [report_id for report_id, _, _ in jobs]
    cleanup = _CleanupDeadline()
    try:
        for index, (report_id, workspace_id, context_id) in enumerate(jobs):
            queued = [queued_id for queued_id, _, _ in jobs[index + 1 :]]
            await _run_sleep_job(
                report_id, user_id, workspace_id, context_id, queued=queued, cleanup=cleanup
            )
            pending.remove(report_id)
    finally:
        if pending:
            await _close_interrupted_reports(pending, cleanup.at())


def _start_sleep_batch(jobs: list[tuple[UUID, str, str]], user_id: str) -> None:
    """Start a batch in the background and track it until it ends (#2856).

    Called right after the route's commit, with no ``await`` in between: from
    then on the guard of this process sees the batch's reports as live.
    """
    report_ids = [report_id for report_id, _, _ in jobs]
    _LIVE_REPORT_IDS.update(report_ids)
    task = asyncio.create_task(_run_sleep_batch(jobs, user_id))
    _IN_FLIGHT_BATCHES.add(task)

    def _done(finished: asyncio.Task[Any]) -> None:
        _IN_FLIGHT_BATCHES.discard(finished)
        _LIVE_REPORT_IDS.difference_update(report_ids)
        _log_background_task_result(finished)

    task.add_done_callback(_done)


async def stop_manual_sleep_runs(timeout: float = MANUAL_SLEEP_SHUTDOWN_WAIT_SECONDS) -> None:
    """Cancel this process's manual Sleep batches and wait for them, bounded (#2856).

    Called at shutdown, while the database is still open, so each cancelled
    batch can close its reports. Never raises.
    """
    tasks = [task for task in _IN_FLIGHT_BATCHES if not task.done()]
    if not tasks:
        return
    for task in tasks:
        task.cancel()
    _, still_running = await asyncio.wait(tasks, timeout=timeout)
    logger.info(
        "manual_sleep_runs_stopped",
        cancelled=len(tasks),
        still_running=len(still_running),
    )


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


async def _raise_if_sleep_running(db: AsyncSession, user_id: str) -> None:
    """Refuse with 409 ``SLEEP-002`` while ``user_id`` has a live running report.

    The one concurrency guard of the manual triggers: the own-account route
    passes the caller, the target-user route (#2838) the user it runs for.

    #2856: a ``running`` report is live while another transaction holds its
    row (a job holds it from its first statement to its end), while a batch
    of this process runs it, or for ``SLEEP_RUN_STALE_AFTER`` after its
    ``started_at`` (a queued report's is moved to each commit of a job of its
    batch, ``_run_sleep_job``). A report that is none of these was left by a process that
    stopped: the guard marks it ``failed`` (``SLEEP_RUN_INTERRUPTED``) in the
    caller's transaction, whose commit also creates the new report, so a user
    never has two ``running`` reports. The rows it closes stay locked by that
    transaction, so a second request at the same time sees them held and
    answers 409.

    The 409 names the oldest live report: in a multi-context batch several
    reports can share status='running', and the oldest is the most likely to
    be currently executing, the most useful target for the UI's "view running
    report" link. With no running report at all, two simultaneous requests
    can still both be admitted.
    """
    held = (await db.execute(_held_running_reports(SleepReport.user_id == user_id))).scalars().all()
    held_ids = {report.id for report in held}
    running = (
        (
            await db.execute(
                select(SleepReport)
                .where(SleepReport.user_id == user_id, SleepReport.status == "running")
                .order_by(SleepReport.started_at, SleepReport.id)
            )
        )
        .scalars()
        .all()
    )
    stale_before = utcnow() - SLEEP_RUN_STALE_AFTER
    for report in running:
        if (
            report.id not in held_ids
            or report.id in _LIVE_REPORT_IDS
            or report.started_at > stale_before
        ):
            raise MemoryCloudException(
                message="A sleep run is already in progress for this user.",
                status_code=409,
                error_code="SLEEP-002",
                running_report_id=str(report.id),
                started_at=to_utc_iso(report.started_at),
            )
    reporter = SleepReporter(db)
    for report in held:
        await reporter.fail_report(report, SLEEP_RUN_INTERRUPTED)
        logger.warning(
            "manual_sleep_report_interrupted",
            report_id=str(report.id),
            user_id=user_id,
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
    admin: AdminAccountUser,
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
        A running report that no process is running and that was started
        more than 15 minutes ago no longer counts: it is marked ``failed``
        as interrupted and the new run is admitted.
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
    await _raise_if_sleep_running(db, user_id)

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

    _start_sleep_batch(created, user_id)

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
    # #1450: distinct from ``already_restored`` — same 409, but the undo did
    # not happen rather than having happened already, so the error_code is what
    # tells an operator whether anything needs doing.
    "edge_changed": 409,
    "not_a_merge": 400,
    "not_merge_deleted": 409,
}


@router.post(
    "/actions/{action_id}/undo-merge",
    response_model=UndoMergeResponse,
    summary="Undo one dedup merge (restore the merged-away memory)",
    responses={
        400: {"description": "The action is not a dedup merge."},
        404: {"description": "Action not found or not owned by the caller."},
        409: {
            "description": (
                "Conflicts with current state. ``SLEEP-UNDO-already_restored``: "
                "the memory or shadow edge is already back in its post-undo "
                "state (no-op). ``SLEEP-UNDO-not_merge_deleted``: the memory was "
                "deleted by something other than this merge. "
                "``SLEEP-UNDO-edge_changed`` (#1450): a later writer changed or "
                "removed the shadow edge, so the pre-merge state could not be "
                "restored — unlike the other two, the undo did NOT happen."
            )
        },
        410: {
            "description": f"The merge loser was {TOMBSTONE_PURGER_CLAUSE} — no longer restorable."
        },
    },
)
async def undo_merge(
    action_id: Annotated[int, Path(le=MAX_BIGINT)],
    admin: AdminAccountUser,
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


# ============================================================================
# The same two actions for a target user (#2838)
# ============================================================================


class SleepRunForUserRequest(BaseModel):
    """Body for POST /admin/users/{user_id}/sleep/run.

    Attributes:
        context_id: The context to run Sleep in. Required: one call schedules
            one run.
    """

    context_id: UUID


@target_router.post(
    "/users/{user_id}/sleep/run",
    response_model=SleepRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Trigger Sleep Maintenance for a user's context",
    responses={
        404: {
            "description": (
                "``RES-001``: no such user. ``SLEEP-003``: the context is not "
                "eligible (deleted, ``sleep_mode='skip'``, or without a live "
                "memory of this user)."
            )
        },
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
async def trigger_sleep_run_for_user(
    request: SleepRunForUserRequest,
    admin: AdminUser,
    user_id: str = Path(pattern=NO_NUL_PATTERN),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Run Sleep Maintenance once for ``user_id`` in one context (#2838).

    For a system admin and, in ``service`` mode, an operator. The run is the
    one the scheduled sweep would start for ``(user_id, the context's
    workspace, context_id)``: the report's ``user_id`` is the target user, and
    the run itself applies the workspace's Sleep state, as for the sweep.

    Eligible: the context is live, its ``sleep_mode`` is not ``skip`` and the
    user has a live memory in it.

    Returns:
        202 with ``{"report_ids": ["<uuid>"]}``. The phases run in the
        background; read ``GET /admin/sleep-reports/{id}`` for progress.

    Raises:
        404 ``RES-001`` for an unknown user.
        409 ``SLEEP-002`` while the target user has a running report, with
        ``running_report_id`` and ``started_at``. Best-effort under
        concurrent requests, like the own-account trigger. A running report
        that no process is running and that was started more than 15 minutes
        ago no longer counts: it is marked ``failed`` as interrupted and the
        new run is admitted.
        404 ``SLEEP-003`` when the context is not eligible; one message for
        every cause.

    An ``audit_logs`` row (``sleep_run_trigger``) is written in the report's
    commit: the caller as actor, and the target user, workspace, context and
    report ids in its metadata. It records the request and stays when the run
    fails.
    """
    actor_id = admin["user_id"]
    actor_email = admin.get("email") or actor_id

    # (a) The target account. The caller is never looked up: an operator
    # principal has no users row.
    target = (
        await db.execute(select(User.user_id).where(User.user_id == user_id))
    ).scalar_one_or_none()
    if target is None:
        raise NotFoundException("User")

    # (b) A run of the TARGET user in progress.
    await _raise_if_sleep_running(db, user_id)

    # (c) Eligibility, in one statement: the live, non-skip context holding a
    # live memory of the target user in the context's own workspace.
    has_live_memory = (
        select(Memory.id)
        .where(
            Memory.context_id == Context.id,
            Memory.workspace_id == Context.workspace_id,
            Memory.user_id == user_id,
            Memory.deleted_at.is_(None),
        )
        .exists()
    )
    eligible = (
        await db.execute(
            select(Context.workspace_id, Context.id).where(
                Context.id == request.context_id,
                Context.deleted_at.is_(None),
                Context.sleep_mode != "skip",
                has_live_memory,
            )
        )
    ).first()
    if eligible is None:
        raise MemoryCloudException(
            message="This context is not eligible for a sleep run for this user.",
            status_code=404,
            error_code="SLEEP-003",
            context_id=str(request.context_id),
        )
    workspace_id, context_id = str(eligible[0]), str(eligible[1])

    # The report and its audit row become visible together, in one commit,
    # before the job (which opens its own session) is scheduled.
    report = await SleepReporter(db).create_report(user_id, workspace_id, context_id)
    db.add(
        AuditLog(
            user_email=actor_email,
            user_id=actor_id,
            action=SLEEP_RUN_TRIGGER_AUDIT_ACTION,
            resource=f"context:{context_id}",
            user_metadata={
                "target_user_id": user_id,
                "workspace_id": workspace_id,
                "context_id": context_id,
                "report_id": str(report.id),
            },
        )
    )
    await db.commit()

    _start_sleep_batch([(report.id, workspace_id, context_id)], user_id)

    logger.info(
        "target_sleep_run_scheduled",
        target_user_id=user_id,
        context_id=context_id,
        report_id=str(report.id),
        operator=is_operator_principal(admin),
    )

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content=SleepRunResponse(report_ids=[report.id]).model_dump(mode="json"),
    )


@target_router.post(
    "/sleep-reports/{report_id}/actions/{action_id}/undo-merge",
    response_model=UndoMergeResponse,
    summary="Undo one dedup merge of a sleep report",
    responses={
        400: {"description": "The action is not a dedup merge."},
        404: {"description": "No such action in this sleep report."},
        409: {
            "description": (
                "Conflicts with current state: ``SLEEP-UNDO-already_restored``, "
                "``SLEEP-UNDO-not_merge_deleted`` or ``SLEEP-UNDO-edge_changed``, "
                "as on ``POST /admin/sleep/actions/{action_id}/undo-merge``."
            )
        },
        410: {
            "description": f"The merge loser was {TOMBSTONE_PURGER_CLAUSE} — no longer restorable."
        },
    },
)
async def undo_merge_for_report(
    report_id: UUID,
    admin: AdminUser,
    action_id: int = Path(ge=1, le=MAX_BIGINT),
    db: AsyncSession = Depends(get_db),
) -> UndoMergeResponse:
    """Restore the loser of ONE dedup merge of a named sleep report (#2838).

    For a system admin and, in ``service`` mode, an operator. The action is
    found by ``action_id`` and ``report_id``, whoever the report belongs to;
    an unknown action, an action of another report and an unknown report
    answer the same 404 ``SLEEP-UNDO-action_not_found``. Every later check
    and answer is the one of the own-account undo.

    The undo is recorded as an ``undo_merge`` action on the report
    (``undone_by`` is the caller's id, or ``operator`` for an operator) and
    as an ``audit_logs`` row (``sleep_merge_undo``) with the caller as actor
    and the report, action and memory ids in its metadata. The answer holds
    ids only.
    """
    from services.sleep.undo import UndoMergeError, undo_merge_action

    actor_id = admin["user_id"]
    actor_email = admin.get("email") or actor_id

    try:
        summary = await undo_merge_action(
            db,
            action_id,
            acting_user_id=actor_id,
            target_report_id=report_id,
            actor_email=actor_email,
        )
    except UndoMergeError as exc:
        raise MemoryCloudException(
            message=exc.message,
            status_code=_UNDO_ERROR_STATUS.get(exc.code, 400),
            error_code=f"SLEEP-UNDO-{exc.code}",
        ) from exc

    return UndoMergeResponse(**summary)
