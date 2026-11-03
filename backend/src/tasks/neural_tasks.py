"""Neural Memory background tasks.

Implements:
- Weight decay (hourly)
- Memory consolidation (daily)
"""

import math
import os
from collections.abc import AsyncIterator
from typing import Any, cast

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from config.constants import (
    CLEANUP_TOMBSTONE_RETENTION_DAYS_DEFAULT,
    CLEANUP_TOMBSTONE_RETENTION_DAYS_ENV,
    DEFERRED_POINT_BACKLOG_WARN_SECONDS,
    DEFERRED_POINT_DRAIN_INTERVAL_DEFAULT,
    DEFERRED_POINT_DRAIN_INTERVAL_ENV,
    DEFERRED_POINT_DRAIN_WAIT_DEFAULT,
    DEFERRED_POINT_DRAIN_WAIT_ENV,
    DEFERRED_POINT_DRAIN_WAIT_MAX,
)
from db.base import get_db
from db.graph_learning_lock import lock_graph_learning
from models.memory import GraphMemory
from neural.config import NeuralMemoryConfig, sleep_archive_exempt_tags_from_env
from neural.decay import DecayManager
from repositories.graph import GraphRepository
from repositories.memory import MemoryRepository
from services.graph_service import GraphService
from utils.datetime import to_utc_iso, utcnow
from utils.logger import get_logger

logger = get_logger(__name__)

# Issue #1505: the legacy consolidation path's archival floor, named so the
# durability contract returned on every write (services/persistence.py) can be
# derived from it instead of restating a literal that would silently rot if this
# task were re-tuned. The sleep path's counterpart is
# ``services.sleep.consolidation.ARCHIVE_MIN_AGE_DAYS``. At most one of the two
# runs: this one only when SLEEP_ENABLED is off (see schedule_neural_tasks), the
# sleep one only when SLEEP_ENABLED and ENABLE_NEURAL_MEMORY are both on — so
# SLEEP_ENABLED=true with neural memory off leaves NEITHER running.
LEGACY_ARCHIVE_MIN_AGE_DAYS = 30

# #2385: graphs are read and committed in pages of this size. Each page is one
# transaction, so the graph learning locks a pass holds (#1985) stay bounded.
GRAPH_PAGE_SIZE = 1000


