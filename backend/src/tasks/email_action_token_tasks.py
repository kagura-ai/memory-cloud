"""Hourly cleanup of used and expired email action tokens (Issue #1738).

A row in ``email_action_tokens`` is written for every emailed link (password
reset, password set-up, email verification). Once it is used (consumed, or
invalidated by a later link or password change) or has expired it can never
work again, so this job deletes rows whose ``used_at`` or ``expires_at`` is
older than ``email_action_token_retention_seconds``. The audit trail lives in
``audit_logs``, not here.

The DELETE is idempotent, so a run from more than one API process at once is
harmless.
"""

from datetime import datetime, timedelta
from typing import Any, cast

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import Delete, delete, or_
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import get_settings
from db.base import get_db
from models.auth import EmailActionToken
from utils.datetime import utcnow
from utils.logger import get_logger

logger = get_logger(__name__)


def spent_email_action_tokens_delete(cutoff: datetime) -> Delete:
    """Build the DELETE for tokens used or expired before ``cutoff``.

    Args:
        cutoff: Naive UTC instant; rows used or expired strictly before it go.

    Returns:
        The DELETE statement.
    """
    return delete(EmailActionToken).where(
        or_(EmailActionToken.used_at < cutoff, EmailActionToken.expires_at < cutoff)
    )


async def cleanup_email_action_tokens(db: AsyncSession, now: datetime | None = None) -> int:
    """Delete email action tokens past the retention window.

    Caller owns commit/rollback.

    Args:
        db: Async session.
        now: Current naive UTC time (defaults to ``utcnow()``; tests pin it).

    Returns:
        Number of rows deleted.
    """
    retention = timedelta(seconds=get_settings().email_action_token_retention_seconds)
    cutoff = (now or utcnow()) - retention
    result = await db.execute(spent_email_action_tokens_delete(cutoff))
    return int(cast(CursorResult[Any], result).rowcount or 0)


async def cleanup_email_action_tokens_task() -> None:
    """APScheduler entry point — owns its own session via ``get_db()``."""
    async for db in get_db():
        try:
            deleted = await cleanup_email_action_tokens(db)
            await db.commit()
            logger.info("email_action_token_cleanup_completed", deleted=deleted)
        except Exception:
            logger.exception("email_action_token_cleanup_failed")
            await db.rollback()
        return


def schedule_email_action_token_tasks(scheduler: AsyncIOScheduler) -> None:
    """Register the hourly used/expired email-action-token cleanup job.

    Args:
        scheduler: The shared APScheduler instance.
    """
    scheduler.add_job(
        cleanup_email_action_tokens_task,
        trigger=CronTrigger(minute=35),  # hourly, offset from the device-code cleanup at :20
        id="cleanup_email_action_tokens",
        name="Cleanup used and expired email action tokens (#1738)",
        replace_existing=True,
    )
    logger.info("scheduled_email_action_token_cleanup", interval_hours=1)
