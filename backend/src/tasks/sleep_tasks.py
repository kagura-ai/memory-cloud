"""Sleep Maintenance background tasks.

Issue #101/#103: Scheduled sleep maintenance for all users/contexts.
"""

import os

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from db.base import get_db
from utils.logger import get_logger

logger = get_logger(__name__)


async def sleep_maintenance_task():
    """Run sleep maintenance for all active users/contexts.

    Iterates over distinct (user_id, workspace_id, context_id) tuples
    and runs the SleepOrchestrator for each.

    Only runs when ENABLE_NEURAL_MEMORY=true AND SLEEP_ENABLED=true.
    """
    logger.info("sleep_maintenance_task_started")

    if os.getenv("ENABLE_NEURAL_MEMORY", "false").lower() != "true":
        logger.info("sleep_maintenance_task_skipped", reason="neural_memory_disabled")
        return

    if os.getenv("SLEEP_ENABLED", "false").lower() != "true":
        logger.info("sleep_maintenance_task_skipped", reason="sleep_disabled")
        return

    try:
        async for db in get_db():
            from sqlalchemy import distinct, select

            from models.memory import Memory
            from neural.config import NeuralMemoryConfig
            from services.sleep.orchestrator import SleepOrchestrator

            # Load config once for all contexts (not per-context)
            NeuralMemoryConfig.invalidate_cache()
            config = await NeuralMemoryConfig.from_db(db)

            # Find distinct (user_id, workspace_id, context_id) combinations
            stmt = (
                select(
                    distinct(Memory.user_id),
                    Memory.workspace_id,
                    Memory.context_id,
                )
                .where(Memory.deleted_at.is_(None))
                .where(Memory.workspace_id.isnot(None))
                .where(Memory.context_id.isnot(None))
            )

            result = await db.execute(stmt)
            contexts = result.all()

            total_runs = 0
            total_errors = 0

            for row in contexts:
                user_id = row[0]
                workspace_id = str(row[1]) if row[1] else None
                context_id = str(row[2]) if row[2] else None

                if not workspace_id or not context_id:
                    continue

                try:
                    orchestrator = SleepOrchestrator(db)
                    await orchestrator.run(user_id, workspace_id, context_id, config=config)
                    total_runs += 1
                except Exception as e:
                    total_errors += 1
                    logger.error(
                        "sleep_maintenance_context_failed",
                        user_id=user_id,
                        context_id=context_id,
                        error=str(e),
                        exc_info=True,
                    )

            await db.commit()

            logger.info(
                "sleep_maintenance_task_completed",
                contexts=len(contexts),
                runs=total_runs,
                errors=total_errors,
            )
            return

    except Exception as e:
        logger.error("sleep_maintenance_task_failed", error=str(e), exc_info=True)


def schedule_sleep_tasks(scheduler: AsyncIOScheduler) -> None:
    """Schedule Sleep Maintenance background task.

    Only registers if SLEEP_ENABLED=true.

    Args:
        scheduler: APScheduler instance
    """
    if os.getenv("SLEEP_ENABLED", "false").lower() != "true":
        logger.info("sleep_tasks_not_scheduled", reason="sleep_disabled")
        return

    sleep_hour = int(os.getenv("SLEEP_CRON_HOUR", "2"))
    sleep_minute = int(os.getenv("SLEEP_CRON_MINUTE", "0"))

    scheduler.add_job(
        sleep_maintenance_task,
        trigger=CronTrigger(hour=sleep_hour, minute=sleep_minute),
        id="sleep_maintenance",
        name="Sleep Maintenance",
        replace_existing=True,
    )
    logger.info(
        "scheduled_sleep_maintenance_task",
        hour=sleep_hour,
        minute=sleep_minute,
    )
