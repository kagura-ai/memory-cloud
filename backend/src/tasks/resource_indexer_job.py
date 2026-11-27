"""Resource Indexer background job.

Issue #238: APScheduler job for incremental indexing.

Runs every 5 minutes to process queued indexer jobs. A target with a backlog
is drained batch after batch within a time budget (#3038).
"""

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import exists, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from db.base import get_db
from db.redis import get_redis_client, increment_counter
from models.auth import Context
from models.resource import IndexerState
from services.resource_indexer import (
    _KNOWN_SKIPPED_REASONS,
    IndexerMetrics,
    ResourceIndexer,
    has_pending_events,
    recorded_failed_events,
)
from services.sync_feed_index import STALE_RUNNING_AFTER, maintain_sync_runs
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

# #238: events per ``process_incremental`` call. Each batch is its own
# transaction, quota charge and lock hold; the drain below runs more batches,
# never a larger one.
BATCH_SIZE = 100

# #3038: the drain. After a batch that committed and left events pending, the
# job runs the next batch for the same target, until nothing is pending, a
# batch stops it (held back, a busy lock, failed events, no commit, nothing
# applied) or a time budget is used.
#
# Per target: a backlog of thousands of events is applied a few hundred at a
# time per tick while other targets still get their turn. A batch of 100 takes
# well under a minute, so a drain runs several batches.
DRAIN_TARGET_BUDGET_SECONDS = 180.0
# Per tick, counted from its start (sync maintenance included): a target that
# could not start before this is used stays queued for the next tick. It leaves
# 90 s of the 5-minute interval as a margin, so the tick ends before the next
# one is due (max_instances=1 would skip that one).
#
# Both budgets are checked before each batch against the longest batch of the
# tick so far: a batch starts only when one that long still ends inside the
# budget, so slow batches (slow embeddings) shorten the drain instead of running
# the tick into the next one.
DRAIN_TICK_BUDGET_SECONDS = 210.0
# A pause between two batches of a drain: the batch released its locks at its
# commit, and a status poll or a feed transition waiting on them takes them now
# instead of meeting the next batch's.
DRAIN_YIELD_SECONDS = 1.0
# The ``indexer_state.metrics`` key a drain stopped by a time budget leaves
# (see ``continues_drain``). The next batch's commit replaces the metrics, so
# it admits one run.
DRAIN_CONTINUES_KEY = "drain_continues"
# Stops that leave events pending only because the time ran out.
BUDGET_STOPS = frozenset({"target_budget", "tick_budget"})
# Stops after which a target with events still pending is queued again as an
# ordinary run (``drained`` too: events can arrive while the row is
# ``running``). A hold keeps its own re-queue time; a skipped batch (no schema,
# no context, no resource entity) cannot progress by retrying and waits for
# the next ingest as before.
RETRY_STOPS = frozenset(
    {"drained", "lock_busy", "event_failures", "not_committed", "nothing_applied", "error"}
)

DrainStop = Literal[
    "drained",
    "held_back",
    "skipped",
    "error",
    "not_committed",
    "lock_busy",
    "event_failures",
    "nothing_applied",
    "target_budget",
    "tick_budget",
]


def drain_clock() -> float:
    """The clock the drain's budgets are measured on (monotonic seconds)."""
    return time.monotonic()


def batch_stop(metrics: IndexerMetrics) -> DrainStop | None:
    """Why the drain must not run another batch after this one, or ``None``.

    A batch that is held back (daily quota, memory limit, plan) or skipped
    ends the drain with the reason and re-queue time a single run has. So does
    a batch that did not commit, stopped at a busy lock or applied nothing.
    A batch that recorded failed events ends it too: the failures may be a
    provider outage or the spend cap, which more batches would only hit again.
    """
    if metrics.reason == "memories_per_day_exceeded" or metrics.reason in HELD_BACK_REASONS:
        return "held_back"
    if metrics.skipped:
        return "skipped"
    if metrics.reason is not None:
        return "error"
    if not metrics.batch_committed:
        return "not_committed"
    if metrics.stopped_at_busy_lock:
        return "lock_busy"
    if metrics.errors:
        return "event_failures"
    if not metrics.applied_upserts + metrics.applied_deletes:
        return "nothing_applied"
    return None


