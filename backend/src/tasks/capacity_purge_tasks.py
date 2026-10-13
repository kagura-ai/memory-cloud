"""Daily capacity-over purge sweep (#1969).

Runs ``services.capacity_purge.run_capacity_purge_sweep`` once a day at
04:00 UTC — after the soft-delete file GC (03:15, ``file_tasks``), so files a
purge soft-deletes get their full ``FILE_SOFT_DELETE_RETENTION`` before any
GC pass sees them, and the restore window (``FILE_RESTORE_WINDOW``) holds.
Stamping always runs; the countdown mails and the deletion are behind
``CAPACITY_PURGE_NOTICE_ENABLED`` / ``CAPACITY_PURGE_ENABLED``.
Single-flight across API processes (Postgres advisory lock); a contended
tick skips.
"""

from __future__ import annotations

from dataclasses import asdict

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from db.base import get_db
from services.capacity_purge import run_capacity_purge_sweep
from tasks.single_flight import single_flight
from utils.datetime import utcnow
from utils.logger import get_logger

logger = get_logger(__name__)

_LOCK_KEY = "capacity_purge_sweep"


async def capacity_purge_sweep() -> dict[str, int]:
    """The scheduled job. Returns flat counts for the log / tests."""
    async with single_flight(_LOCK_KEY) as acquired:
        if not acquired:
            logger.info("capacity_purge_sweep_skipped", reason="single_flight")
            return {"skipped": 1}
        counts: dict[str, int] = {}
        async for db in get_db():
            result = await run_capacity_purge_sweep(db, now=utcnow())
            counts.update(asdict(result.stamp))
            if result.notices is not None:
                counts.update({f"notice_{k}": v for k, v in asdict(result.notices).items()})
            if result.purge is not None:
                counts.update({f"purge_{k}": v for k, v in asdict(result.purge).items()})
        logger.info("capacity_purge_sweep_done", **counts)
        return counts


def schedule_capacity_purge_tasks(scheduler: AsyncIOScheduler) -> None:
    """Register the daily sweep (04:00 UTC, after the 03:15 file GC)."""
    scheduler.add_job(
        capacity_purge_sweep,
        trigger=CronTrigger(hour=4, minute=0, timezone="UTC"),
        id="capacity_purge_sweep",
        name="Capacity-over purge sweep (#1969)",
        replace_existing=True,
    )
    logger.info("scheduled_capacity_purge_tasks", cron_utc="04:00")
