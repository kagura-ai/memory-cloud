"""Daily purge of expired workspace operation history (#2739).

Every entitled workspace keeps its history for 90 days
(``config.retention.OPERATION_HISTORY_RETENTION_DAYS``) plus its
``history_retention_days`` add-on. The read API already hides older rows; this
job deletes them. A row whose workspace row no longer exists uses the base.
A downgraded workspace's rows expire the same way, nothing else deletes them.

Runs once a day at 05:10 UTC (after the 04:xx jobs; nothing else is scheduled
in that hour), leader-only like every scheduled job and single-flight across
processes. Deletes in bounded batches, committing each, until nothing expired
is left or the time budget is spent; the next day continues. Each batch is
idempotent, so a rerun or an interrupted run is harmless.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import DateTime, delete, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from config.retention import (
    OPERATION_HISTORY_MAX_RETENTION_BONUS_DAYS,
    OPERATION_HISTORY_RETENTION_DAYS,
)
from db.base import get_db
from models.auth import Workspace
from models.operation_history import OperationHistory as Event
from tasks.single_flight import single_flight
from utils.logger import get_logger

logger = get_logger(__name__)

PURGE_BATCH_SIZE = 5_000
# Wall-clock budget of one run; the rest waits for the next day.
PURGE_TIME_BUDGET_SECONDS = 600.0
PURGE_HOUR_UTC = 5
PURGE_MINUTE_UTC = 10
_LOCK_KEY = "operation_history_purge"


def _expired_batch(now: datetime, batch_size: int):
    """Ids of at most ``batch_size`` rows older than their workspace's retention.

    Mirrors ``config.retention.operation_history_retention_days`` in SQL: the
    base plus the add-on bonus, clamped to ``[0, max]``; no workspace row means
    no bonus. The first predicate (older than the base) holds for every expired
    row and lets the scan skip recent rows without the join.
    """
    at = literal(now, DateTime(timezone=True))
    bonus = func.least(
        func.greatest(func.coalesce(Workspace.addon_history_retention_days_bonus, 0), 0),
        OPERATION_HISTORY_MAX_RETENTION_BONUS_DAYS,
    )
    return (
        select(Event.id)
        .select_from(Event.__table__.outerjoin(Workspace, Workspace.id == Event.workspace_id))
        .where(
            Event.occurred_at < at - func.make_interval(0, 0, 0, OPERATION_HISTORY_RETENTION_DAYS),
            Event.occurred_at
            < at - func.make_interval(0, 0, 0, OPERATION_HISTORY_RETENTION_DAYS + bonus),
        )
        .limit(batch_size)
    )


async def purge_operation_history(
    db: AsyncSession,
    now: datetime | None = None,
    *,
    batch_size: int = PURGE_BATCH_SIZE,
    time_budget_seconds: float = PURGE_TIME_BUDGET_SECONDS,
) -> dict[str, int]:
    """Delete expired history rows in batches, committing after each.

    Args:
        db: Async session.
        now: Current time (aware UTC; defaults to now). Tests pin it.
        batch_size: Rows per DELETE statement.
        time_budget_seconds: Stop starting new batches after this long.

    Returns:
        ``deleted`` rows, ``batches`` that deleted something, and
        ``budget_exhausted`` (1 when the run stopped at its budget).
    """
    now = now or datetime.now(UTC)
    started = time.monotonic()
    deleted = batches = budget_exhausted = 0
    while True:
        result = await db.execute(
            delete(Event)
            .where(Event.id.in_(_expired_batch(now, batch_size).scalar_subquery()))
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        count = int(getattr(result, "rowcount", 0) or 0)
        if count == 0:
            break
        deleted += count
        batches += 1
        if count < batch_size:
            break
        if time.monotonic() - started >= time_budget_seconds:
            budget_exhausted = 1
            break
    counts = {"deleted": deleted, "batches": batches, "budget_exhausted": budget_exhausted}
    logger.info("operation_history_purged", **counts)
    return counts


async def purge_operation_history_task() -> None:
    """APScheduler entry point: owns its session, one process at a time."""
    async with single_flight(_LOCK_KEY) as acquired:
        if not acquired:
            logger.info("operation_history_purge_skipped", reason="single_flight")
            return
        async for db in get_db():
            try:
                await purge_operation_history(db)
            except Exception:
                logger.exception("operation_history_purge_failed")
                await db.rollback()
            return


def schedule_operation_history_tasks(scheduler: AsyncIOScheduler) -> None:
    """Register the daily expired-history purge (05:10 UTC).

    Args:
        scheduler: The shared APScheduler instance.
    """
    scheduler.add_job(
        purge_operation_history_task,
        trigger=CronTrigger(hour=PURGE_HOUR_UTC, minute=PURGE_MINUTE_UTC, timezone="UTC"),
        id="purge_operation_history",
        name="Purge expired operation history (#2739)",
        replace_existing=True,
    )
    logger.info("scheduled_operation_history_purge", cron_utc="05:10")
