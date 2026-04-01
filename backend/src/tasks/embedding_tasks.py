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

    - pending older than 10s: create_task likely failed or never fired
    - processing older than 60s: worker crashed mid-processing (stale)
    process_pending_embedding() handles both cases via its claim logic.
    """
    from sqlalchemy import and_, or_

    from db.base import get_db
    from models.memory import Memory
    from services.memory_service import process_pending_embedding
    from utils.datetime import utcnow

    async for db in get_db():
        try:
            now = utcnow()
            pending_cutoff = now - timedelta(seconds=10)
            stale_cutoff = now - timedelta(seconds=60)
            result = await db.execute(
                select(Memory.id)
                .where(
                    Memory.deleted_at.is_(None),
                    or_(
                        and_(
                            Memory.embedding_status == "pending",
                            Memory.created_at < pending_cutoff,
                        ),
                        and_(
                            Memory.embedding_status == "processing",
                            Memory.updated_at < stale_cutoff,
                        ),
                    ),
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
