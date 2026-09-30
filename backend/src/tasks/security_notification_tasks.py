"""Every-minute digest of coalesced security notices (Issue #1752).

``services.security_notification_service`` emails the first occurrence of a
security event at once and buffers repeats for a window in Redis. This job
closes the windows that have ended and sends one digest per window. Each
window is claimed with ``ZREM``, so a run from more than one API process at
once sends each digest once.
"""

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from services.security_notification_service import flush_due_security_notifications
from utils.logger import get_logger

logger = get_logger(__name__)


async def flush_security_notifications_task() -> None:
    """APScheduler entry point; the flush opens its own sessions and never raises."""
    sent = await flush_due_security_notifications()
    if sent:
        logger.info("security_notification_digests_sent", sent=sent)


def schedule_security_notification_tasks(scheduler: AsyncIOScheduler) -> None:
    """Register the every-minute security notice digest job.

    Args:
        scheduler: The shared APScheduler instance.
    """
    scheduler.add_job(
        flush_security_notifications_task,
        trigger=CronTrigger(second=30),  # every minute, at :30
        id="flush_security_notifications",
        name="Send coalesced security notice digests (#1752)",
        replace_existing=True,
    )
    logger.info("scheduled_security_notification_flush", interval_minutes=1)
