"""``scheduled_job_status``: registration, run writes and the overdue rule (#2768).

The scheduler leader's run recorder (:mod:`tasks.job_runs`) calls the three
writers below from its single consumer task; any API process reads the rows.

Writers:

- :func:`record_registrations` — one ``INSERT ... ON CONFLICT`` for the whole
  registration set, rows sorted by ``job_id`` (two leaders' upserts lock rows
  in one order). ``last_registered_at`` is the database ``now()`` of that one
  statement, so every job of a registration shares one generation value
  whatever the leader's clock says. ``registered_since`` restarts when a job
  was not part of the previous generation (a re-enabled job).
- :func:`record_job_started` — ``last_started_at`` of a submitted run
  (``GREATEST`` guard).
- :func:`record_job_run` — the outcome of one or more finished runs (see
  :class:`JobRunUpdate`). An ``UPDATE``: the row is created only by the
  registration upsert, and a run write never touches the registration
  columns. Guarded by ``last_outcome_at`` against an older write landing late
  (the handover overlap of two leaders).

Each writer runs one statement in its own short session with a local
``lock_timeout`` / ``statement_timeout``, commits, and RAISES on failure: the
recorder is the never-raise boundary (it logs the class name only).

Reader helpers :func:`is_overdue` / :func:`is_running` are pure.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Protocol

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import Update, case, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from models.operations_status import (
    JOB_ID_MAX_LENGTH,
    JOB_NAME_MAX_LENGTH,
    JobOutcome,
    JobTriggerKind,
    ScheduledJobStatus,
)

# A job id the recorder may write: code-defined snake_case. An id built from
# data (``f"..._{workspace_id}"``) never reaches the table.
JOB_ID_RE = re.compile(rf"^[a-z][a-z0-9_]{{0,{JOB_ID_MAX_LENGTH - 1}}}$")

# How many cron fires are inspected to find the longest gap (4 gaps).
_CRON_FIRES_INSPECTED = 5

# Bounds of one write; the recorder also caps the whole call with wait_for.
_LOCK_TIMEOUT_MS = 2000
_STATEMENT_TIMEOUT_MS = 5000
_LOCAL_TIMEOUTS_SQL = text(
    "SELECT set_config('lock_timeout', :lock_ms, true),"
    " set_config('statement_timeout', :statement_ms, true)"
)

SessionFactory = Callable[[], AsyncSession]

# Tests replace this; ``None`` means the application's session factory.
_session_factory: SessionFactory | None = None


@dataclass(frozen=True)
class JobRegistration:
    """One registered job as the leader's scheduler holds it.

    Attributes:
        job_id: APScheduler job id (matches :data:`JOB_ID_RE`).
        name: The job's display name, clipped to the column width.
        trigger_kind: ``interval``, ``cron`` or ``other``.
        expected_interval_seconds: Longest gap between two fires, or ``None``.
    """

    job_id: str
    name: str
    trigger_kind: JobTriggerKind
    expected_interval_seconds: int | None


@dataclass(frozen=True)
class JobRunUpdate:
    """What one run (or several coalesced runs) of a job changes on its row.

    A ``missed`` run only moves ``outcome`` / ``outcome_at``; the finish fields
    describe the latest run that completed or failed and are ``None`` when the
    update holds only missed runs. Two updates of one job merge with
    :meth:`then`, so a queue that coalesces per job keeps the failure count.

    Attributes:
        job_id: The job.
        outcome: Outcome of the latest run.
        outcome_at: When it was observed.
        started_at: Submission time of the latest finished run, if known.
        finished_at: When the latest completed or failed run ended.
        duration_ms: Its duration, if its start was seen.
        error_class: Its exception class name (failed runs only).
        counts: Its sanitised counts.
        success_at: The latest run that counts as a success (``ok`` with real
            work; a run whose only count is ``skipped`` does not).
        reset_failures: A success happened: ``consecutive_failures`` restarts
            from ``failures_added``.
        failures_added: Failed runs after the last success in this update.
    """

    job_id: str
    outcome: JobOutcome
    outcome_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = None
    error_class: str | None = None
    counts: Mapping[str, int] | None = None
    success_at: datetime | None = None
    reset_failures: bool = False
    failures_added: int = 0

    def then(self, later: JobRunUpdate) -> JobRunUpdate:
        """This update followed by ``later`` (same job), as one update."""
        finish = later if later.finished_at is not None else self
        if later.reset_failures:
            reset, added = True, later.failures_added
        else:
            reset, added = self.reset_failures, self.failures_added + later.failures_added
        return replace(
            later,
            started_at=finish.started_at,
            finished_at=finish.finished_at,
            duration_ms=finish.duration_ms,
            error_class=finish.error_class,
            counts=finish.counts,
            success_at=later.success_at or self.success_at,
            reset_failures=reset,
            failures_added=added,
        )


class JobStatusRow(Protocol):
    """The columns :func:`is_overdue` / :func:`is_running` read."""

    trigger_kind: str
    expected_interval_seconds: int | None
    last_registered_at: datetime
    registered_since: datetime
    last_started_at: datetime | None
    last_outcome_at: datetime | None


def trigger_kind(trigger: Any) -> JobTriggerKind:
    """``interval`` / ``cron`` / ``other`` for an APScheduler trigger."""
    if isinstance(trigger, IntervalTrigger):
        return "interval"
    if isinstance(trigger, CronTrigger):
        return "cron"
    return "other"


def expected_interval_seconds(trigger: Any, now: datetime) -> int | None:
    """The longest gap between two consecutive fires of ``trigger``.

    - ``IntervalTrigger``: its interval.
    - ``CronTrigger``: the largest gap among its next fires after ``now`` (a
      monthly cron gives about 31 days).
    - Anything else (a date trigger, an unknown one): ``None``, never overdue.

    Args:
        trigger: The job's trigger.
        now: Timezone-aware current time.
    """
    if isinstance(trigger, IntervalTrigger):
        return max(1, int(trigger.interval_length))
    if not isinstance(trigger, CronTrigger):
        return None
    fires: list[datetime] = []
    previous: datetime | None = None
    cursor = now
    for _ in range(_CRON_FIRES_INSPECTED):
        nxt = trigger.get_next_fire_time(previous, cursor)
        if nxt is None:
            break
        fires.append(nxt)
        previous = nxt
        cursor = nxt + timedelta(microseconds=1)
    gaps = [int((b - a).total_seconds()) for a, b in zip(fires, fires[1:], strict=False)]
    longest = max(gaps, default=0)
    return longest if longest > 0 else None


def job_registrations(scheduler: Any, now: datetime) -> list[JobRegistration]:
    """Every job ``scheduler`` holds, sorted by id (pending jobs too, before start).

    Ids that do not match :data:`JOB_ID_RE` are left out: the table only
    holds code-defined ids.
    """
    by_id: dict[str, JobRegistration] = {}
    for job in scheduler.get_jobs():
        job_id = job.id
        if not isinstance(job_id, str) or not JOB_ID_RE.match(job_id):
            continue
        name = str(job.name or job_id)[:JOB_NAME_MAX_LENGTH]
        by_id[job_id] = JobRegistration(
            job_id=job_id,
            name=name,
            trigger_kind=trigger_kind(job.trigger),
            expected_interval_seconds=expected_interval_seconds(job.trigger, now),
        )
    return [by_id[k] for k in sorted(by_id)]


def is_running(row: JobStatusRow) -> bool:
    """A run was submitted after the latest outcome was recorded."""
    if row.last_started_at is None:
        return False
    return row.last_outcome_at is None or row.last_started_at > row.last_outcome_at


def is_overdue(
    row: JobStatusRow,
    *,
    now: datetime,
    grace_seconds: int,
    latest_registration: datetime | None,
) -> bool:
    """Whether a registered job has not started within its interval plus the grace.

    - Only a job of the latest registration generation can be overdue (a job
      the current configuration no longer registers keeps its row but is not
      ``registered``); a job without an expected interval never is.
    - The base is the latest start, or the start of the job's current
      registration when that is later (a job re-enabled after an absence is
      not overdue at once).
    - Interval triggers restart their period at every registration (the first
      fire is registration + interval), so their base is never earlier than
      the current registration: a deploy does not flag them.
    - A cron run skipped by downtime is flagged, and a run that hangs is
      flagged at its start + interval + grace. A failing job that keeps
      running is not overdue (its ``consecutive_failures`` shows it).

    Args:
        row: The job's row.
        now: Timezone-aware current time.
        grace_seconds: Slack added to the interval.
        latest_registration: ``max(last_registered_at)`` over all rows.
    """
    if latest_registration is None or row.last_registered_at != latest_registration:
        return False
    if row.expected_interval_seconds is None:
        return False
    base = row.registered_since
    if row.last_started_at is not None and row.last_started_at > base:
        base = row.last_started_at
    if row.trigger_kind == "interval" and row.last_registered_at > base:
        base = row.last_registered_at
    return now > base + timedelta(seconds=row.expected_interval_seconds + grace_seconds)


def _new_session() -> AsyncSession:
    if _session_factory is not None:
        return _session_factory()
    from db.base import _get_session_factory

    return _get_session_factory()()


async def _execute_one(stmt: Any) -> None:
    """Run ``stmt`` in its own short session with local timeouts, and commit."""
    session = _new_session()
    try:
        await session.execute(
            _LOCAL_TIMEOUTS_SQL,
            {"lock_ms": str(_LOCK_TIMEOUT_MS), "statement_ms": str(_STATEMENT_TIMEOUT_MS)},
        )
        await session.execute(stmt)
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    finally:
        await session.close()


async def record_registrations(regs: Sequence[JobRegistration]) -> None:
    """Upsert the registration set in one statement (one ``now()`` for all).

    Raises:
        Exception: Whatever the database raised.
    """
    if not regs:
        return
    table = ScheduledJobStatus.__table__
    ordered = sorted(regs, key=lambda r: r.job_id)
    stmt = pg_insert(table).values(
        [
            {
                "job_id": r.job_id,
                "job_name": r.name,
                "trigger_kind": r.trigger_kind,
                "expected_interval_seconds": r.expected_interval_seconds,
                "last_registered_at": func.now(),
                "registered_since": func.now(),
            }
            for r in ordered
        ]
    )
    # The previous generation, as the statement's snapshot sees it (rows this
    # statement writes are not visible to its own subquery).
    previous_generation = select(func.max(table.c.last_registered_at)).scalar_subquery()
    stmt = stmt.on_conflict_do_update(
        index_elements=[table.c.job_id],
        set_={
            "job_name": stmt.excluded.job_name,
            "trigger_kind": stmt.excluded.trigger_kind,
            "expected_interval_seconds": stmt.excluded.expected_interval_seconds,
            "last_registered_at": stmt.excluded.last_registered_at,
            "registered_since": case(
                (
                    table.c.last_registered_at == previous_generation,
                    table.c.registered_since,
                ),
                else_=stmt.excluded.registered_since,
            ),
            "updated_at": func.now(),
        },
    )
    await _execute_one(stmt)


def job_started_update(job_id: str, at: datetime) -> Update:
    """The ``last_started_at`` write of a submitted run."""
    table = ScheduledJobStatus.__table__
    return (
        update(table)
        .where(table.c.job_id == job_id)
        .values(
            last_started_at=func.greatest(table.c.last_started_at, at),
            updated_at=func.now(),
        )
    )


async def record_job_started(job_id: str, at: datetime) -> None:
    """Record that a run of ``job_id`` was submitted at ``at``.

    Raises:
        Exception: Whatever the database raised.
    """
    await _execute_one(job_started_update(job_id, at))


def job_run_update(run: JobRunUpdate) -> Update:
    """The ``UPDATE`` that applies ``run`` to its job's row."""
    table = ScheduledJobStatus.__table__
    values: dict[str, Any] = {
        "last_outcome": run.outcome,
        "last_outcome_at": run.outcome_at,
        "updated_at": func.now(),
    }
    if run.started_at is not None:
        values["last_started_at"] = func.greatest(table.c.last_started_at, run.started_at)
    if run.finished_at is not None:
        values["last_finished_at"] = run.finished_at
        values["last_duration_ms"] = run.duration_ms
        values["last_error_class"] = run.error_class
        values["last_counts"] = dict(run.counts) if run.counts is not None else None
    if run.success_at is not None:
        values["last_success_at"] = func.greatest(table.c.last_success_at, run.success_at)
    if run.reset_failures:
        values["consecutive_failures"] = run.failures_added
    elif run.failures_added:
        values["consecutive_failures"] = table.c.consecutive_failures + run.failures_added
    return (
        update(table)
        .where(
            table.c.job_id == run.job_id,
            or_(
                table.c.last_outcome_at.is_(None),
                table.c.last_outcome_at <= run.outcome_at,
            ),
        )
        .values(**values)
    )


async def record_job_run(run: JobRunUpdate) -> None:
    """Apply ``run`` to its job's row (no row, or a newer outcome: no change).

    Raises:
        Exception: Whatever the database raised.
    """
    await _execute_one(job_run_update(run))


__all__ = [
    "JOB_ID_RE",
    "JobRegistration",
    "JobRunUpdate",
    "JobStatusRow",
    "expected_interval_seconds",
    "is_overdue",
    "is_running",
    "job_registrations",
    "job_run_update",
    "job_started_update",
    "record_job_run",
    "record_job_started",
    "record_registrations",
    "trigger_kind",
]
