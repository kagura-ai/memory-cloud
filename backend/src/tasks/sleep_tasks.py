"""Sleep Maintenance background tasks.

Issue #101/#103: Scheduled sleep maintenance for all users/contexts.
Issue #223: Per-(workspace, context) hub-tag cache refresh runs alongside
the per-context orchestrator loop.
"""

import os

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.ext.asyncio import AsyncSession

from db.base import get_db
from tasks.single_flight import single_flight
from utils.logger import get_logger

logger = get_logger(__name__)


async def _refresh_hub_tag_cache(
    db: AsyncSession,
    workspace_id: str,
    context_id: str,
    threshold: float,
) -> int:
    """Recompute the hub-tag set for one (workspace, context) and upsert.

    Issue #223: hub tags = tags appearing on more than ``threshold`` fraction
    of the non-deleted memories within this (workspace, context). Used by
    ``_create_tag_cooccurrence_seed_edges`` to skip overly-popular tags that
    would otherwise create hub explosions in the graph.

    Args:
        db: AsyncSession (caller owns commit/rollback).
        workspace_id: Workspace UUID as string.
        context_id: Context UUID as string.
        threshold: Frequency threshold from
            ``NeuralMemoryConfig.tag_cooccurrence_hub_threshold``.

    Returns:
        Number of hub tags found (may be 0 — that's a valid result and is
        upserted explicitly so readers can distinguish "computed: nothing"
        from "never computed").
    """
    from uuid import UUID

    from sqlalchemy import text
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from models.hub_tag import HubTagCache

    # Single SQL: count distinct memory occurrences per tag, normalize by
    # total memory count in (workspace, context), and return tags above
    # threshold. Note: this query does NOT exercise the GIN index on
    # ``memories.tags`` (no array-overlap / contains operators) — it walks
    # all in-scope rows. The GIN index is used by the seeding query in
    # ``memory_service._create_tag_cooccurrence_seed_edges``, not here.
    sql = text(
        """
        WITH scope AS (
            SELECT id, tags
            FROM memories
            WHERE workspace_id = CAST(:workspace_id AS uuid)
              AND context_id = CAST(:context_id AS uuid)
              AND deleted_at IS NULL
              AND tags IS NOT NULL
              AND cardinality(tags) > 0
        ),
        total AS (
            -- Denominator is ALL non-deleted memories in the (workspace,
            -- context), NOT just the tagged subset. The hub-threshold
            -- semantic per #223 section 5 is "tags appearing on > X% of
            -- memories within the (workspace, context) scope" — measured
            -- against every memory the user has, not only those that
            -- happen to carry tags. Using ``scope`` here would inflate
            -- frequencies in contexts with many untagged memories
            -- (Copilot loop 5 catch).
            SELECT COUNT(*)::float AS n
            FROM memories
            WHERE workspace_id = CAST(:workspace_id AS uuid)
              AND context_id = CAST(:context_id AS uuid)
              AND deleted_at IS NULL
        ),
        tag_counts AS (
            -- Count DISTINCT memories per tag, not raw unnest occurrences
            -- (Copilot loop 2): if a memory's tags array contains duplicate
            -- entries (e.g. ["python", "python", "backend"]), counting raw
            -- unnest rows would over-count "python" as 2 even though it
            -- appears in only 1 memory. Wrapping in SELECT DISTINCT id, tag
            -- collapses duplicates per memory before the GROUP BY.
            --
            -- Cast unnest(tags) to text so ARRAY_AGG produces text[] and the
            -- COALESCE fallback ``ARRAY[]::text[]`` is element-type-compatible
            -- (Copilot loop 1: ``Column(ARRAY(String))`` → varchar[] in PG).
            SELECT tag, COUNT(*) AS cnt
            FROM (
                SELECT DISTINCT id, unnest(tags)::text AS tag
                FROM scope
            ) sub
            GROUP BY tag
        )
        -- Outer SELECT has NO `FROM tag_counts` — that would return 0 rows
        -- when tag_counts is empty (context has memories but none are
        -- tagged), making `memory_count` incorrectly absent for that
        -- context. Instead, build both fields as scalar subqueries from
        -- the FROM-less SELECT, which always returns exactly 1 row.
        -- ARRAY_AGG inside the scalar subquery handles the empty-tag case
        -- naturally (returns NULL → COALESCE → []) (Copilot loop 6 catch).
        SELECT
            (SELECT n FROM total)::int AS memory_count,
            COALESCE(
                (
                    SELECT ARRAY_AGG(tag) FILTER (
                        WHERE (SELECT n FROM total) > 0
                          AND cnt::float / (SELECT n FROM total) > :threshold
                    )
                    FROM tag_counts
                ),
                ARRAY[]::text[]
            ) AS hub_tags
        """
    )
    result = await db.execute(
        sql,
        {
            "workspace_id": workspace_id,
            "context_id": context_id,
            "threshold": threshold,
        },
    )
    # Defensive: aggregate-without-GROUP-BY *should* return exactly 1 row
    # even on empty input, but use one_or_none() so a future SQL refactor
    # that accidentally turns it into a "0 rows on empty scope" query does
    # not raise NoResultFound for every context with zero tagged memories
    # (which would log the whole nightly hub-tag refresh as failed for that
    # context). Treat None as "empty scope, write empty hub set" — the same
    # outcome the cache should reflect anyway. (Copilot loop 2.)
    row = result.one_or_none()
    if row is None:
        memory_count = 0
        hub_tags: list[str] = []
    else:
        memory_count = int(row.memory_count)
        hub_tags = list(row.hub_tags or [])

    # Upsert on (workspace_id, context_id). Bypass ORM for the upsert so we
    # can use the DB-side conflict resolution rather than SELECT-then-INSERT.
    # Coerce str → UUID explicitly: the columns are ``UUID(as_uuid=True)`` and
    # the postgresql dialect ``insert()`` does not invoke the column type
    # processor for raw kwargs the same way ORM ``add()`` would, so a plain
    # str can fail bind on asyncpg (Copilot loop 3).
    stmt = pg_insert(HubTagCache).values(
        workspace_id=UUID(workspace_id),
        context_id=UUID(context_id),
        hub_tags=hub_tags,
        memory_count=memory_count,
        threshold_used=threshold,
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_hub_tag_cache_ws_ctx",
        set_={
            "hub_tags": stmt.excluded.hub_tags,
            "memory_count": stmt.excluded.memory_count,
            "threshold_used": stmt.excluded.threshold_used,
            "computed_at": text("NOW()"),
        },
    )
    await db.execute(stmt)
    return len(hub_tags)


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

    # Issue #933: cross-process single-flight guard. Both the blue and green API
    # containers run APScheduler in-process, so without this guard each process
    # fires the nightly cron independently and every context is swept twice
    # (``max_instances=1`` only dedupes within one process). The first process to
    # acquire the Postgres advisory lock runs the sweep; the others no-op.
    try:
        async with single_flight("sleep_maintenance") as acquired:
            if not acquired:
                logger.info(
                    "sleep_maintenance_task_skipped",
                    reason="lock_held_by_other_process",
                )
                return
            await _sleep_maintenance_run()
    except Exception as e:
        # Lock-acquisition / infra failures (e.g. Postgres unreachable when the
        # cron fires) occur outside _sleep_maintenance_run's own handler. Use a
        # DISTINCT event name from the body-failure event (sleep_maintenance_task_failed)
        # so alerting can route the two correctly: this one means "could not even
        # start the sweep" (DB connectivity — wide blast radius), the inner one
        # means "the sweep itself failed".
        logger.error("sleep_maintenance_lock_acquire_failed", error=str(e), exc_info=True)


