"""APScheduler setup for background tasks."""

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

logger = logging.getLogger(__name__)

# Global scheduler instance
_scheduler: AsyncIOScheduler | None = None


def get_scheduler() -> AsyncIOScheduler:
    """Get or create scheduler instance.

    Returns:
        AsyncIOScheduler instance
    """
    global _scheduler

    if _scheduler is None:
        _scheduler = AsyncIOScheduler(
            timezone="UTC",
            job_defaults={
                "coalesce": True,  # Combine multiple missed runs
                "max_instances": 1,  # Prevent concurrent runs
                "misfire_grace_time": 300,  # 5 minutes grace period
            },
        )
        logger.info("APScheduler created")

    return _scheduler


def start_scheduler() -> None:
    """Start the scheduler.

    Call this during FastAPI startup.
    """
    scheduler = get_scheduler()

    if not scheduler.running:
        scheduler.start()
        logger.info("APScheduler started")
    else:
        logger.warning("APScheduler already running")


def shutdown_scheduler() -> None:
    """Shutdown the scheduler.

    Call this during FastAPI shutdown.
    """
    global _scheduler

    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("APScheduler shutdown")
        _scheduler = None