async def _graph_pages(graph_repo: GraphRepository) -> AsyncIterator[list[GraphMemory]]:
    """Yield every graph_memory row in primary-key pages (#2385).

    ``GraphRepository.list`` orders by id, so the pages cover the table in a
    fixed order and no graph is left out once there are more than one page of
    them. The caller commits after each page; committing does not move rows
    between pages because an update keeps its id. A graph deleted by another
    session during the pass moves the later rows down one place, so one of
    them can be skipped in that pass and is picked up by the next one.

    Args:
        graph_repo: Repository bound to the task's session.

    Yields:
        Non-empty lists of at most ``GRAPH_PAGE_SIZE`` graphs, lowest ids first.
    """
    skip = 0
    while True:
        page = await graph_repo.list(skip=skip, limit=GRAPH_PAGE_SIZE)
        if not page:
            return
        yield page
        if len(page) < GRAPH_PAGE_SIZE:
            return
        skip += len(page)


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

            # #2385: every graph, one id-ordered page per transaction.
            total_graphs = 0
            total_updated = 0
            async for graphs in _graph_pages(graph_repo):
                total_graphs += len(graphs)
                # #1985: users in a fixed order, each graph's edge writes under the
                # graph learning lock so a live recall pass never interleaves.
                for graph_model in sorted(graphs, key=lambda g: g.user_id):
                    user_id = graph_model.user_id
                    await lock_graph_learning(db, user_id)

                    graph_service = GraphService(user_id, db)

                    # Apply decay (uses SQL backend directly). Issue #970: pass the
                    # persisted last_decay_at so Δt reflects REAL elapsed time — a
                    # fresh DecayManager is constructed per run and has no instance
                    # state across runs, so without this the decay always used the
                    # default interval (the "dead _last_decay_time" bug).
                    decay_manager = DecayManager(graph_service, config)
                    decay_result = await decay_manager.apply_decay(
                        user_id, last_decay_at=graph_model.last_decay_at
                    )
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
                total_graphs=total_graphs,
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
    # #2584: SLEEP_ARCHIVE_EXEMPT_TAGS binds this pass too — it is the one a
    # deployment without SLEEP_ENABLED runs, and its delete is physical.
    exempt_tags = frozenset(sleep_archive_exempt_tags_from_env())
    logger.info("consolidation_task_started")

    try:
        async for db in get_db():
            memory_repo = MemoryRepository(db)
            graph_repo = GraphRepository(db)

            total_users = 0
            total_promoted = 0
            total_deleted = 0

            # #2385: every user with a graph_memory row, one id-ordered page
            # per transaction.
            async for graphs in _graph_pages(graph_repo):
                user_ids = [g.user_id for g in graphs]
                total_users += len(user_ids)

                for user_id in sorted(user_ids):
                    # #1985: deleting a memory cascades to its edge rows; take the
                    # graph learning lock first (users in a fixed order).
                    await lock_graph_learning(db, user_id)
                    # Get working memories
                    working_memories = await memory_repo.list(
                        filters={"user_id": user_id, "scope": "working"}
                    )

                    # #1985: promotions are written in one id-ordered statement
                    # after the loop (no row locks taken against recall's order).
                    to_promote: dict[Any, tuple[Any, int]] = {}
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
                            to_promote[memory.id] = (memory, age_days)

                        # #1523: a pinned row (delivery_mode='always') is never an
                        # automated-delete candidate — same exemption as the Sleep
                        # phases, and this branch is a physical delete with no
                        # tombstone to restore from.
                        elif memory.is_pinned:
                            logger.info(
                                "legacy_consolidation_skipped_pinned", memory_id=str(memory.id)
                            )

                        # Tool guardrails share the exemption: a physically deleted
                        # guardrail would silently stop firing in every client hook.
                        elif memory.is_tool_triggered:
                            logger.info(
                                "legacy_consolidation_skipped_tool_triggered",
                                memory_id=str(memory.id),
                            )

                        # #2584: an exempt-tagged row (an open review memo) is
                        # never deleted; removing the tag ends the exemption.
                        # Debug level: open memos are many and stay for months.
                        elif not exempt_tags.isdisjoint(memory.tags or ()):
                            logger.debug(
                                "legacy_consolidation_skipped_exempt_tag",
                                memory_id=str(memory.id),
                            )

                        # Deletion criteria
                        elif age_days >= LEGACY_ARCHIVE_MIN_AGE_DAYS and memory.access_count == 0:
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

                            await delete_memory_from_qdrant(user_id, memory.point_id)

                            # Then delete from PostgreSQL
                            await memory_repo.delete(memory.id)
                            total_deleted += 1

                            logger.info(
                                "old_memory_deleted",
                                memory_id=str(memory.id),
                                user_id=user_id,
                                age_days=age_days,
                            )

                    promoted = (
                        await memory_repo.bulk_promote_to_persistent(list(to_promote))
                        if to_promote
                        else []
                    )
                    for memory_id in promoted:
                        memory, age_days = to_promote[memory_id]
                        total_promoted += 1
                        logger.info(
                            "memory_promoted",
                            memory_id=str(memory_id),
                            user_id=user_id,
                            access_count=memory.access_count,
                            age_days=age_days,
                            importance=memory.importance,
                        )

                await db.commit()

            logger.info(
                "consolidation_task_completed",
                users=total_users,
                promoted=total_promoted,
                deleted=total_deleted,
            )
            return

    except Exception as e:
        logger.error("consolidation_task_failed", error=str(e), exc_info=True)


def _sleep_enabled() -> bool:
    """Whether Sleep maintenance is on. Read at call time, not import time."""
    return os.getenv("SLEEP_ENABLED", "false").lower() == "true"


def _cleanup_retention_days() -> int:
    """The tombstone sweep window (#1521): ``CLEANUP_DELETED_MEMORIES_RETENTION_DAYS``.

    Same env pattern as the other task knobs in this module. 0 disables the
    sweep; an unparsable or negative value falls back to the default and is
    logged, so a typo never silently turns into an aggressive purge.
    """
    raw = os.getenv(
        CLEANUP_TOMBSTONE_RETENTION_DAYS_ENV, str(CLEANUP_TOMBSTONE_RETENTION_DAYS_DEFAULT)
    )
    try:
        days = int(raw)
    except ValueError:
        days = -1
    if days < 0:
        logger.warning(
            "cleanup_deleted_memories_retention_invalid",
            value=raw,
            fallback_days=CLEANUP_TOMBSTONE_RETENTION_DAYS_DEFAULT,
        )
        return CLEANUP_TOMBSTONE_RETENTION_DAYS_DEFAULT
    return days


