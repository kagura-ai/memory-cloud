"""Credentials-related background tasks.

Migration 034: Auto-hide expired credentials (7-day visibility window).
"""

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from background.auto_hide_credentials import auto_hide_expired_credentials

logger = logging.getLogger(__name__)


def schedule_credentials_tasks(scheduler: AsyncIOScheduler) -> None:
    """Schedule credentials-related background tasks.

    Migration 034: Auto-hide expired credentials every hour.

    Args:
        scheduler: APScheduler instance
    """
    # Auto-hide expired credentials (hourly)
    scheduler.add_job(
        auto_hide_expired_credentials,
        trigger="cron",
        hour="*",  # Every hour
        minute=5,  # At 5 minutes past the hour
        id="auto_hide_credentials",
        name="Auto-hide expired credentials",
        replace_existing=True,
    )
    logger.info("scheduled_credentials_tasks: auto_hide_credentials (hourly)")
