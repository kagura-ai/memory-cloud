"""Background task: sweep pending embeddings.

Issue #76: Crash recovery for memories whose embedding processing was
interrupted (server restart, error, or create_task failure).
"""

from datetime import timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from utils.logger import get_logger

logger = get_logger(__name__)


async def sweep_pending_embeddings() -> None:
    """Find and process memories stuck in pending/processing status.

    Only processes memories older than 10 seconds to avoid racing with
    the fire-and-forget create_task from remember().
    """
    from db.base import get_db
    from models.memory import Memory
    from services.memory_service import process_pending_embedding
    from utils.datetime import utcnow

    async for db in get_db():
        try:
            cutoff = utcnow() - timedelta(seconds=10)
            result = await db.execute(
                select(Memory.id)
                .where(
                    Memory.embedding_status.in_(["pending", "processing"]),
                    Memory.created_at < cutoff,
                    Memory.deleted_at.is_(None),
                )
                .limit(20)
            )
            pending_ids = [row[0] for row in result.all()]

            if not pending_ids:
                return

            logger.info("sweep_pending_embeddings", count=len(pending_ids))

            for mid in pending_ids:
                await process_pending_embedding(mid)

            logger.info("sweep_pending_embeddings_done", processed=len(pending_ids))

        except Exception as e:
            logger.error("sweep_pending_embeddings_failed", error=str(e), exc_info=True)


def schedule_embedding_tasks(scheduler: AsyncIOScheduler) -> None:
    """Register embedding sweep task."""
    scheduler.add_job(
        sweep_pending_embeddings,
        trigger=IntervalTrigger(seconds=30),
        id="sweep_pending_embeddings",
        name="Sweep Pending Embeddings",
        replace_existing=True,
    )
    logger.info("scheduled_embedding_sweep_task", interval_seconds=30)
