"""Neural Memory background tasks.

Implements:
- Weight decay (hourly)
- Memory consolidation (daily)
"""

import os

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from db.base import get_db
from neural.config import NeuralMemoryConfig
from neural.decay import DecayManager
from repositories.graph import GraphRepository
from repositories.memory import MemoryRepository
from services.graph_service import GraphService
from utils.datetime import to_utc_iso, utcnow
from utils.logger import get_logger

logger = get_logger(__name__)


async def weight_decay_task():
    """Apply weight decay to all users' graphs.

    Runs every hour to decay unused edge weights and prune weak edges.
    """
    logger.info("weight_decay_task_started")

    # Check if Neural Memory is enabled
    if os.getenv("ENABLE_NEURAL_MEMORY", "false").lower() != "true":
        logger.info("weight_decay_task_skipped", reason="neural_memory_disabled")
        return

    # Check if decay is enabled
    if os.getenv("ENABLE_DECAY", "true").lower() != "true":
        logger.info("weight_decay_task_skipped", reason="decay_disabled")
        return

    try:
        async for db in get_db():
            graph_repo = GraphRepository(db)
            # Issue #107: DB-driven config
            config = await NeuralMemoryConfig.from_db(db)

            # Get all graphs
            graphs = await graph_repo.list(skip=0, limit=1000)
            total_updated = 0

            for graph_model in graphs:
                user_id = graph_model.user_id

                graph_service = GraphService(user_id, db)

                # Apply decay (uses SQL backend directly)
                decay_manager = DecayManager(graph_service, config)
                decay_result = await decay_manager.apply_decay(user_id)
                edges_decayed = decay_result.get("edges_decayed", 0)

                if edges_decayed > 0:
                    # Issue #651: previously called graph_service.stats() here to
                    # refresh graph_memory.total_*/avg/max cache columns. That call
                    # violates the 3-level isolation invariant from #273 H-2 / #383
                    # (workspace_id/context_id are unavailable in this global
                    # cross-tenant task, and the validator on get_stats rejects
                    # None pairs to prevent cross-tenant aggregation). The cache
                    # columns are write-only — no consumer reads them — so
                    # dropping the refresh has no functional impact. Removal of
                    # the dead columns themselves is tracked as a follow-up
                    # issue (TBD — to be filed when this PR lands).
                    now = utcnow()
                    graph_model.last_decay_at = now
                    graph_model.updated_at = now

                    total_updated += 1

                    logger.info(
                        "graph_decay_applied",
                        user_id=user_id,
                        edges_decayed=edges_decayed,
                        edges_pruned=decay_result.get("edges_pruned", 0),
                    )

            await db.commit()

            logger.info(
                "weight_decay_task_completed",
                total_graphs=len(graphs),
                updated_graphs=total_updated,
            )
            return

    except Exception as e:
        logger.error("weight_decay_task_failed", error=str(e), exc_info=True)


async def consolidation_task():
    """Consolidate working memories to persistent.

    Runs daily at 3 AM UTC to promote frequently used working memories
    and delete old unused ones.

    Issue #651: removed the Neural Memory metrics integration (the Issue #44
    enhancement). graph_service.stats() and graph_service.get_node_metrics()
    both require workspace_id and context_id, which are not available in this
    global cross-tenant task, and the get_stats validator rejects None pairs
    to prevent cross-tenant aggregation (#273 H-2 / #383). Additionally, the
    get_node_metrics call in main lacked an ``await``, so the entire neural-
    enhanced branch was already raising TypeError and being swallowed by the
    bare ``except`` — the integration was non-functional in practice. Promotion
    and deletion now use only the original Issue #1 criteria (access patterns,
    importance, age). The deletion guard's former ``is_isolated`` check is
    therefore not restored here (it never executed in prod). Re-introducing
    neural metrics under the 3-level isolation model — including a properly
    awaited isolation-aware ``is_isolated`` deletion guard — is tracked as a
    follow-up issue (TBD — to be filed when this PR lands).

    Note: this task only runs when ``SLEEP_ENABLED=false``. With sleep
    enabled (the production setting), ``services/sleep/consolidation.py``
    handles consolidation and retains its own neural metrics path.
    """
    logger.info("consolidation_task_started")

    try:
        async for db in get_db():
            memory_repo = MemoryRepository(db)
            graph_repo = GraphRepository(db)

            # Get all users (from graph_memory table)
            graphs = await graph_repo.list(skip=0, limit=1000)
            user_ids = [g.user_id for g in graphs]

            total_promoted = 0
            total_deleted = 0

            for user_id in user_ids:
                # Get working memories
                working_memories = await memory_repo.list(
                    filters={"user_id": user_id, "scope": "working"}
                )

                for memory in working_memories:
                    age_days = (utcnow() - memory.created_at).days

                    # Promotion criteria (Issue #1)
                    should_promote = (
                        # Pattern 1: Frequently used + Important
                        (memory.access_count >= 3 and memory.importance >= 0.5)
                        # Pattern 2: Very frequently used
                        or (memory.access_count >= 5)
                        # Pattern 3: Important + aged
                        or (memory.importance >= 0.8 and age_days >= 3)
                        # Pattern 4: Old + used at least once
                        or (age_days >= 30 and memory.access_count >= 1)
                    )

                    if should_promote:
                        # Promote to persistent
                        await memory_repo.promote_to_persistent(memory.id)
                        total_promoted += 1

                        logger.info(
                            "memory_promoted",
                            memory_id=str(memory.id),
                            user_id=user_id,
                            access_count=memory.access_count,
                            age_days=age_days,
                            importance=memory.importance,
                        )

                    # Deletion criteria
                    elif age_days >= 30 and memory.access_count == 0:
                        # ================================================================
                        # BUG FIX #83-10: Delete from Qdrant to prevent orphan vectors
                        # ================================================================
                        # Problem: consolidation_task was deleting from PostgreSQL only,
                        #          leaving orphan vectors in Qdrant. These orphans:
                        #          - Waste storage
                        #          - Pollute search results
                        #          - Cannot be cleaned up easily
                        #
                        # Solution: Call delete_memory_from_qdrant() before PostgreSQL
                        #           deletion to ensure complete cleanup.
                        #
                        # Note: This matches the pattern in MemoryService.forget() API.
                        # ================================================================

                        # Delete from Qdrant first
                        from db.qdrant import delete_memory_from_qdrant

                        await delete_memory_from_qdrant(user_id, memory.id)

                        # Then delete from PostgreSQL
                        await memory_repo.delete(memory.id)
                        total_deleted += 1

                        logger.info(
                            "old_memory_deleted",
                            memory_id=str(memory.id),
                            user_id=user_id,
                            age_days=age_days,
                        )

            await db.commit()

            logger.info(
                "consolidation_task_completed",
                users=len(user_ids),
                promoted=total_promoted,
                deleted=total_deleted,
            )
            return

    except Exception as e:
        logger.error("consolidation_task_failed", error=str(e), exc_info=True)