async def cleanup_deleted_memories_task():
    """Permanently delete every soft-deleted memory older than the sweep window.

    Runs daily at 4 AM UTC. Qdrant points are already removed at soft-delete
    time (forget, context deletion, merge, archive), best-effort; the row is
    what this task removes, and ``sweep_orphan_vectors_task`` removes a point
    that outlived its row (#1798).

    #1521: the window is ``CLEANUP_DELETED_MEMORIES_RETENTION_DAYS`` (default
    30; 0 disables the sweep). It applies to every tombstone regardless of
    ``deleted_by`` or context state — this sweep is what bounds the erasure of
    a ``forget()`` row (#1336) and the reversibility of merges and archives in
    deployments where Sleep never visits the row (Sleep off, ``sleep_mode=
    'skip'``, dead contexts). The Sleep retention windows
    (``sleep_merge_retention_days`` / ``sleep_forget_retention_days``) purge
    earlier when set; they never extend past this one.
    """
    logger.info("cleanup_deleted_memories_task_started")

    try:
        from datetime import timedelta

        retention_days = _cleanup_retention_days()
        if retention_days == 0:
            logger.info("cleanup_deleted_memories_disabled", reason="retention_days_zero")
            return

        async for db in get_db():
            cutoff = utcnow() - timedelta(days=retention_days)

            from sqlalchemy import delete
            from sqlalchemy.engine import CursorResult

            from models.memory import Memory

            # Set-based, like services.sleep.merge_retention.purge_tombstones:
            # the predicate is evaluated at DELETE time, so a row restored by
            # a concurrent undo/rollback (deleted_at back to NULL) survives.
            result = await db.execute(
                delete(Memory).where(
                    Memory.deleted_at.is_not(None),
                    Memory.deleted_at < cutoff,
                )
            )
            # Result[Any] at type level, CursorResult at runtime (#1442).
            purged = int(cast(CursorResult[Any], result).rowcount or 0)
            await db.commit()

            logger.info(
                "cleanup_deleted_memories_task_completed",
                purged=purged,
                retention_days=retention_days,
                cutoff=to_utc_iso(cutoff),
            )
            return

    except Exception as e:
        logger.error("cleanup_deleted_memories_task_failed", error=str(e), exc_info=True)


def _orphan_sweep_enabled() -> bool:
    """Whether the daily orphan vector sweep runs (#1798). Read at call time.

    The switch guards a job that deletes, so every common way of writing
    "off" turns it off.
    """
    value = os.getenv("ORPHAN_VECTOR_SWEEP_ENABLED", "true").strip().lower()
    return value not in ("false", "0", "no", "off")


async def sweep_orphan_vectors_task() -> None:
    """Delete vector-store points that no live row points at (#1798).

    Runs daily at 4:30 AM UTC, after the tombstone purge. Postgres commits
    first and the vector store follows best-effort, so a failed vector delete
    leaves a point nothing else removes. See services/orphan_vector_sweep.py
    for what counts as an orphan; a point whose memory is live never does.

    The scheduled run refuses to delete more than half of what it scanned —
    that is a sweep pointed at the wrong database, or a backlog an operator
    should look at first with ``python -m src.cli.sweep_orphan_vectors``.
    ``ORPHAN_VECTOR_SWEEP_ENABLED=false`` turns the scheduled run off.
    """
    if not _orphan_sweep_enabled():
        logger.info("orphan_vector_sweep_disabled")
        return

    logger.info("orphan_vector_sweep_task_started")
    try:
        from services.orphan_vector_sweep import (
            SCHEDULED_MAX_ORPHAN_RATIO,
            sweep_orphan_points,
        )
        from tasks.single_flight import single_flight

        # One API process per deployment sweeps; the others would only repeat
        # the full scan and report the same deletes again.
        async with single_flight("orphan_vector_sweep") as acquired:
            if not acquired:
                logger.info("orphan_vector_sweep_task_skipped", reason="another_process_running")
                return
            async for db in get_db():
                result = await sweep_orphan_points(
                    db, dry_run=False, max_orphan_ratio=SCHEDULED_MAX_ORPHAN_RATIO
                )
                logger.info(
                    "orphan_vector_sweep_task_completed",
                    scanned=result.scanned,
                    orphans=result.orphans,
                    by_reason=result.by_reason,
                    deleted=result.deleted,
                    refused=result.refused,
                    live_embedded_memories=result.live_embedded_memories,
                )
                break
    except Exception as e:
        logger.error("orphan_vector_sweep_task_failed", error=str(e), exc_info=True)


