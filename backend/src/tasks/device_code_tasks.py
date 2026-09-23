"""Hourly cleanup of expired device codes (Issue #1656).

A row in ``oauth_device_codes`` is written by every
``POST /api/v1/oauth/device/authorize``. Once it has expired it can no longer
be verified, confirmed or exchanged for a token, so this job deletes rows whose
``expires_at`` is older than ``oauth_device_code_retention_seconds``.

The DELETE is a range scan on the ``expires_at`` index and is idempotent, so a
run from more than one API process at once is harmless.
"""

from datetime import datetime, timedelta
from typing import Any, cast

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import Delete, delete
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import get_settings
from db.base import get_db
from models.auth import OAuth2DeviceCode
from utils.datetime import utcnow
from utils.logger import get_logger

logger = get_logger(__name__)


def expired_device_codes_delete(cutoff: datetime) -> Delete:
    """Build the DELETE for device codes that expired before ``cutoff``.

    Args:
        cutoff: Naive UTC instant; rows with ``expires_at`` strictly before it go.

    Returns:
        The DELETE statement.
    """
    return delete(OAuth2DeviceCode).where(OAuth2DeviceCode.expires_at < cutoff)


async def cleanup_expired_device_codes(db: AsyncSession, now: datetime | None = None) -> int:
    """Delete device codes past the retention window.

    Caller owns commit/rollback.

    Args:
        db: Async session.
        now: Current naive UTC time (defaults to ``utcnow()``; tests pin it).

    Returns:
        Number of rows deleted.
    """
    retention = timedelta(seconds=get_settings().oauth_device_code_retention_seconds)
    cutoff = (now or utcnow()) - retention
    result = await db.execute(expired_device_codes_delete(cutoff))
    return int(cast(CursorResult[Any], result).rowcount or 0)


async def cleanup_expired_device_codes_task() -> None:
    """APScheduler entry point — owns its own session via ``get_db()``."""
    async for db in get_db():
        try:
            deleted = await cleanup_expired_device_codes(db)
            await db.commit()
            logger.info("device_code_cleanup_completed", deleted=deleted)
        except Exception:
            logger.exception("device_code_cleanup_failed")
            await db.rollback()
        return


def schedule_device_code_tasks(scheduler: AsyncIOScheduler) -> None:
    """Register the hourly expired-device-code cleanup job.

    Args:
        scheduler: The shared APScheduler instance.
    """
    scheduler.add_job(
        cleanup_expired_device_codes_task,
        trigger=CronTrigger(minute=20),  # hourly, offset from auto_hide_credentials at :05
        id="cleanup_expired_device_codes",
        name="Cleanup expired device codes (#1656)",
        replace_existing=True,
    )
    logger.info("scheduled_device_code_cleanup", interval_hours=1)
