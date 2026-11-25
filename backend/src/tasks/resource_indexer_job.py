"""Resource Indexer background job.

Issue #238: APScheduler job for incremental indexing.

Runs every 5 minutes to process queued indexer jobs.
"""

import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import exists, select

from db.base import get_db
from db.redis import get_redis_client, increment_counter
from models.auth import Context
from models.resource import IndexerState
from services.resource_indexer import ResourceIndexer, recorded_failed_events
from services.sync_feed_index import maintain_sync_runs
from services.workspace_slot_gate import active_workspace_predicate
from tasks.job_runs import JobResult, job_failed
from utils.datetime import utcnow
from utils.logger import get_logger

logger = get_logger(__name__)

# Constraints (Issue #238)
MAX_RUNS_PER_HOUR = 6
MIN_INTERVAL_SECONDS = 600  # 10 minutes
# #3009: the last-run timestamp is taken when a run finishes, and the scheduler
# fires every 5 minutes, so the tick two periods after a run sees an elapsed
# time short of MIN_INTERVAL_SECONDS by the run's duration. A run is admitted
# once MIN_INTERVAL_SECONDS minus this tolerance has passed. The hourly cap is
# checked first and on its own, so the tolerance never admits more runs per
# hour than MAX_RUNS_PER_HOUR.
MIN_INTERVAL_TOLERANCE_SECONDS = 60


def _next_utc_midnight() -> datetime:
    """When the #1549 daily memory-creation counter resets (naive UTC, like
    ``IndexerState.next_run_at``)."""
    return datetime.combine(utcnow().date() + timedelta(days=1), datetime.min.time())


# #1939: how long a batch held back by a plan suspension or the workspace
# memory limit waits before the next attempt. Neither has a known reset time
# (re-subscribing or deleting memories lifts them), so the row is retried on a
# slow cadence instead of every 5-minute cycle.
HELD_BACK_RETRY = timedelta(hours=1)
HELD_BACK_REASONS = frozenset({"plan_suspended", "memory_limit_exceeded"})
# A plan pause lifts only on a plan change, so its rows retry daily rather than
# hourly: they cannot progress before then, and each retry spends a queue slot.
HELD_BACK_RETRY_BY_REASON = {"plan_suspended": timedelta(hours=24)}
# A row the per-resource rate limit refuses moves behind the rows that can run
# now, so a burst of re-queued rows cannot hold every slot of the
# longest-waiting-first pick cycle after cycle.
RATE_LIMITED_RETRY = timedelta(minutes=5)
# #3009: a row the rate limit refuses is rescheduled this much before the
# moment the limit admits it, so the first tick at or after that moment picks it
# even when the skip was written a few seconds into its tick.
RATE_LIMITED_RETRY_SLACK = timedelta(seconds=30)


@dataclass(frozen=True)
class IndexerRateLimit:
    """The rate-limit verdict for one resource (#3009).

    ``limit`` names the limit that refused the run (``hourly_cap`` or
    ``min_interval``) and ``wait_seconds`` the seconds until it admits it;
    both are ``None`` when the run is allowed.
    """

    allowed: bool
    reason: str
    limit: Literal["hourly_cap", "min_interval"] | None = None
    wait_seconds: float | None = None


def rate_limited_retry_at(now: datetime, wait_seconds: float) -> datetime:
    """``next_run_at`` of a row the rate limit refused (#3009).

    ``now`` is the process clock (``utcnow()``), the clock the pick query of
    the next tick compares ``next_run_at`` with. The row is due no later than
    the moment the limit admits it, at most ``RATE_LIMITED_RETRY`` ahead, and
    never before ``now``.
    """
    retry = min(timedelta(seconds=max(wait_seconds, 0.0)), RATE_LIMITED_RETRY)
    return max(now, now + retry - RATE_LIMITED_RETRY_SLACK)


async def check_indexer_rate_limit(resource_id: str, context_id: UUID) -> IndexerRateLimit:
    """Check if indexer can run now (rate limiting).

    Constraints:
    - Max 6 runs per hour (token bucket), checked first
    - Min 10 minutes since the last run finished, less
      ``MIN_INTERVAL_TOLERANCE_SECONDS``

    Redis errors fail open (the run is allowed).
    """
    redis_client = get_redis_client()
    redis_key = f"indexer:runs:{resource_id}:{context_id}:hour"
    last_run_key = f"indexer:last_run:{resource_id}:{context_id}"

    try:
        # 1. Check hourly quota (max 6 runs/hour)
        runs_this_hour_str = await redis_client.get(redis_key)
        runs_this_hour = int(runs_this_hour_str) if runs_this_hour_str else 0

        if runs_this_hour >= MAX_RUNS_PER_HOUR:
            # The cap admits again when the hour counter expires. A missing key
            # (-2) or one without a TTL (-1) waits the plain retry.
            ttl_ms = await redis_client.pttl(redis_key)
            wait_seconds = ttl_ms / 1000 if ttl_ms > 0 else RATE_LIMITED_RETRY.total_seconds()
            return IndexerRateLimit(
                False,
                f"hourly_quota_exceeded ({runs_this_hour}/{MAX_RUNS_PER_HOUR})",
                limit="hourly_cap",
                wait_seconds=wait_seconds,
            )

        # 2. Check minimum interval since the last run finished
        last_run_ts_str = await redis_client.get(last_run_key)
        if last_run_ts_str:
            last_run_ts = float(last_run_ts_str)
            elapsed = time.time() - last_run_ts
            required = MIN_INTERVAL_SECONDS - MIN_INTERVAL_TOLERANCE_SECONDS

            if elapsed < required:
                wait_seconds = required - elapsed
                return IndexerRateLimit(
                    False,
                    f"min_interval_not_met (wait {wait_seconds:.0f}s)",
                    limit="min_interval",
                    wait_seconds=wait_seconds,
                )

        return IndexerRateLimit(True, "ok")

    except Exception as e:
        # Redis errors: fail-open (allow run)
        logger.error("redis_rate_limit_check_failed", error=str(e))
        return IndexerRateLimit(True, "redis_error_fail_open")


