"""Monthly hygiene for origin='semantic' edges (Issue #722).

Semantic edges are exempt from the Hebbian decay/prune loop because they
represent a static property (cosine similarity) rather than a usage trace.
This task runs monthly to delete edges whose src or dst memory has been
soft-deleted.
"""

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.ext.asyncio import AsyncSession

from db.base import get_db
from repositories.neural_edge import NeuralEdgeRepository
from utils.logger import get_logger

logger = get_logger(__name__)


async def semantic_edge_reverify_run(db: AsyncSession) -> dict[str, int]:
    """Run one pass of semantic-edge re-verification.

    Caller is responsible for committing or rolling back the session.
    The scheduled entrypoint commits; tests can verify without committing
    so the db_session fixture's rollback isolation is preserved.

    Args:
        db: SQLAlchemy async session (caller owns commit/rollback).

    Returns:
        Dict with ``semantic_edges_deleted`` count.
    """
    repo = NeuralEdgeRepository(db)
    deleted = await repo.delete_semantic_edges_for_dead_pairs()
    return {"semantic_edges_deleted": deleted}


async def _scheduled_entrypoint() -> None:
    """APScheduler entry point — owns its own session via get_db()."""
    try:
        async for db in get_db():
            try:
                result = await semantic_edge_reverify_run(db)
                await db.commit()
                logger.info(
                    "semantic_edge_reverify_completed",
                    semantic_edges_deleted=result["semantic_edges_deleted"],
                )
                return  # exiting the async-for here sends GeneratorExit into get_db at the yield,
                # so get_db's post-yield commit() never runs — our explicit commit above is
                # the only one that executes.
            except Exception:
                logger.exception("semantic_edge_reverify_failed")
                await db.rollback()
                return
    except Exception:
        logger.exception("semantic_edge_reverify_session_error")


def schedule_semantic_edge_reverify_tasks(scheduler: AsyncIOScheduler) -> None:
    """Register the monthly reverify job. Called from api/main.py startup.

    Args:
        scheduler: The shared APScheduler instance.
    """
    scheduler.add_job(
        _scheduled_entrypoint,
        trigger=CronTrigger(
            day=1, hour=4, minute=15
        ),  # 1st of month, 04:15 UTC — offset from neural_tasks consolidation at 03:00 UTC
        id="semantic_edge_reverify",
        name="Semantic Edge Reverify (#722)",
        replace_existing=True,
    )
    logger.info("scheduled_semantic_edge_reverify")