def _env_seconds(name: str, default: float, maximum: float | None = None) -> float:
    """A non-negative number of seconds from ``name``. Read at call time.

    An unparsable, negative or above-``maximum`` value falls back to
    ``default`` and is logged, like the tombstone retention window, so a typo
    never turns into a wait without bound or a job silently switched off.
    """
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        value = -1.0
    if not math.isfinite(value) or value < 0 or (maximum is not None and value > maximum):
        logger.warning("env_seconds_invalid", name=name, value=raw, fallback=default)
        return default
    return value


def _deferred_point_drain_interval() -> int:
    """Seconds between drain runs (#2476); 0 turns the job off."""
    seconds = _env_seconds(DEFERRED_POINT_DRAIN_INTERVAL_ENV, DEFERRED_POINT_DRAIN_INTERVAL_DEFAULT)
    # A fraction of a second still means "on": never round it down to off.
    return max(1, int(seconds)) if seconds > 0 else 0


def _deferred_point_drain_wait() -> float:
    """Seconds a drain run waits for the point writers (#2476); 0 does not wait."""
    return _env_seconds(
        DEFERRED_POINT_DRAIN_WAIT_ENV,
        DEFERRED_POINT_DRAIN_WAIT_DEFAULT,
        maximum=DEFERRED_POINT_DRAIN_WAIT_MAX,
    )


async def drain_deferred_point_deletes_task() -> None:
    """Delete the points ``forget`` queued because a point writer held the lock (#2476).

    Runs every ``DEFERRED_POINT_DRAIN_INTERVAL_SECONDS`` (default 300) and
    waits up to ``DEFERRED_POINT_DRAIN_WAIT_SECONDS`` (default 30) for the
    point writers — the resource indexer holds the lock for a whole batch —
    while it queues every new writer behind it, so the wait is the knob to
    lower on a deployment where writer stalls matter. No lock is taken when
    nothing is queued. Independent of ``ORPHAN_VECTOR_SWEEP_ENABLED``: with the
    sweep off this job is what removes those points.

    No single_flight: the scheduler leader runs one copy, and a second run in
    another process only waits on the same lock and finds the rows gone.
    """
    try:
        from services.deferred_point_deletes import drain_deferred_point_deletes

        async for db in get_db():
            result = await drain_deferred_point_deletes(
                db, wait_seconds=_deferred_point_drain_wait()
            )
            fields = {
                "drained": result.drained,
                "kept": result.kept,
                "pending": result.pending,
                "oldest_age_seconds": result.oldest_age_seconds,
            }
            if result.busy:
                logger.info("deferred_point_deletes_writers_busy", **fields)
            elif result.drained or result.kept:
                logger.info("deferred_point_deletes_drained", **fields)
            if (
                result.oldest_age_seconds is not None
                and result.oldest_age_seconds > DEFERRED_POINT_BACKLOG_WARN_SECONDS
            ):
                logger.warning("deferred_point_deletes_backlog_old", **fields)
            break
    except Exception as e:
        logger.error(
            "deferred_point_deletes_task_failed", error_class=type(e).__name__, exc_info=True
        )


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
    if not _sleep_enabled():
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

    # Cleanup Deleted Memories: Daily at 4 AM UTC. Always scheduled; the
    # window is CLEANUP_DELETED_MEMORIES_RETENTION_DAYS (#1521, 0 disables).
    scheduler.add_job(
        cleanup_deleted_memories_task,
        trigger=CronTrigger(hour=4, minute=0),
        id="cleanup_deleted",
        name="Cleanup Deleted Memories",
        replace_existing=True,
    )
    logger.info("scheduled_cleanup_deleted_task")

    # Orphan vector sweep (#1798): daily at 4:30 AM UTC, after the purge above.
    scheduler.add_job(
        sweep_orphan_vectors_task,
        trigger=CronTrigger(hour=4, minute=30),
        id="sweep_orphan_vectors",
        name="Sweep Orphan Vectors",
        replace_existing=True,
    )
    logger.info("scheduled_orphan_vector_sweep_task")

    # Deferred point deletes (#2476): forget's points a point writer kept it
    # from deleting. Not tied to the orphan sweep switch.
    drain_interval = _deferred_point_drain_interval()
    if drain_interval > 0:
        scheduler.add_job(
            drain_deferred_point_deletes_task,
            trigger=IntervalTrigger(seconds=drain_interval),
            id="drain_deferred_point_deletes",
            name="Drain Deferred Point Deletes",
            replace_existing=True,
        )
        logger.info("scheduled_deferred_point_drain_task", interval_seconds=drain_interval)
    else:
        logger.info("deferred_point_drain_disabled", reason="interval_zero")