async def _sleep_maintenance_run() -> None:
    """Run the Sleep Maintenance sweep across all active (user, workspace, context) tuples.

    Extracted from ``sleep_maintenance_task`` (Issue #933) so the cross-process
    advisory lock wraps the entire multi-commit run — the per-context loop below
    commits repeatedly, so a transaction-scoped lock would not span it.
    """
    try:
        async for db in get_db():
            from sqlalchemy import select

            from models.memory import Memory
            from neural.config import NeuralMemoryConfig
            from services.sleep.orchestrator import SleepOrchestrator

            # Load config once for all contexts (not per-context)
            NeuralMemoryConfig.invalidate_cache()
            config = await NeuralMemoryConfig.from_db(db)

            # Find distinct (user_id, workspace_id, context_id) combinations
            stmt = (
                select(
                    Memory.user_id,
                    Memory.workspace_id,
                    Memory.context_id,
                )
                .distinct()
                .where(Memory.deleted_at.is_(None))
                .where(Memory.workspace_id.isnot(None))
                .where(Memory.context_id.isnot(None))
            )

            result = await db.execute(stmt)
            contexts = result.all()

            total_runs = 0
            total_errors = 0

            # Issue #223: refresh hub-tag cache once per (workspace, context),
            # *not* per (user, workspace, context). Multiple users in a
            # shared context would otherwise re-compute the same set
            # redundantly. Done before the orchestrator loop so newly-stored
            # hub tags are visible to any seeding work in the same run.
            if config.tag_cooccurrence_enabled:
                seen_ws_ctx: set[tuple[str, str]] = set()
                hub_refreshed = 0
                hub_errors = 0
                for row in contexts:
                    workspace_id = str(row[1]) if row[1] else None
                    context_id = str(row[2]) if row[2] else None
                    if not workspace_id or not context_id:
                        continue
                    key = (workspace_id, context_id)
                    if key in seen_ws_ctx:
                        continue
                    seen_ws_ctx.add(key)
                    try:
                        n_hub = await _refresh_hub_tag_cache(
                            db,
                            workspace_id=workspace_id,
                            context_id=context_id,
                            threshold=config.tag_cooccurrence_hub_threshold,
                        )
                        await db.commit()
                        hub_refreshed += 1
                        logger.debug(
                            "hub_tag_cache_refreshed",
                            workspace_id=workspace_id,
                            context_id=context_id,
                            hub_tag_count=n_hub,
                        )
                    except Exception as e:
                        await db.rollback()
                        hub_errors += 1
                        logger.warning(
                            "hub_tag_cache_refresh_failed",
                            workspace_id=workspace_id,
                            context_id=context_id,
                            error=str(e),
                        )
                logger.info(
                    "hub_tag_cache_run_completed",
                    contexts=hub_refreshed,
                    errors=hub_errors,
                )

            for row in contexts:
                user_id = row[0]
                workspace_id = str(row[1]) if row[1] else None
                context_id = str(row[2]) if row[2] else None

                if not workspace_id or not context_id:
                    continue

                try:
                    orchestrator = SleepOrchestrator(db)
                    await orchestrator.run(user_id, workspace_id, context_id, config=config)
                    await db.commit()
                    total_runs += 1
                except Exception as e:
                    await db.rollback()
                    total_errors += 1
                    logger.error(
                        "sleep_maintenance_context_failed",
                        user_id=user_id,
                        context_id=context_id,
                        error=str(e),
                        exc_info=True,
                    )

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