async def can_run_indexer(resource_id: str, context_id: UUID) -> tuple[bool, str]:
    """``check_indexer_rate_limit`` as ``(can_run, reason)``."""
    verdict = await check_indexer_rate_limit(resource_id, context_id)
    return verdict.allowed, verdict.reason


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


async def run_queued_indexers() -> JobResult:
    """Process all queued indexers (APScheduler job).

    Runs every 5 minutes, processes up to 10 queued indexers per cycle.
    """
    logger.debug("indexer_job_started")

    async for db in get_db():
        try:
            # #2901: sync maintenance is a step of its own. A failure there is
            # rolled back and logged, and the tick still runs the queued
            # indexers of every workspace.
            try:
                await maintain_sync_runs(db)
            except Exception as e:
                await db.rollback()
                logger.error(
                    "indexer_sync_maintenance_failed",
                    error_type=type(e).__name__,
                    error=str(e),
                )
            # Find queued indexers ready to run
            pick = select(IndexerState).where(
                IndexerState.job_status == "queued",
                IndexerState.next_run_at <= utcnow(),
            )
            # #2918: a slot-suspended workspace's rows stay queued, unpicked,
            # until it is resumed (nothing is added unless the mode is enforce).
            slot_active = active_workspace_predicate(Context.workspace_id)
            if slot_active is not None:
                pick = pick.where(
                    exists().where(Context.id == IndexerState.context_id, slot_active)
                )
            # Longest-waiting first (#1939): rows held back by a plan
            # suspension or the memory limit re-queue hourly and must not
            # crowd runnable rows out of the 10-row pick. id breaks ties.
            # Process up to 10 per cycle.
            result = await db.execute(
                pick.order_by(IndexerState.next_run_at.asc(), IndexerState.id.asc()).limit(10)
            )
            states = list(result.scalars().all())

            if not states:
                logger.debug("indexer_no_queued_jobs")
                return {"picked": 0, "failed": 0, "rate_limited": 0}

            logger.info("indexer_processing_queued_jobs", count=len(states))
            failed = rate_limited = 0

            for state in states:
                # The row was read at the pick: an operator rewind may have
                # landed since (a stale last_offset would write over it), and a
                # rollback in the previous row's run expired it.
                await db.refresh(state)

                # Check rate limit
                verdict = await check_indexer_rate_limit(state.resource_id, state.context_id)

                if not verdict.allowed:
                    wait_seconds = (
                        verdict.wait_seconds
                        if verdict.wait_seconds is not None
                        else RATE_LIMITED_RETRY.total_seconds()
                    )
                    logger.info(
                        "indexer_waiting_for_interval"
                        if verdict.limit == "min_interval"
                        else "indexer_skipped",
                        resource_id=state.resource_id,
                        context_id=state.context_id,
                        reason=verdict.reason,
                        wait_seconds=round(wait_seconds),
                    )
                    # #3009: due by the moment the limit admits it, so the
                    # first tick from then on runs it.
                    state.next_run_at = rate_limited_retry_at(utcnow(), wait_seconds)
                    await db.commit()
                    rate_limited += 1
                    continue

                # Mark as running
                state.job_status = "running"
                state.last_run_at = utcnow()
                await db.commit()
                # #2477: the runbook's rewind and clear refuse a running row,
                # so one that landed since the refresh above is the last
                # operator write the run can meet: start from it, not from the
                # copy that refresh read, or the run writes the copy back.
                await db.refresh(state)

                # #2254: state.metrics is replaced below on every path, and the
                # skipped events it names are the rewind target. #2477: the run
                # takes the record, so its batch commit already holds it merged
                # with the batch's outcome, and returns it on every path.
                failed_events = recorded_failed_events(state.metrics)

                # Run indexer
                try:
                    indexer = ResourceIndexer(db)
                    metrics = await indexer.process_incremental(
                        state.resource_id,
                        state.context_id,
                        batch_size=100,
                        failed_events=failed_events,
                    )

                    # Update state. Only an ingest event re-queues an idle row,
                    # so a batch the #1549 daily memory quota refused (nothing
                    # applied, offset unchanged) is re-queued here for the UTC
                    # reset instead of parking until the next event arrives.
                    if metrics.reason == "memories_per_day_exceeded":
                        state.job_status = "queued"
                        state.next_run_at = _next_utc_midnight()
                    elif metrics.reason in HELD_BACK_REASONS:
                        # #1939: same "applied nothing, keep the events" shape.
                        state.job_status = "queued"
                        state.next_run_at = utcnow() + HELD_BACK_RETRY_BY_REASON.get(
                            metrics.reason, HELD_BACK_RETRY
                        )
                    else:
                        state.job_status = "idle"
                    new_metrics = metrics.to_dict()
                    state.metrics = new_metrics
                    failed_events = new_metrics.get("failed_events", failed_events)
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
                    failed += 1
                    state.job_status = "failed"
                    error_metrics: dict[str, Any] = {"error": str(e)}
                    if failed_events:
                        error_metrics["failed_events"] = failed_events
                    state.metrics = error_metrics
                    await db.commit()

            # Exit async for loop
            return {"picked": len(states), "failed": failed, "rate_limited": rate_limited}

        except Exception as e:
            logger.error("indexer_job_failed", error=str(e))
            return job_failed(e)
    return None


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
