"""BM25 IDF drift cron task.

Issue #343: scheduled job that walks every active context, computes the
IDF distribution drift via Bm25DriftOrchestrator, and persists one
bm25_idf_drift_log row per context per cycle.

Disabled by default (bm25_drift_cron_enabled=false).
Mirrors the sleep_enabled precedent at neural/config.py:168.
"""

import os

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from config.settings import get_settings
from db.base import get_db
from utils.logger import get_logger

logger = get_logger(__name__)


async def bm25_drift_maintenance_task() -> None:
    """Run BM25 IDF drift measurement for every active context.

    Iterates over distinct context_id rows from `memories` and calls
    Bm25DriftOrchestrator per context with per-context commit/rollback —
    a failing context does not poison the rest of the cycle.

    Only runs when settings.bm25_drift_cron_enabled is true (env var
    BM25_DRIFT_CRON_ENABLED feeds the same setting via pydantic-settings).
    The double-check inside the task body (mirroring sleep_tasks.py) catches
    the case where the scheduler somehow registered the job despite the
    registration-time gate.
    """
    logger.info("bm25_drift_task_started")

    settings = get_settings()
    if not settings.bm25_drift_cron_enabled:
        logger.info("bm25_drift_task_skipped", reason="bm25_drift_disabled")
        return

    try:
        async for db in get_db():
            from sqlalchemy import select

            from models.memory import Memory
            from services.bm25_drift.orchestrator import Bm25DriftOrchestrator

            stmt = (
                select(Memory.context_id)
                .distinct()
                .where(Memory.deleted_at.is_(None))
                .where(Memory.context_id.isnot(None))
            )
            result = await db.execute(stmt)
            context_ids = [row[0] for row in result.all() if row[0] is not None]

            total_runs = 0
            total_errors = 0

            orchestrator = Bm25DriftOrchestrator(db)
            for context_id in context_ids:
                try:
                    await orchestrator.run(context_id)
                    await db.commit()
                    total_runs += 1
                except Exception as e:
                    await db.rollback()
                    total_errors += 1
                    logger.error(
                        "bm25_drift_context_failed",
                        context_id=str(context_id),
                        error=str(e),
                        exc_info=True,
                    )

            logger.info(
                "bm25_drift_task_completed",
                contexts=len(context_ids),
                runs=total_runs,
                errors=total_errors,
            )
            return

    except Exception as e:
        logger.error("bm25_drift_task_failed", error=str(e), exc_info=True)


def schedule_bm25_drift_tasks(scheduler: AsyncIOScheduler) -> None:
    """Schedule the BM25 IDF drift maintenance cron.

    Only registers when BM25_DRIFT_CRON_ENABLED=true. The flag is read at
    registration time AND inside the task body (defense in depth — a
    misconfigured environment cannot accidentally start emitting drift
    rows in production).
    """
    settings = get_settings()
    if not settings.bm25_drift_cron_enabled:
        logger.info("bm25_drift_tasks_not_scheduled", reason="bm25_drift_disabled")
        return

    cron_hour = int(os.getenv("BM25_DRIFT_CRON_HOUR", "3"))
    cron_minute = int(os.getenv("BM25_DRIFT_CRON_MINUTE", "0"))

    scheduler.add_job(
        bm25_drift_maintenance_task,
        trigger=CronTrigger(hour=cron_hour, minute=cron_minute),
        id="bm25_drift_maintenance",
        name="BM25 IDF Drift Maintenance",
        replace_existing=True,
    )
    logger.info(
        "scheduled_bm25_drift_task",
        hour=cron_hour,
        minute=cron_minute,
    )
