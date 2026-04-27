"""Background sweep for account erasures whose cooling-off period has ended.

Issue #360. APScheduler runs in-memory, but the durable state lives in
``erasure_requests`` (status='cooling_off' rows with ``scheduled_for <= now``)
— so a process restart loses no work, the next sweep just picks up where
the previous one stopped. Hourly is sufficient given the 7-day cooling-off
period and the GDPR 1-month SLA.
"""

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from utils.logger import get_logger

logger = get_logger(__name__)


async def sweep_pending_erasures() -> None:
    """Find cooling_off requests whose scheduled_for has passed and execute.

    Each request runs in its own service-level transaction; failures are
    logged and recorded on the row (status='failed') without aborting the
    sweep — one bad row should not block the rest of the queue.
    """
    from db.base import get_db
    from services.account_erasure_service import AccountErasureService

    async for db in get_db():
        try:
            service = AccountErasureService(db)
            executed = await service.sweep_pending_erasures()
            if executed:
                logger.info("erasure_sweep_executed", count=executed)
        except Exception as exc:
            logger.error(
                "erasure_sweep_failed",
                error=str(exc),
                exc_info=True,
            )


def schedule_erasure_tasks(scheduler: AsyncIOScheduler) -> None:
    """Register hourly erasure sweep with the global scheduler."""
    scheduler.add_job(
        sweep_pending_erasures,
        trigger=IntervalTrigger(hours=1),
        id="sweep_pending_erasures",
        name="Sweep Pending Account Erasures",
        replace_existing=True,
    )
    logger.info("scheduled_erasure_sweep_task", interval_hours=1)
