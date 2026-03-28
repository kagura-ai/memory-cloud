"""Neural Memory background tasks.

Implements:
- Weight decay (hourly)
- Memory consolidation (daily)
"""

import logging
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
from utils.datetime import utcnow

logger = logging.getLogger(__name__)


async def weight_decay_task():
    """Apply weight decay to all users' graphs.

    Runs every hour to decay unused edge weights and prune weak edges.
    """
    logger.info("weight_decay_task_started")

    # Check if Neural Memory is enabled
    if os.getenv("ENABLE_NEURAL_MEMORY", "false").lower() != "true":
        logger.info("weight_decay_task_skipped")
        return

    # Check if decay is enabled
    if os.getenv("ENABLE_DECAY", "true").lower() != "true":
        logger.info("weight_decay_task_skipped")
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
                    stats = await graph_service.stats()

                    graph_model.total_nodes = stats["total_nodes"]
                    graph_model.total_edges = stats["total_edges"]
                    graph_model.avg_edge_weight = stats["avg_edge_weight"]
                    graph_model.max_edge_weight = stats["max_edge_weight"]
                    graph_model.last_decay_at = utcnow()
                    graph_model.updated_at = utcnow()

                    total_updated += 1

                    logger.info(
                        f"graph_decay_applied: user={user_id}, "
                        f"edges_decayed={edges_decayed}, total_edges={stats['total_edges']}"
                    )

            await db.commit()

            logger.info(
                f"weight_decay_task_completed: total_graphs={len(graphs)}, "
                f"updated_graphs={total_updated}"
            )
            return

    except Exception as e:
        logger.error(f"weight_decay_task_failed: {e}", exc_info=True)


async def consolidation_task():
    """Consolidate working memories to persistent.

    Runs daily at 3 AM UTC to promote frequently used working memories
    and delete old unused ones.
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

            # Get environment variable thresholds (Issue #44)
            neural_centrality_threshold = float(os.getenv("NEURAL_CENTRALITY_THRESHOLD", "0.7"))
            neural_hub_threshold = int(os.getenv("NEURAL_HUB_NODE_THRESHOLD", "5"))
            neural_weight_threshold = float(os.getenv("NEURAL_EDGE_WEIGHT_THRESHOLD", "0.8"))

            for user_id in user_ids:
                # Load graph for Neural Memory metrics (Issue #44)
                graph_service = GraphService(user_id, db)
                graph_stats = await graph_service.stats()
                has_graph = graph_stats["total_edges"] > 0

                # Get working memories
                working_memories = await memory_repo.list(
                    filters={"user_id": user_id, "scope": "working"}
                )

                for memory in working_memories:
                    age_days = (utcnow() - memory.created_at).days

                    # Get Neural Memory metrics (Issue #44)
                    neural_metrics = None
                    if has_graph:
                        neural_metrics = graph_service.get_node_metrics(str(memory.id))

                    # Enhanced promotion criteria (Issue #44)
                    should_promote = (
                        # Existing criteria (from Issue #1)
                        # Pattern 1: Frequently used + Important
                        (memory.access_count >= 3 and memory.importance >= 0.5)
                        # Pattern 2: Very frequently used
                        or (memory.access_count >= 5)
                        # Pattern 3: Important + aged
                        or (memory.importance >= 0.8 and age_days >= 3)
                        # Pattern 4: Old + used at least once
                        or (age_days >= 30 and memory.access_count >= 1)
                        # NEW: Neural Memory criteria (Issue #44)
                        or (
                            neural_metrics
                            and neural_metrics["centrality"] >= neural_centrality_threshold
                        )
                        or (neural_metrics and neural_metrics["edge_count"] >= neural_hub_threshold)
                        or (
                            neural_metrics
                            and neural_metrics["avg_edge_weight"] >= neural_weight_threshold
                        )
                    )

                    if should_promote:
                        # Promote to persistent
                        await memory_repo.promote_to_persistent(memory.id)
                        total_promoted += 1

                        promotion_reason = "standard"
                        if neural_metrics:
                            if neural_metrics["centrality"] >= neural_centrality_threshold:
                                promotion_reason = "neural_centrality"
                            elif neural_metrics["edge_count"] >= neural_hub_threshold:
                                promotion_reason = "neural_hub"
                            elif neural_metrics["avg_edge_weight"] >= neural_weight_threshold:
                                promotion_reason = "neural_weight"

                        logger.info(
                            f"memory_promoted: memory_id={memory.id}, user={user_id}, "
                            f"access_count={memory.access_count}, age_days={age_days}, "
                            f"importance={memory.importance}, reason={promotion_reason}, "
                            f"neural_metrics={neural_metrics}"
                        )

                    # Enhanced deletion criteria (Issue #44)
                    elif (
                        age_days >= 30
                        and memory.access_count == 0
                        and (not neural_metrics or neural_metrics["is_isolated"])
                    ):
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
                            f"old_memory_deleted: memory_id={memory.id}, "
                            f"user={user_id}, age_days={age_days}, "
                            f"isolated={neural_metrics['is_isolated'] if neural_metrics else True}"
                        )

            await db.commit()

            logger.info(
                f"consolidation_task_completed: users={len(user_ids)}, "
                f"promoted={total_promoted}, deleted={total_deleted}"
            )
            return

    except Exception as e:
        logger.error(f"consolidation_task_failed: {e}", exc_info=True)


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
                logger.info(
                    f"old_deleted_memory_purged: memory_id={memory.id}, "
                    f"deleted_at={memory.deleted_at}, age_days={(utcnow() - memory.deleted_at).days}"
                )

            await db.commit()

            logger.info(
                f"cleanup_deleted_memories_task_completed: purged={total_deleted}, cutoff={cutoff}"
            )
            return

    except Exception as e:
        logger.error(f"cleanup_deleted_memories_task_failed: {e}", exc_info=True)


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
    scheduler.add_job(
        consolidation_task,
        trigger=CronTrigger(hour=3, minute=0),
        id="consolidation",
        name="Memory Consolidation",
        replace_existing=True,
    )
    logger.info("scheduled_consolidation_task")

    # Cleanup Deleted Memories: Daily at 4 AM UTC
    scheduler.add_job(
        cleanup_deleted_memories_task,
        trigger=CronTrigger(hour=4, minute=0),
        id="cleanup_deleted",
        name="Cleanup Deleted Memories",
        replace_existing=True,
    )
    logger.info("scheduled_cleanup_deleted_task")
