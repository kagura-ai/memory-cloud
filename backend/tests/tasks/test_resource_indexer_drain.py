"""#3038: the resource indexer job drains a backlog within a time budget.

The job body runs against the real ``db_session`` (the pick, the row
transitions and the commits are real); the indexer, the pending check, the
drain clock and Redis are doubles, so a test chooses how many events are
pending, how long a batch takes and what each batch returns.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import numpy  # noqa: F401  isort: skip  # load-bearing: see test_resource_indexer_job_coverage
import pydantic.root_model  # noqa: F401  isort: skip
import pytest
import pytest_asyncio
import structlog
from sqlalchemy import update

from models.auth import Context
from models.resource import IndexerState
from services.resource_indexer import IndexerMetrics
from tasks import resource_indexer_job as job
from tasks.resource_indexer_job import (
    DRAIN_CONTINUES_KEY,
    DRAIN_TARGET_BUDGET_SECONDS,
    DRAIN_TICK_BUDGET_SECONDS,
    HELD_BACK_RETRY,
    MAX_RUNS_PER_HOUR,
    MIN_INTERVAL_SECONDS,
    run_queued_indexers,
)
from tests.tasks.test_resource_indexer_job_coverage import (  # noqa: F401  (fixture)
    _mock_get_db,
    seeded_state,
)
from utils.datetime import utcnow


def test_the_budgets_fit_the_five_minute_tick():
    """The tick budget leaves room for the last batch inside the 5-minute
    interval (max_instances=1 would skip the next tick otherwise), and a
    target's budget fits in it."""
    assert DRAIN_TARGET_BUDGET_SECONDS <= DRAIN_TICK_BUDGET_SECONDS < 5 * 60
    assert 5 * 60 - DRAIN_TICK_BUDGET_SECONDS >= 60


class _Targets:
    """Pending events per target and the clock the drain reads.

    ``process_incremental`` applies up to ``batch_size`` pending events of the
    target it is called for, moves the clock on by ``batch_seconds`` and
    returns what ``script`` says for that call (a committed batch by default).
    """

    def __init__(self, batch_seconds: float = 10.0) -> None:
        self.pending: dict[str, int] = {}
        self.by_state: dict[int, str] = {}
        self.calls: list[str] = []
        self.batch_seconds = batch_seconds
        self.script: dict[tuple[str, int], IndexerMetrics] = {}
        self.t = 1000.0

    def add(self, state: IndexerState, pending: int) -> None:
        self.pending[state.resource_id + str(state.context_id)] = pending
        self.by_state[state.id] = state.resource_id + str(state.context_id)

    def clock(self) -> float:
        return self.t

    async def has_pending(self, _db, state_id: int) -> bool:
        key = self.by_state.get(state_id)
        return key is not None and self.pending[key] > 0

    async def process(self, resource_id, context_id, batch_size=100, *, failed_events=()):
        key = resource_id + str(context_id)
        if key not in self.pending:
            # A row another test left queued.
            return IndexerMetrics(skipped=True, reason="no_pending_events")
        self.calls.append(key)
        self.t += self.batch_seconds
        scripted = self.script.get((key, len([c for c in self.calls if c == key])))
        if scripted is not None:
            applied = scripted.applied_upserts + scripted.applied_deletes
            self.pending[key] -= min(applied, self.pending[key])
            scripted.failed_events = scripted.failed_events or list(failed_events)
            return scripted
        if self.pending[key] == 0:
            return IndexerMetrics(skipped=True, reason="no_pending_events")
        applied = min(batch_size, self.pending[key])
        self.pending[key] -= applied
        return IndexerMetrics(
            applied_upserts=applied, batch_committed=True, failed_events=list(failed_events)
        )

    def batches(self, state: IndexerState) -> int:
        return self.calls.count(state.resource_id + str(state.context_id))