async def cleanup_deleted_memories_task():
    """Permanently delete soft-deleted memories older than 30 days.

    Runs daily at 4 AM UTC to clean up old deleted memories from PostgreSQL.
    Note: Qdrant entries are already deleted when memory is soft-deleted.
    """
    logger.info("cleanup_deleted_memories_task_started")

    try:
        from datetime import timedelta

        async for db in get_db():
            memory_repo = MemoryRepository(db)

            # Calculate cutoff date (30 days ago)
            cutoff = utcnow() - timedelta(days=30)

            # Find old deleted memories
            from sqlalchemy import and_, select

            from models.memory import Memory

            result = await db.execute(
                select(Memory).where(
                    and_(Memory.deleted_at.isnot(None), Memory.deleted_at < cutoff)
                )
            )
            old_deleted = list(result.scalars().all())

            total_deleted = 0
            for memory in old_deleted:
                await memory_repo.delete(memory.id)
                total_deleted += 1
                # WHERE clause above filters Memory.deleted_at.isnot(None),
                # so deleted_at is guaranteed non-None here. The assert
                # narrows the type for pyright (which cannot infer through
                # SQLAlchemy filter expressions).
                assert memory.deleted_at is not None
                logger.info(
                    "old_deleted_memory_purged",
                    memory_id=str(memory.id),
                    deleted_at=to_utc_iso(memory.deleted_at),
                    age_days=(utcnow() - memory.deleted_at).days,
                )

            await db.commit()

            logger.info(
                "cleanup_deleted_memories_task_completed",
                purged=total_deleted,
                cutoff=to_utc_iso(cutoff),
            )
            return

    except Exception as e:
        logger.error("cleanup_deleted_memories_task_failed", error=str(e), exc_info=True)


def schedule_neural_tasks(scheduler: AsyncIOScheduler) -> None:
    """Schedule Neural Memory background tasks.

    Args:
        scheduler: APScheduler instance
    """
    # Weight Decay: Every hour
    decay_interval = int(os.getenv("DECAY_BACKGROUND_INTERVAL", "3600"))
    scheduler.add_job(
        weight_decay_task,
        trigger=IntervalTrigger(seconds=decay_interval),
        id="weight_decay",
        name="Neural Memory Weight Decay",
        replace_existing=True,
    )
    logger.info("scheduled_weight_decay_task")

    # Consolidation: Daily at 3 AM UTC
    # When Sleep Maintenance is enabled, Phase 4 handles consolidation instead
    if os.getenv("SLEEP_ENABLED", "false").lower() != "true":
        scheduler.add_job(
            consolidation_task,
            trigger=CronTrigger(hour=3, minute=0),
            id="consolidation",
            name="Memory Consolidation",
            replace_existing=True,
        )
        logger.info("scheduled_consolidation_task", mode="legacy")
    else:
        logger.info("consolidation_task_skipped", reason="sleep_maintenance_handles_this")

    # Cleanup Deleted Memories: Daily at 4 AM UTC
    scheduler.add_job(
        cleanup_deleted_memories_task,
        trigger=CronTrigger(hour=4, minute=0),
        id="cleanup_deleted",
        name="Cleanup Deleted Memories",
        replace_existing=True,
    )
    logger.info("scheduled_cleanup_deleted_task")