async def continues_drain(db: AsyncSession, state: IndexerState) -> bool:
    """Whether this run continues a drain the rate limit must not refuse (#3038).

    Invariant: a run skips the per-target interval and hourly cap only when
    the last run of the target ended on a time budget with events pending
    (it left ``DRAIN_CONTINUES_KEY``) and events are still pending now. The
    key admits one run: that run's batch commit replaces the metrics, and only
    another budget stop with events pending sets it again. Every continuation
    applies at least one batch, so the exemption lasts while the backlog does.
    A target with nothing pending is limited as before.
    """
    metrics = state.metrics
    if not isinstance(metrics, dict) or metrics.get(DRAIN_CONTINUES_KEY) is not True:
        return False
    return await has_pending_events(db, state.id)


@dataclass
class TickBudget:
    """The tick's clock: when it started and its longest batch so far (#3038)."""

    started: float
    longest_batch: float = 0.0

    def room_for_a_batch(self, since: float, budget: float) -> bool:
        """Whether a batch as long as the longest so far ends within ``budget`` from ``since``."""
        elapsed = drain_clock() - since
        return elapsed < budget and elapsed + self.longest_batch <= budget


@dataclass
class Drain:
    """What a drain did so far: the last batch, the failure record, the counts.

    ``applied_upserts``, ``applied_deletes`` and ``errors`` add up the batches:
    a drain is one run, and its stored metrics count all of them.
    """

    failed_events: list[dict[str, Any]]
    metrics: IndexerMetrics | None = None
    batches: int = 0
    applied_upserts: int = 0
    applied_deletes: int = 0
    errors: int = 0
    stop: DrainStop | None = None

    @property
    def events(self) -> int:
        return self.applied_upserts + self.applied_deletes + self.errors

    def stored_metrics(self) -> dict[str, Any]:
        """The last batch's metrics, with the drain's counts in place of its own
        when it ran more than one batch."""
        assert self.metrics is not None, "a drain runs at least one batch"
        if self.batches == 1:
            return self.metrics.to_dict()
        return {
            **self.metrics.to_dict(),
            "applied_upserts": self.applied_upserts,
            "applied_deletes": self.applied_deletes,
            "errors": self.errors,
        }


async def drain_target(
    db: AsyncSession,
    state: IndexerState,
    drain: Drain,
    *,
    tick: TickBudget,
) -> None:
    """Run batches for one target until ``batch_stop``, nothing pending or a budget (#3038).

    Each batch is one ``process_incremental`` call with its own commit, quota
    charge and lock release. ``drain`` is updated after every batch, so the
    caller still has the record of the last committed batch if a later call
    raises.
    """
    started = drain_clock()
    resource_id, context_id, state_id = state.resource_id, state.context_id, state.id
    indexer = ResourceIndexer(db)
    while True:
        batch_started = drain_clock()
        metrics = await indexer.process_incremental(
            resource_id,
            context_id,
            batch_size=BATCH_SIZE,
            failed_events=drain.failed_events,
        )
        tick.longest_batch = max(tick.longest_batch, drain_clock() - batch_started)
        drain.metrics = metrics
        drain.batches += 1
        drain.failed_events = metrics.failed_events
        drain.applied_upserts += metrics.applied_upserts
        drain.applied_deletes += metrics.applied_deletes
        drain.errors += metrics.errors
        stop = batch_stop(metrics)
        if stop is None and not await has_pending_events(db, state_id):
            stop = "drained"
        if stop is None and not tick.room_for_a_batch(tick.started, DRAIN_TICK_BUDGET_SECONDS):
            stop = "tick_budget"
        if stop is None and not tick.room_for_a_batch(started, DRAIN_TARGET_BUDGET_SECONDS):
            stop = "target_budget"
        if stop is not None:
            drain.stop = stop
            return
        await asyncio.sleep(DRAIN_YIELD_SECONDS)