class _Redis:
    """The rate limiter's two keys, set by hand."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key):
        return self.store.get(key)

    async def setex(self, key, _ttl, value):
        self.store[key] = value
        return True

    async def pttl(self, key):
        return 3_000_000 if key in self.store else -2

    async def increment_counter(self, key, ttl=None):
        self.store[key] = str(int(self.store.get(key, "0")) + 1)
        return int(self.store[key])

    def ran_just_now(self, state: IndexerState, runs: int = 1) -> None:
        import time

        self.store[f"indexer:last_run:{state.resource_id}:{state.context_id}"] = str(time.time())
        self.store[f"indexer:runs:{state.resource_id}:{state.context_id}:hour"] = str(runs)


async def _tick(db_session, targets: _Targets, redis: _Redis | None = None, **budgets):
    redis = redis or _Redis()
    indexer = MagicMock()
    indexer.process_incremental = AsyncMock(side_effect=targets.process)
    with (
        patch.object(job, "get_db", _mock_get_db(db_session)),
        patch.object(job, "get_redis_client", return_value=redis),
        patch.object(job, "increment_counter", redis.increment_counter),
        patch.object(job, "ResourceIndexer", MagicMock(return_value=indexer)),
        patch.object(job, "has_pending_events", targets.has_pending),
        patch.object(job, "drain_clock", targets.clock),
        patch.multiple(job, DRAIN_YIELD_SECONDS=0, **budgets),
        structlog.testing.capture_logs() as logs,
    ):
        result = await run_queued_indexers()
    return result, logs


def _completed(logs, state):
    return [
        e
        for e in logs
        if e["event"] == "indexer_completed" and e.get("resource_id") == state.resource_id
    ]


async def _second_target(db_session, first: IndexerState, *, waited: timedelta) -> IndexerState:
    context = await db_session.get(Context, first.context_id)
    other = Context(
        id=uuid.uuid4(),
        workspace_id=context.workspace_id,
        name=f"ctx-{uuid.uuid4().hex[:8]}",
        created_by=context.created_by,
        is_private=False,
    )
    db_session.add(other)
    await db_session.flush()
    second = IndexerState(
        resource_pk=first.resource_pk,
        resource_id=first.resource_id,
        context_id=other.id,
        last_offset=0,
        next_run_at=utcnow() - waited,
        job_status="queued",
    )
    db_session.add(second)
    await db_session.commit()
    return second


@pytest_asyncio.fixture
async def first(db_session, seeded_state):  # noqa: F811
    """The seeded target, due long ago so it is picked before rows other tests left."""
    seeded_state.next_run_at = utcnow() - timedelta(days=1)
    await db_session.commit()
    resource_pk = seeded_state.resource_pk
    yield seeded_state
    # A later test of the job expects no queued row: leave this resource's idle.
    await db_session.rollback()
    await db_session.execute(
        update(IndexerState)
        .where(IndexerState.resource_pk == resource_pk)
        .values(job_status="idle")
    )
    await db_session.commit()


class TestDrain:
    async def test_drains_every_batch_then_stops_on_empty(self, db_session, first):
        targets = _Targets()
        targets.add(first, 250)

        _result, logs = await _tick(db_session, targets)

        assert targets.batches(first) == 3
        assert targets.pending[first.resource_id + str(first.context_id)] == 0
        await db_session.refresh(first)
        assert first.job_status == "idle"
        assert DRAIN_CONTINUES_KEY not in first.metrics
        # A drain is one run: its batches' counts are stored together.
        assert first.metrics["applied_upserts"] == 250
        assert first.metrics["applied_deletes"] == 0
        [line] = _completed(logs, first)
        assert line["batches"] == 3
        assert line["events"] == 250
        assert line["stop"] == "drained"
        assert isinstance(line["duration_ms"], int)
        assert "metrics" not in line

    async def test_a_drain_counts_as_one_run(self, db_session, first):
        targets = _Targets()
        targets.add(first, 250)
        record = AsyncMock()

        with patch.object(job, "record_indexer_run", record):
            await _tick(db_session, targets)

        record.assert_awaited_once_with(first.resource_id, first.context_id, count_hour=True)

    async def test_each_batch_gets_the_record_the_last_one_returned(self, db_session, first):
        failed = {"event_id": 3, "doc_id": "d3", "error_class": "RuntimeError"}
        targets = _Targets()
        targets.add(first, 150)
        given: list[list] = []
        process = targets.process

        async def _watch(*a, failed_events=(), **k):
            given.append(list(failed_events))
            metrics = await process(*a, failed_events=failed_events, **k)
            if len(given) == 1:
                metrics.failed_events = [failed]
            return metrics

        targets.process = _watch
        await _tick(db_session, targets)

        assert given == [[], [failed]]

    @pytest.mark.parametrize(
        ("metrics", "status", "due", "stop"),
        [
            (
                IndexerMetrics(skipped=True, reason="memories_per_day_exceeded"),
                "queued",
                "midnight",
                "held_back",
            ),
            (
                # A partial batch: the prefix that fits the memory limit applied.
                IndexerMetrics(
                    applied_upserts=40, reason="memory_limit_exceeded", batch_committed=True
                ),
                "queued",
                "hour",
                "held_back",
            ),
            (
                IndexerMetrics(skipped=True, reason="plan_suspended"),
                "queued",
                "day",
                "held_back",
            ),
            (
                IndexerMetrics(applied_upserts=5, batch_committed=True, stopped_at_busy_lock=True),
                "idle",
                None,
                "lock_busy",
            ),
            (IndexerMetrics(errors=1, reason="commit failed"), "idle", None, "error"),
            (IndexerMetrics(), "idle", None, "not_committed"),
            (
                IndexerMetrics(
                    applied_upserts=99,
                    errors=1,
                    batch_committed=True,
                    failed_events=[{"event_id": 150, "doc_id": "d", "error_class": "QdrantError"}],
                ),
                "idle",
                None,
                "event_failures",
            ),
            (IndexerMetrics(batch_committed=True), "idle", None, "nothing_applied"),
        ],
        ids=[
            "daily-quota",
            "memory-limit",
            "plan",
            "lock-busy",
            "rolled-back",
            "not-committed",
            "failed-events",
            "nothing-applied",
        ],
    )
    async def test_a_stop_reason_ends_the_drain_as_a_single_run_ends_today(
        self, db_session, first, metrics, status, due, stop
    ):
        targets = _Targets()
        targets.add(first, 500)
        key = first.resource_id + str(first.context_id)
        targets.script[(key, 2)] = metrics
        before = utcnow()

        _result, logs = await _tick(db_session, targets)

        assert targets.batches(first) == 2
        await db_session.refresh(first)
        assert first.job_status == status
        assert DRAIN_CONTINUES_KEY not in first.metrics
        if due == "midnight":
            assert first.next_run_at == datetime.combine(
                utcnow().date() + timedelta(days=1), datetime.min.time()
            )
        elif due == "hour":
            assert before + HELD_BACK_RETRY - timedelta(minutes=1) <= first.next_run_at
            assert first.next_run_at <= utcnow() + HELD_BACK_RETRY
        elif due == "day":
            assert before + timedelta(hours=23) <= first.next_run_at
        [line] = _completed(logs, first)
        assert line["stop"] == stop
        assert line["batches"] == 2

    async def test_a_run_with_nothing_pending_is_one_batch(self, db_session, first):
        targets = _Targets()
        targets.add(first, 0)

        _result, logs = await _tick(db_session, targets)

        assert targets.batches(first) == 1
        await db_session.refresh(first)
        assert first.job_status == "idle"
        assert _completed(logs, first)[0]["stop"] == "skipped"


class TestTimeBudget:
    async def test_the_target_budget_leaves_it_due_at_the_next_tick(self, db_session, first):
        targets = _Targets(batch_seconds=65)
        targets.add(first, 1000)
        before = utcnow()

        _result, logs = await _tick(db_session, targets)

        # 65 s, 130 s: a third batch would end at 195 s > 180 s: stop.
        assert targets.batches(first) == 2
        await db_session.refresh(first)
        assert first.job_status == "queued"
        assert before <= first.next_run_at <= utcnow()
        assert first.metrics[DRAIN_CONTINUES_KEY] is True
        assert _completed(logs, first)[0]["stop"] == "target_budget"

    async def test_the_continuation_is_admitted_inside_the_interval(self, db_session, first):
        targets = _Targets(batch_seconds=65)
        targets.add(first, 1000)
        redis = _Redis()
        await _tick(db_session, targets, redis)
        # The drain recorded its run: the interval would refuse the row now.
        assert redis.store[f"indexer:last_run:{first.resource_id}:{first.context_id}"]

        _result, logs = await _tick(db_session, targets, redis)

        assert targets.batches(first) == 4
        assert not [e for e in logs if e["event"] == "indexer_waiting_for_interval"]

    async def test_the_continuation_is_admitted_past_the_hourly_cap(self, db_session, first):
        targets = _Targets(batch_seconds=65)
        targets.add(first, 1000)
        redis = _Redis()
        await _tick(db_session, targets, redis)
        redis.ran_just_now(first, runs=MAX_RUNS_PER_HOUR)

        await _tick(db_session, targets, redis)

        assert targets.batches(first) == 4

    async def test_the_continuation_ends_when_the_backlog_is_applied(self, db_session, first):
        targets = _Targets(batch_seconds=65)
        targets.add(first, 400)
        redis = _Redis()
        await _tick(db_session, targets, redis)
        await _tick(db_session, targets, redis)

        await db_session.refresh(first)
        assert first.job_status == "idle"
        assert DRAIN_CONTINUES_KEY not in first.metrics
        # Nothing pending: the next queued run waits for the interval again.
        first.job_status, first.next_run_at = "queued", utcnow() - timedelta(days=1)
        await db_session.commit()
        targets.pending[first.resource_id + str(first.context_id)] = 5
        _result, logs = await _tick(db_session, targets, redis)
        assert targets.batches(first) == 4
        assert [e["event"] for e in logs if e.get("resource_id") == first.resource_id] == [
            "indexer_waiting_for_interval"
        ]

    async def test_a_marked_target_with_nothing_pending_is_limited(self, db_session, first):
        """The exemption holds only while events remain."""
        first.metrics = {DRAIN_CONTINUES_KEY: True}
        await db_session.commit()
        targets = _Targets()
        targets.add(first, 0)
        redis = _Redis()
        redis.ran_just_now(first)

        _result, logs = await _tick(db_session, targets, redis)

        assert targets.batches(first) == 0
        await db_session.refresh(first)
        assert first.job_status == "queued"
        assert first.next_run_at > utcnow()
        waiting = [e for e in logs if e["event"] == "indexer_waiting_for_interval"]
        assert [e["resource_id"] for e in waiting] == [first.resource_id]

    async def test_an_unmarked_target_with_events_pending_is_limited(self, db_session, first):
        """A backlog alone is no exemption: only a drain that stopped on its budget."""
        targets = _Targets()
        targets.add(first, 500)
        redis = _Redis()
        redis.ran_just_now(first, runs=MAX_RUNS_PER_HOUR)

        _result, logs = await _tick(db_session, targets, redis)

        assert targets.batches(first) == 0
        skipped = [e for e in logs if e.get("resource_id") == first.resource_id]
        assert [e["event"] for e in skipped] == ["indexer_skipped"]
        assert skipped[0]["reason"].startswith("hourly_quota_exceeded")

    async def test_min_interval_constant_is_unchanged(self):
        assert MIN_INTERVAL_SECONDS == 600
        assert MAX_RUNS_PER_HOUR == 6


class TestFairness:
    async def test_a_second_target_is_served_in_the_same_tick(self, db_session, first):
        second = await _second_target(db_session, first, waited=timedelta(hours=12))
        targets = _Targets(batch_seconds=65)
        targets.add(first, 1000)
        targets.add(second, 100)

        await _tick(db_session, targets)

        assert targets.batches(first) == 2
        assert targets.batches(second) == 1
        await db_session.refresh(second)
        assert second.job_status == "idle"

    async def test_the_tick_budget_leaves_a_target_queued(self, db_session, first):
        second = await _second_target(db_session, first, waited=timedelta(hours=12))
        due = second.next_run_at
        targets = _Targets(batch_seconds=70)
        targets.add(first, 1000)
        targets.add(second, 100)

        _result, logs = await _tick(
            db_session, targets, DRAIN_TARGET_BUDGET_SECONDS=DRAIN_TICK_BUDGET_SECONDS + 90
        )

        # 70, 140, 210 s: the tick budget is used by the first target.
        assert targets.batches(first) == 3
        assert targets.batches(second) == 0
        await db_session.refresh(first)
        assert first.job_status == "queued"
        assert _completed(logs, first)[0]["stop"] == "tick_budget"
        await db_session.refresh(second)
        assert second.job_status == "queued"
        assert second.next_run_at == due
        [deferred] = [e for e in logs if e["event"] == "indexer_tick_budget_used"]
        assert deferred["deferred"] >= 1


def _hour_key(state: IndexerState) -> str:
    return f"indexer:runs:{state.resource_id}:{state.context_id}:hour"


def _last_run_key(state: IndexerState) -> str:
    return f"indexer:last_run:{state.resource_id}:{state.context_id}"


async def _queue_again(db_session, state: IndexerState) -> None:
    state.job_status, state.next_run_at = "queued", utcnow() - timedelta(days=1)
    await db_session.commit()


class TestOneRunPerDrain:
    """A drain over several ticks is one run for the limiter: the hourly cap
    counts its first tick, and the interval counts from its last."""

    async def test_continuations_do_not_fill_the_hourly_cap(self, db_session, first):
        ticks = MAX_RUNS_PER_HOUR + 1
        targets = _Targets(batch_seconds=65)
        # Two batches of 100 per tick: the last tick drains it.
        targets.add(first, ticks * 200)
        redis = _Redis()
        stops = []
        for _ in range(ticks):
            _result, logs = await _tick(db_session, targets, redis)
            stops += [line["stop"] for line in _completed(logs, first)]

        assert stops == ["target_budget"] * (ticks - 1) + ["drained"]
        assert redis.store[_hour_key(first)] == "1"

        # New events inside the interval: refused by the interval, not the cap.
        await _queue_again(db_session, first)
        targets.pending[first.resource_id + str(first.context_id)] = 5
        _result, logs = await _tick(db_session, targets, redis)
        [waiting] = [e for e in logs if e.get("resource_id") == first.resource_id]
        assert waiting["event"] == "indexer_waiting_for_interval"
        assert waiting["reason"].startswith("min_interval_not_met")

        # Once the interval has passed since the drain ended, the run goes.
        import time

        redis.store[_last_run_key(first)] = str(time.time() - MIN_INTERVAL_SECONDS)
        await _queue_again(db_session, first)
        _result, logs = await _tick(db_session, targets, redis)
        assert _completed(logs, first)[0]["stop"] == "drained"
        assert redis.store[_hour_key(first)] == "2"

    async def test_a_continuation_refreshes_the_last_run(self, db_session, first):
        targets = _Targets(batch_seconds=65)
        targets.add(first, 400)
        redis = _Redis()
        await _tick(db_session, targets, redis)
        redis.store[_last_run_key(first)] = "0"

        await _tick(db_session, targets, redis)

        assert float(redis.store[_last_run_key(first)]) > 0
        assert redis.store[_hour_key(first)] == "1"


class TestSlowBatches:
    """A batch is started only when one more of the last batch's length fits
    the budget, so a slow deployment does not run past the next tick."""

    async def test_a_slow_batch_is_not_followed_by_one_that_would_overrun(self, db_session, first):
        second = await _second_target(db_session, first, waited=timedelta(hours=12))
        due = second.next_run_at
        targets = _Targets(batch_seconds=150)
        targets.add(first, 1000)
        targets.add(second, 100)

        _result, logs = await _tick(db_session, targets)

        # 150 s; a second batch would end at 300 s, the next tick (past both
        # budgets; the tick's is checked first).
        assert targets.batches(first) == 1
        assert _completed(logs, first)[0]["stop"] == "tick_budget"
        await db_session.refresh(first)
        assert first.job_status == "queued"
        assert first.metrics[DRAIN_CONTINUES_KEY] is True
        # 150 s + a 150 s batch passes the tick budget: the next row waits.
        assert targets.batches(second) == 0
        await db_session.refresh(second)
        assert second.job_status == "queued"
        assert second.next_run_at == due
        assert [e["deferred"] for e in logs if e["event"] == "indexer_tick_budget_used"] == [1]


class TestFailures:
    async def test_a_failed_statement_after_a_batch_marks_the_target_failed(
        self, db_session, first
    ):
        """The aborted transaction is rolled back before the failed state is
        written, and the tick goes on to the next target."""
        from sqlalchemy import text

        second = await _second_target(db_session, first, waited=timedelta(hours=12))
        targets = _Targets()
        targets.add(first, 500)
        targets.add(second, 50)
        has_pending = targets.has_pending

        async def _fails_for_first(db, state_id):
            if state_id == first.id:
                await db.execute(text("SELECT 1/0"))
            return await has_pending(db, state_id)

        targets.has_pending = _fails_for_first

        result, logs = await _tick(db_session, targets)

        assert result == {"picked": 2, "failed": 1, "rate_limited": 0}
        await db_session.refresh(first)
        assert first.job_status == "failed"
        assert "error" in first.metrics
        await db_session.refresh(second)
        assert second.job_status == "idle"
        assert targets.batches(second) == 1


class TestStaleRunning:
    """A target left ``running`` by a process that stopped mid-run is queued
    again once its last batch is older than the stale limit (#3038)."""

    async def test_a_stale_running_target_is_queued_and_run(self, db_session, first):
        first.job_status = "running"
        first.last_run_at = utcnow() - job.STALE_RUNNING_AFTER - timedelta(minutes=1)
        await db_session.commit()
        targets = _Targets()
        targets.add(first, 50)

        _result, logs = await _tick(db_session, targets)

        assert targets.batches(first) == 1
        await db_session.refresh(first)
        assert first.job_status == "idle"
        assert _completed(logs, first)[0]["stop"] == "drained"

    async def test_a_running_target_with_a_recent_batch_is_left_alone(self, db_session, first):
        first.job_status = "running"
        first.last_run_at = utcnow() - timedelta(minutes=5)
        await db_session.commit()
        targets = _Targets()
        targets.add(first, 50)

        await _tick(db_session, targets)

        await db_session.refresh(first)
        assert targets.batches(first) == 0
        assert first.job_status == "running"

    def test_the_stale_limit_is_well_above_a_tick(self):
        assert job.STALE_RUNNING_AFTER.total_seconds() >= 5 * DRAIN_TICK_BUDGET_SECONDS
