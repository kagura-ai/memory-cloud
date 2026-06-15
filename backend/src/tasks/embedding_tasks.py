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
    """Find and process memories stuck in pending/processing/failed status.

    - pending older than 10s: create_task likely failed or never fired
    - processing older than 60s: worker crashed mid-processing (stale)
    - failed past the retry backoff with budget left (#979): a transient
      embedding/Qdrant blip self-heals instead of needing the manual admin
      retry endpoint, bounded by MAX_EMBEDDING_RETRIES so a poison row stops.
    process_pending_embedding() re-checks each gate atomically in its claim,
    so this SELECT is only a candidate prefilter (no double-increment race).
    """
    from sqlalchemy import and_, case, or_

    from db.base import get_db
    from models.memory import Memory
    from services.memory_service import (
        embedding_retry_eligible_clause,
        process_pending_embedding,
    )
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
                        # #979: shared with the claim gate so they cannot drift.
                        embedding_retry_eligible_clause(now),
                    ),
                )
                # #979: process genuinely-pending/processing rows before failed
                # retries so a backlog of failed rows can't starve the limit(20)
                # budget and delay brand-new memories' first embedding.
                .order_by(
                    case((Memory.embedding_status == "failed", 1), else_=0),
                    Memory.created_at,
                )
                .limit(20)
            )
            pending_ids = [row[0] for row in result.all()]

            if not pending_ids:
                return

            logger.info("sweep_pending_embeddings", count=len(pending_ids))

            # Per-id try/except so one stuck embedding does not abort
            # the sweep — the next sweep tick (30s later) would re-pick
            # only this id, but in the meantime the OTHER pending ids
            # would sit untouched. Caught via Copilot review on #496.
            failed = 0
            for mid in pending_ids:
                try:
                    await process_pending_embedding(mid)
                except Exception as e:  # noqa: BLE001 — we want to keep going
                    failed += 1
                    logger.error(
                        "sweep_pending_embeddings_item_failed",
                        memory_id=str(mid),
                        error=str(e),
                        exc_info=True,
                    )

            logger.info(
                "sweep_pending_embeddings_done",
                processed=len(pending_ids) - failed,
                failed=failed,
            )

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