async def requeue_stale_running(db: AsyncSession) -> int:
    """Queue again the targets a stopped process left ``running`` (#3038).

    The job marks a target ``running`` and every batch commit refreshes its
    ``last_run_at``, so a target whose last batch is older than
    ``STALE_RUNNING_AFTER`` has no run in progress: the process ended mid-run
    (a restart during a drain). Nothing else moves such a row (an ingest queues
    only an ``idle`` row), so it is queued here, due now. A row another
    transaction holds is skipped, not waited on. Returns how many were queued.
    """
    stale = (
        select(IndexerState.id)
        .where(
            IndexerState.job_status == "running",
            or_(
                IndexerState.last_run_at.is_(None),
                IndexerState.last_run_at <= utcnow() - STALE_RUNNING_AFTER,
            ),
        )
        .order_by(IndexerState.id)
        .limit(100)
        .with_for_update(skip_locked=True)
    )
    result = await db.execute(
        update(IndexerState)
        .where(IndexerState.id.in_(stale.scalar_subquery()))
        .values(job_status="queued", next_run_at=utcnow())
        .returning(IndexerState.id)
        .execution_options(synchronize_session=False)
    )
    requeued = result.scalars().all()
    await db.commit()
    if requeued:
        logger.warning("indexer_stale_running_requeued", count=len(requeued))
    return len(requeued)


def _logged_reason(reason: str | None) -> str | None:
    """A batch's reason for the log: a known skip reason, ``error`` for any other text."""
    if reason is None or reason in _KNOWN_SKIPPED_REASONS:
        return reason
    return "error"


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


def rate_limited_retry_at(
    now: datetime, wait_seconds: float, *, cap: timedelta | None = RATE_LIMITED_RETRY
) -> datetime:
    """``next_run_at`` of a row the rate limit refused (#3009).

    ``now`` is the process clock (``utcnow()``), the clock the pick query of
    the next tick compares ``next_run_at`` with. The row is due no later than
    the moment the limit admits it, at most ``cap`` ahead, and never before
    ``now``. ``cap=None`` (the hourly cap) waits the whole time: a row whose
    hour counter has 45 minutes left is not picked and refused at every tick
    in between, where it would take one of the pick's slots each time.
    """
    retry = timedelta(seconds=max(wait_seconds, 0.0))
    if cap is not None:
        retry = min(retry, cap)
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
            # A failed TTL read keeps the refusal (it must not fail open past
            # the cap) and waits the plain retry too.
            try:
                ttl_ms = await redis_client.pttl(redis_key)
            except Exception as e:
                logger.warning("redis_rate_limit_ttl_failed", error=str(e))
                ttl_ms = -1
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


