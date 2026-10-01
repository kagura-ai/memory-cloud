"""Daily cleanup of known devices not seen for a while (Issue #1769).

``user_known_devices`` grows by one row per browser an account signs in from.
A browser not seen for ``known_device_retention_days`` is forgotten: its next
sign-in is treated as a new device and emails the owner. The DELETE is a range
scan on the ``last_seen`` index and is idempotent, so a run from more than one
API process at once is harmless.
"""

from datetime import datetime, timedelta
from typing import Any, cast

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import get_settings
from db.base import get_db
from services.known_device_service import stale_devices_delete
from utils.datetime import utcnow
from utils.logger import get_logger

logger = get_logger(__name__)


async def cleanup_stale_known_devices(db: AsyncSession, now: datetime | None = None) -> int:
    """Delete known devices whose ``last_seen`` is past the retention window.

    Caller owns commit/rollback.

    Args:
        db: Async session.
        now: Current naive UTC time (defaults to ``utcnow()``; tests pin it).

    Returns:
        Number of rows deleted.
    """
    retention = timedelta(days=get_settings().known_device_retention_days)
    cutoff = (now or utcnow()) - retention
    result = await db.execute(stale_devices_delete(cutoff))
    return int(cast(CursorResult[Any], result).rowcount or 0)


async def cleanup_stale_known_devices_task() -> None:
    """APScheduler entry point — owns its own session via ``get_db()``."""
    async for db in get_db():
        try:
            deleted = await cleanup_stale_known_devices(db)
            await db.commit()
            logger.info("known_device_cleanup_completed", deleted=deleted)
        except Exception:
            logger.exception("known_device_cleanup_failed")
            await db.rollback()
        return


def schedule_known_device_tasks(scheduler: AsyncIOScheduler) -> None:
    """Register the daily stale-known-device cleanup job.

    Args:
        scheduler: The shared APScheduler instance.
    """
    scheduler.add_job(
        cleanup_stale_known_devices_task,
        trigger=CronTrigger(hour=4, minute=40),  # daily, off the hourly jobs' minutes
        id="cleanup_stale_known_devices",
        name="Cleanup stale known devices (#1769)",
        replace_existing=True,
    )
    logger.info("scheduled_known_device_cleanup", cron="04:40 UTC")
