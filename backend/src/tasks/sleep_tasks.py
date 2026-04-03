"""Sleep Maintenance background tasks.

Issue #101/#103: Scheduled sleep maintenance for all users/contexts.
"""

import logging
import os

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from db.base import get_db

logger = logging.getLogger(__name__)


async def sleep_maintenance_task():
    """Run sleep maintenance for all active users/contexts.

    Iterates over all users with graph data, then for each user
    finds distinct (workspace_id, context_id) pairs and runs
    the SleepOrchestrator for each.

    Only runs when ENABLE_NEURAL_MEMORY=true AND SLEEP_ENABLED=true.
    """
    logger.info("sleep_maintenance_task_started")

    if os.getenv("ENABLE_NEURAL_MEMORY", "false").lower() != "true":
        logger.info("sleep_maintenance_task_skipped: neural_memory_disabled")
        return

    if os.getenv("SLEEP_ENABLED", "false").lower() != "true":
        logger.info("sleep_maintenance_task_skipped: sleep_disabled")
        return

    try:
        async for db in get_db():
            from sqlalchemy import distinct, select

            from models.memory import Memory
            from services.sleep.orchestrator import SleepOrchestrator

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
                    await orchestrator.run(user_id, workspace_id, context_id)
                    total_runs += 1
                except Exception as e:
                    total_errors += 1
                    logger.error(
                        f"sleep_maintenance_context_failed: "
                        f"user={user_id}, context={context_id}, error={e}",
                        exc_info=True,
                    )

            await db.commit()

            logger.info(
                f"sleep_maintenance_task_completed: "
                f"contexts={len(contexts)}, runs={total_runs}, errors={total_errors}"
            )
            return

    except Exception as e:
        logger.error(f"sleep_maintenance_task_failed: {e}", exc_info=True)


def schedule_sleep_tasks(scheduler: AsyncIOScheduler) -> None:
    """Schedule Sleep Maintenance background task.

    Only registers if SLEEP_ENABLED=true.

    Args:
        scheduler: APScheduler instance
    """
    if os.getenv("SLEEP_ENABLED", "false").lower() != "true":
        logger.info("sleep_tasks_not_scheduled: sleep_disabled")
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
    logger.info(f"scheduled_sleep_maintenance_task: hour={sleep_hour}, minute={sleep_minute}")