async def record_indexer_run(
    resource_id: str, context_id: UUID, *, count_hour: bool = True
) -> None:
    """Record indexer run in token bucket.

    Args:
        resource_id: Resource ID
        context_id: Context ID
        count_hour: Add the run to the hourly counter. ``False`` for a tick
            that continues a drain (#3038): the drain counted once when it
            started, and only the last-run timestamp moves, so the interval
            counts from the drain's end.
    """
    redis_client = get_redis_client()
    redis_key = f"indexer:runs:{resource_id}:{context_id}:hour"
    last_run_key = f"indexer:last_run:{resource_id}:{context_id}"

    try:
        # Increment hourly counter
        if count_hour:
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

    Runs every 5 minutes, picks up to 10 queued indexers per cycle and drains
    each in turn (#3038) while the tick budget lasts.
    """
    logger.debug("indexer_job_started")
    tick = TickBudget(started=drain_clock())

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
            try:
                await requeue_stale_running(db)
            except Exception as e:
                await db.rollback()
                logger.error(
                    "indexer_stale_recovery_failed",
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
                return {"picked": 0, "deferred": 0, "failed": 0, "rate_limited": 0}

            logger.info("indexer_processing_queued_jobs", count=len(states))
            failed = rate_limited = deferred = 0

            for index, state in enumerate(states):
                # #3038: the rows not started once the tick budget is used (or
                # has no room for one more batch as long as the longest of the
                # tick) stay queued as they are, ahead of later rows at the
                # next pick.
                if not tick.room_for_a_batch(tick.started, DRAIN_TICK_BUDGET_SECONDS):
                    deferred = len(states) - index
                    logger.info("indexer_tick_budget_used", deferred=deferred)
                    break

                # The row was read at the pick: an operator rewind may have
                # landed since (a stale last_offset would write over it), and a
                # rollback in the previous row's run expired it.
                await db.refresh(state)
                # Read once here: a rollback below expires the row, and its
                # attributes cannot be loaded lazily then.
                resource_id, context_id = state.resource_id, state.context_id

                # Check rate limit; a drain cut short by its time budget goes on.
                continuation = await continues_drain(db, state)
                if continuation:
                    verdict = IndexerRateLimit(True, "drain_continues")
                else:
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
                    state.next_run_at = rate_limited_retry_at(
                        utcnow(),
                        wait_seconds,
                        cap=None if verdict.limit == "hourly_cap" else RATE_LIMITED_RETRY,
                    )
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
                drain = Drain(failed_events=recorded_failed_events(state.metrics))
                drain_started = drain_clock()

                # Run indexer
                try:
                    await drain_target(db, state, drain, tick=tick)
                    metrics = drain.metrics
                    assert metrics is not None, "a drain runs at least one batch"

                    # Update state. Only an ingest event re-queues an idle row,
                    # so a batch the #1549 daily memory quota refused (nothing
                    # applied, offset unchanged) is re-queued here for the UTC
                    # reset instead of parking until the next event arrives.
                    new_metrics = drain.stored_metrics()
                    if drain.stop in BUDGET_STOPS:
                        # #3038: events are pending and only the time ran out:
                        # due at the next tick, which continues the drain.
                        state.job_status = "queued"
                        state.next_run_at = utcnow()
                        new_metrics[DRAIN_CONTINUES_KEY] = True
                    elif metrics.reason == "memories_per_day_exceeded":
                        state.job_status = "queued"
                        state.next_run_at = _next_utc_midnight()
                    elif metrics.reason in HELD_BACK_REASONS:
                        # #1939: same "applied nothing, keep the events" shape.
                        state.job_status = "queued"
                        state.next_run_at = utcnow() + HELD_BACK_RETRY_BY_REASON.get(
                            metrics.reason, HELD_BACK_RETRY
                        )
                    elif drain.stop in RETRY_STOPS and await has_pending_events(db, state.id):
                        # #3038: events are still pending after a stop that
                        # is not a hold (a busy lock, failed events, a batch
                        # that did not commit or applied nothing), or arrived
                        # while the row was ``running`` (an ingest queues only
                        # an ``idle`` row). Queued as an ordinary run, without
                        # the continuation key: the per-target interval and
                        # hourly cap pace it, so a batch that fails every time
                        # runs at most once per MIN_INTERVAL_SECONDS and
                        # MAX_RUNS_PER_HOUR times an hour, never in a loop.
                        state.job_status = "queued"
                        state.next_run_at = utcnow()
                    else:
                        state.job_status = "idle"
                    state.metrics = new_metrics
                    await db.commit()

                    # Record run for rate limiting: a drain is one run, counted
                    # in the hour when it starts; a continuation only moves
                    # the last-run timestamp.
                    await record_indexer_run(resource_id, context_id, count_hour=not continuation)

                    # One line per drain (#3038): counts and known reasons
                    # only, no error text.
                    logger.info(
                        "indexer_completed",
                        resource_id=resource_id,
                        context_id=context_id,
                        batches=drain.batches,
                        events=drain.events,
                        duration_ms=int((drain_clock() - drain_started) * 1000),
                        stop=drain.stop,
                        reason=_logged_reason(metrics.reason),
                        applied_upserts=drain.applied_upserts,
                        applied_deletes=drain.applied_deletes,
                        errors=drain.errors,
                        failed_events=len(drain.failed_events),
                    )

                except Exception as e:
                    logger.error(
                        "indexer_failed",
                        resource_id=resource_id,
                        context_id=context_id,
                        error=str(e),
                    )
                    failed += 1
                    # A failed statement leaves the transaction aborted, and
                    # the failed state below could not commit in it. What the
                    # drain's batches committed stays; ``drain.failed_events``
                    # is the record of the last of them.
                    await db.rollback()
                    state.job_status = "failed"
                    error_metrics: dict[str, Any] = {"error": str(e)}
                    if drain.failed_events:
                        error_metrics["failed_events"] = drain.failed_events
                    state.metrics = error_metrics
                    await db.commit()

            # Exit async for loop
            # ``picked``: the rows this tick took on (run or refused by the
            # rate limit); ``deferred``: the rows the tick budget left queued.
            return {
                "picked": len(states) - deferred,
                "deferred": deferred,
                "failed": failed,
                "rate_limited": rate_limited,
            }

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
