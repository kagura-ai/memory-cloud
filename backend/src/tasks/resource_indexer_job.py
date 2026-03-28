"""Resource Indexer background job.

Issue #238: APScheduler job for incremental indexing.

Runs every 5 minutes to process queued indexer jobs.
"""

import time
from uuid import UUID

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from db.base import get_db
from db.redis import get_redis_client, increment_counter
from models.resource import IndexerState
from services.resource_indexer import ResourceIndexer
from utils.datetime import utcnow
from utils.logger import get_logger

logger = get_logger(__name__)

# Constraints (Issue #238)
MAX_RUNS_PER_HOUR = 6
MIN_INTERVAL_SECONDS = 600  # 10 minutes


async def can_run_indexer(resource_id: str, context_id: UUID) -> tuple[bool, str]:
    """Check if indexer can run now (rate limiting).

    Constraints:
    - Max 6 runs per hour (token bucket)
    - Min 10 minutes since last run

    Args:
        resource_id: Resource ID
        context_id: Context ID

    Returns:
        Tuple of (can_run, reason)
    """
    redis_client = get_redis_client()
    redis_key = f"indexer:runs:{resource_id}:{context_id}:hour"
    last_run_key = f"indexer:last_run:{resource_id}:{context_id}"

    try:
        # 1. Check hourly quota (max 6 runs/hour)
        runs_this_hour_str = await redis_client.get(redis_key)
        runs_this_hour = int(runs_this_hour_str) if runs_this_hour_str else 0

        if runs_this_hour >= MAX_RUNS_PER_HOUR:
            return False, f"hourly_quota_exceeded ({runs_this_hour}/{MAX_RUNS_PER_HOUR})"

        # 2. Check minimum interval (10 minutes since last run)
        last_run_ts_str = await redis_client.get(last_run_key)
        if last_run_ts_str:
            last_run_ts = float(last_run_ts_str)
            elapsed = time.time() - last_run_ts

            if elapsed < MIN_INTERVAL_SECONDS:
                wait_seconds = MIN_INTERVAL_SECONDS - elapsed
                return False, f"min_interval_not_met (wait {wait_seconds:.0f}s)"

        return True, "ok"

    except Exception as e:
        # Redis errors: fail-open (allow run)
        logger.error("redis_rate_limit_check_failed", error=str(e))
        return True, "redis_error_fail_open"


async def record_indexer_run(resource_id: str, context_id: UUID) -> None:
    """Record indexer run in token bucket.

    Args:
        resource_id: Resource ID
        context_id: Context ID
    """
    redis_client = get_redis_client()
    redis_key = f"indexer:runs:{resource_id}:{context_id}:hour"
    last_run_key = f"indexer:last_run:{resource_id}:{context_id}"

    try:
        # Increment hourly counter
        await increment_counter(redis_key, ttl=3600)  # 1 hour TTL

        # Update last run timestamp
        await redis_client.setex(last_run_key, MIN_INTERVAL_SECONDS * 2, str(time.time()))

        logger.debug(
            "indexer_run_recorded",
            resource_id=resource_id,
            context_id=context_id,
        )

    except Exception as e:
        logger.error("redis_run_record_failed", error=str(e))
        # Don't raise - recording failure shouldn't block indexing


async def run_queued_indexers() -> None:
    """Process all queued indexers (APScheduler job).

    Runs every 5 minutes, processes up to 10 queued indexers per cycle.
    """
    logger.debug("indexer_job_started")

    async for db in get_db():
        try:
            # Find queued indexers ready to run
            result = await db.execute(
                select(IndexerState)
                .where(
                    IndexerState.job_status == "queued",
                    IndexerState.next_run_at <= utcnow(),
                )
                .limit(10)  # Process up to 10 per cycle
            )
            states = list(result.scalars().all())

            if not states:
                logger.debug("indexer_no_queued_jobs")
                return

            logger.info("indexer_processing_queued_jobs", count=len(states))

            for state in states:
                # Check rate limit
                can_run, reason = await can_run_indexer(state.resource_id, state.context_id)

                if not can_run:
                    logger.info(
                        "indexer_skipped",
                        resource_id=state.resource_id,
                        context_id=state.context_id,
                        reason=reason,
                    )
                    continue

                # Mark as running
                state.job_status = "running"
                state.last_run_at = utcnow()
                await db.commit()

                # Run indexer
                try:
                    indexer = ResourceIndexer(db)
                    metrics = await indexer.process_incremental(
                        state.resource_id,
                        state.context_id,
                        batch_size=100,
                    )

                    # Update state
                    state.job_status = "idle"
                    state.metrics = metrics.to_dict()
                    await db.commit()

                    # Record run for rate limiting
                    await record_indexer_run(state.resource_id, state.context_id)

                    logger.info(
                        "indexer_completed",
                        resource_id=state.resource_id,
                        context_id=state.context_id,
                        metrics=metrics.to_dict(),
                    )

                except Exception as e:
                    logger.error(
                        "indexer_failed",
                        resource_id=state.resource_id,
                        context_id=state.context_id,
                        error=str(e),
                    )
                    state.job_status = "failed"
                    state.metrics = {"error": str(e)}
                    await db.commit()

            return  # Exit async for loop

        except Exception as e:
            logger.error("indexer_job_failed", error=str(e))
            return


def schedule_resource_indexer_jobs(scheduler: AsyncIOScheduler) -> None:
    """Add resource indexer job to APScheduler.

    Issue #238: Runs every 5 minutes to process queued indexers.

    Args:
        scheduler: APScheduler instance
    """
    scheduler.add_job(
        run_queued_indexers,
        trigger=IntervalTrigger(minutes=5),
        id="resource_indexer",
        name="Resource Indexer",
        coalesce=True,  # Combine missed runs
        max_instances=1,  # Only one instance at a time
        replace_existing=True,
    )

    logger.info("resource_indexer_job_scheduled", interval="5 minutes")
