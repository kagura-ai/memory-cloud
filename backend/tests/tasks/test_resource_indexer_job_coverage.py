"""Coverage tests for the Resource Indexer background job (Issue #238).

``tasks.resource_indexer_job`` has three rate-limit helpers and the
APScheduler job body that drives them:

- ``can_run_indexer`` — token-bucket + min-interval rate limit, fail-open on
  Redis errors.
- ``record_indexer_run`` — records a run in Redis, swallowing failures so a
  recording error never blocks indexing.
- ``run_queued_indexers`` — selects ``queued`` IndexerState rows that are due,
  honours the rate limiter, transitions ``queued → running → idle`` (or
  ``failed`` on error), and records each successful run.
- ``schedule_resource_indexer_jobs`` — registers the 5-minute interval job.

These tests exercise the DB-backed job body against the real ``db_session``
(seeding Workspace + Resource + Context + IndexerState rows so the FK +
``resource_pk`` writer invariant from ``models/resource.py`` are satisfied) and
mock every external boundary: Redis (``get_redis_client`` /
``increment_counter``) and the ``ResourceIndexer`` service. No network calls.
"""

from __future__ import annotations

import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import numpy  # noqa: F401  isort: skip  # load-bearing: pre-load numpy's C ext before --cov instrumentation (else "cannot load module more than once")
import pydantic.root_model  # noqa: F401  isort: skip
import pytest
import pytest_asyncio
import structlog
from sqlalchemy import text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

# Importing ``tasks.resource_indexer_job`` triggers ``tasks/__init__`` →
# ``mcp_tasks`` → ``mcp.types``, whose ``RootModel[...]`` generic submodel
# creation does ``sys.modules['pydantic.root_model']`` — which raises a
# ``KeyError`` under coverage instrumentation if that module was never imported
# eagerly. The ``import pydantic.root_model`` above pins it into ``sys.modules``
# first so the import chain is stable with and without ``--cov``.
from models.auth import Context, Workspace
from models.resource import IndexerState, Resource
from services.resource_indexer import (
    IndexerMetrics,
    get_indexer_status_for_context,
    recorded_failed_events,
)
from tasks.resource_indexer_job import (
    MAX_RUNS_PER_HOUR,
    MIN_INTERVAL_SECONDS,
    MIN_INTERVAL_TOLERANCE_SECONDS,
    RATE_LIMITED_RETRY,
    RATE_LIMITED_RETRY_SLACK,
    IndexerRateLimit,
    can_run_indexer,
    check_indexer_rate_limit,
    rate_limited_retry_at,
    record_indexer_run,
    run_queued_indexers,
    schedule_resource_indexer_jobs,
)
from utils.datetime import utcnow


def _mock_get_db(db):
    """Build an async generator yielding ``db`` once (matches ``async for db in get_db()``)."""

    async def _gen():
        yield db

    return _gen


def _fake_redis():
    """A redis client mock whose get/setex are awaitable and configurable."""
    client = MagicMock()
    client.get = AsyncMock(return_value=None)
    client.setex = AsyncMock(return_value=True)
    client.pttl = AsyncMock(return_value=-2)  # no hour counter
    return client


@pytest_asyncio.fixture
async def seeded_state(db_session):
    """Seed Workspace + Resource + Context + a single queued, due IndexerState.

    Returns the IndexerState row. ``next_run_at`` is set 1 minute in the past so
    the job's ``next_run_at <= utcnow()`` filter selects it.
    """
    owner = f"owner_{uuid.uuid4().hex[:8]}"
    slug = f"res-{uuid.uuid4().hex[:8]}"
    ws = Workspace(
        id=uuid.uuid4(),
        name=f"ws-{uuid.uuid4().hex[:8]}",
        plan_name="pro",
        owner_user_id=owner,
        daily_api_limit=50000,
        weekly_api_limit=250000,
    )
    db_session.add(ws)
    await db_session.flush()

    ctx = Context(
        id=uuid.uuid4(),
        workspace_id=ws.id,
        name=f"ctx-{uuid.uuid4().hex[:8]}",
        created_by=owner,
        is_private=False,
    )
    resource = Resource(
        id=uuid.uuid4(),
        workspace_id=ws.id,
        resource_id=slug,
        name="Test Resource",
        created_by=owner,
    )
    db_session.add_all([ctx, resource])
    await db_session.flush()

    state = IndexerState(
        resource_pk=resource.id,
        resource_id=slug,
        context_id=ctx.id,
        last_offset=0,
        next_run_at=utcnow() - timedelta(minutes=1),
        job_status="queued",
    )
    db_session.add(state)
    await db_session.commit()
    return state


class TestCanRunIndexer:
    """Rate-limit gate: hourly quota + min interval, with Redis fail-open."""

    async def test_allows_run_when_no_history(self):
        """No counter and no last-run timestamp → (True, 'ok')."""
        client = _fake_redis()
        client.get = AsyncMock(return_value=None)
        with patch("tasks.resource_indexer_job.get_redis_client", return_value=client):
            ok, reason = await can_run_indexer("res-1", uuid.uuid4())
        assert ok is True
        assert reason == "ok"

    async def test_blocks_when_hourly_quota_exceeded(self):
        """runs_this_hour >= MAX_RUNS_PER_HOUR → blocked with quota reason."""
        client = _fake_redis()
        # First .get → hourly counter at the cap; last_run .get is never reached.
        client.get = AsyncMock(return_value=str(MAX_RUNS_PER_HOUR))
        with patch("tasks.resource_indexer_job.get_redis_client", return_value=client):
            ok, reason = await can_run_indexer("res-1", uuid.uuid4())
        assert ok is False
        assert reason.startswith("hourly_quota_exceeded")
        assert f"{MAX_RUNS_PER_HOUR}/{MAX_RUNS_PER_HOUR}" in reason

    async def test_blocks_when_min_interval_not_met(self):
        """A recent last-run timestamp (< MIN_INTERVAL) → blocked with wait reason."""
        client = _fake_redis()
        recent = "1000000.0"
        # Hour counter under cap, then last_run timestamp is "very recent".
        client.get = AsyncMock(side_effect=["0", recent])
        with (
            patch("tasks.resource_indexer_job.get_redis_client", return_value=client),
            # time.time() just after the recorded run → elapsed ~5s < 600s.
            patch("tasks.resource_indexer_job.time.time", return_value=1000005.0),
        ):
            ok, reason = await can_run_indexer("res-1", uuid.uuid4())
        assert ok is False
        assert reason.startswith("min_interval_not_met")

    async def test_allows_when_interval_elapsed(self):
        """Old last-run timestamp (>= MIN_INTERVAL elapsed) → allowed."""
        client = _fake_redis()
        client.get = AsyncMock(side_effect=["1", "1000000.0"])
        with (
            patch("tasks.resource_indexer_job.get_redis_client", return_value=client),
            patch(
                "tasks.resource_indexer_job.time.time",
                return_value=1000000.0 + MIN_INTERVAL_SECONDS + 1,
            ),
        ):
            ok, reason = await can_run_indexer("res-1", uuid.uuid4())
        assert ok is True
        assert reason == "ok"

    async def test_fail_open_on_redis_error(self):
        """Any Redis exception → fail-open (allow run) with the sentinel reason."""
        client = _fake_redis()
        client.get = AsyncMock(side_effect=RuntimeError("redis down"))
        with patch("tasks.resource_indexer_job.get_redis_client", return_value=client):
            ok, reason = await can_run_indexer("res-1", uuid.uuid4())
        assert ok is True
        assert reason == "redis_error_fail_open"


class TestCapHoldsWhenTheTtlReadFails:
    """#3009: a failed TTL read must not fail open past the hourly cap."""

    async def test_refused_with_the_plain_retry(self):
        client = AsyncMock()
        client.get = AsyncMock(return_value=str(MAX_RUNS_PER_HOUR))
        client.pttl = AsyncMock(side_effect=ConnectionError("down"))
        with patch("tasks.resource_indexer_job.get_redis_client", return_value=client):
            verdict = await check_indexer_rate_limit("res", uuid.uuid4())
        assert verdict.allowed is False
        assert verdict.limit == "hourly_cap"
        assert verdict.wait_seconds == RATE_LIMITED_RETRY.total_seconds()


class TestRecordIndexerRun:
    """Token-bucket recording: increments counter + stores timestamp, errors swallowed."""

    async def test_records_run_increments_and_sets_timestamp(self):
        """Happy path: increment_counter called with 1h TTL and setex stores the ts."""
        client = _fake_redis()
        ctx_id = uuid.uuid4()
        inc = AsyncMock(return_value=1)
        with (
            patch("tasks.resource_indexer_job.get_redis_client", return_value=client),
            patch("tasks.resource_indexer_job.increment_counter", inc),
        ):
            await record_indexer_run("res-1", ctx_id)

        inc.assert_awaited_once()
        # Hourly counter key + 1 hour TTL.
        args, kwargs = inc.call_args
        assert args[0] == f"indexer:runs:res-1:{ctx_id}:hour"
        assert kwargs["ttl"] == 3600
        # Last-run timestamp written with the doubled-interval TTL.
        client.setex.assert_awaited_once()
        setex_args = client.setex.call_args.args
        assert setex_args[0] == f"indexer:last_run:res-1:{ctx_id}"
        assert setex_args[1] == MIN_INTERVAL_SECONDS * 2

    async def test_record_run_swallows_redis_errors(self):
        """A failing increment must NOT raise — recording failure can't block indexing."""
        client = _fake_redis()
        inc = AsyncMock(side_effect=RuntimeError("redis down"))
        with (
            patch("tasks.resource_indexer_job.get_redis_client", return_value=client),
            patch("tasks.resource_indexer_job.increment_counter", inc),
        ):
            # Must return normally despite the error.
            await record_indexer_run("res-1", uuid.uuid4())
        # setex never reached because increment raised first.
        client.setex.assert_not_awaited()


class TestRunQueuedIndexers:
    """The APScheduler job body: selection, rate-limit skip, state transitions, errors."""

    async def test_no_queued_jobs_is_a_noop(self, db_session):
        """Empty queue → early return, no Redis or indexer touched."""
        client = _fake_redis()
        indexer_ctor = MagicMock()
        with (
            patch("tasks.resource_indexer_job.get_db", _mock_get_db(db_session)),
            patch("tasks.resource_indexer_job.get_redis_client", return_value=client),
            patch("tasks.resource_indexer_job.ResourceIndexer", indexer_ctor),
        ):
            await run_queued_indexers()
        indexer_ctor.assert_not_called()

    async def test_queue_pick_runs_the_longest_waiting_rows_first(self):
        """#1939: held-back rows re-queue hourly, so the 10-row pick must be
        ordered — oldest due ``next_run_at`` first, ``id`` as the tie-break —
        or they could keep crowding out runnable rows."""
        from sqlalchemy.dialects import postgresql

        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        db = MagicMock()
        db.execute = AsyncMock(return_value=result)
        db.commit = AsyncMock()

        with patch("tasks.resource_indexer_job.get_db", _mock_get_db(db)):
            await run_queued_indexers()

        stmt = db.execute.await_args.args[0]
        sql = str(stmt.compile(dialect=postgresql.dialect()))
        table = IndexerState.__tablename__
        assert f"ORDER BY {table}.next_run_at ASC, {table}.id ASC" in sql
        assert "LIMIT" in sql

    async def test_successful_run_transitions_to_idle_and_records(self, db_session, seeded_state):
        """Due queued job, allowed by rate limit → running → idle, metrics stored, run recorded."""
        client = _fake_redis()
        client.get = AsyncMock(return_value=None)  # can_run → ok

        metrics = MagicMock()
        metrics.to_dict.return_value = {"applied_upserts": 3, "skipped": False}
        indexer_instance = MagicMock()
        indexer_instance.process_incremental = AsyncMock(return_value=metrics)
        indexer_ctor = MagicMock(return_value=indexer_instance)
        record = AsyncMock()

        with (
            patch("tasks.resource_indexer_job.get_db", _mock_get_db(db_session)),
            patch("tasks.resource_indexer_job.get_redis_client", return_value=client),
            patch("tasks.resource_indexer_job.increment_counter", AsyncMock()),
            patch("tasks.resource_indexer_job.ResourceIndexer", indexer_ctor),
            patch("tasks.resource_indexer_job.record_indexer_run", record),
        ):
            await run_queued_indexers()

        # process_incremental called with the row's ids + batch_size=100.
        indexer_instance.process_incremental.assert_awaited_once()
        call = indexer_instance.process_incremental.call_args
        assert call.args[0] == seeded_state.resource_id
        assert call.kwargs.get("batch_size") == 100
        # A successful run was recorded for rate limiting.
        record.assert_awaited_once()

        # State persisted to idle with the metrics dict.
        await db_session.refresh(seeded_state)
        assert seeded_state.job_status == "idle"
        assert seeded_state.metrics == {"applied_upserts": 3, "skipped": False}
        assert seeded_state.last_run_at is not None

    async def test_ordinary_indexing_continues_while_sync_maintenance_fails(
        self, db_session, seeded_state
    ):
        """#2901: sync maintenance runs first in each tick. When it fails (here it
        leaves the transaction aborted, as a database error does), the job rolls
        it back, logs it and still indexes the queued ordinary row."""

        async def failing_maintenance(db):
            with pytest.raises(DBAPIError):
                await db.execute(text("SELECT 1/0"))
            raise RuntimeError("sync maintenance failed")

        client = _fake_redis()
        metrics = MagicMock()
        metrics.reason = None
        metrics.to_dict.return_value = {"applied_upserts": 1}
        indexer_instance = MagicMock()
        indexer_instance.process_incremental = AsyncMock(return_value=metrics)
        indexer_ctor = MagicMock(return_value=indexer_instance)
        errors: list[tuple[str, dict]] = []

        with (
            patch("tasks.resource_indexer_job.get_db", _mock_get_db(db_session)),
            patch("tasks.resource_indexer_job.maintain_sync_runs", failing_maintenance),
            patch("tasks.resource_indexer_job.get_redis_client", return_value=client),
            patch("tasks.resource_indexer_job.increment_counter", AsyncMock()),
            patch("tasks.resource_indexer_job.ResourceIndexer", indexer_ctor),
            patch("tasks.resource_indexer_job.record_indexer_run", AsyncMock()),
            patch(
                "tasks.resource_indexer_job.logger.error",
                lambda event, **kw: errors.append((event, kw)),
            ),
        ):
            result = await run_queued_indexers()

        assert result == {"picked": 1, "failed": 0, "rate_limited": 0}
        indexer_instance.process_incremental.assert_awaited_once()
        await db_session.refresh(seeded_state)
        assert seeded_state.job_status == "idle"
        assert seeded_state.metrics == {"applied_upserts": 1}
        assert [(event, kw["error_type"]) for event, kw in errors] == [
            ("indexer_sync_maintenance_failed", "RuntimeError")
        ]

    async def test_memories_per_day_refusal_requeues_for_utc_reset(self, db_session, seeded_state):
        """#1549: a batch refused for the daily memory-creation quota applied
        nothing, so the row must not park ``idle`` until the next ingest event —
        it is re-queued for the next UTC midnight, when the counter resets."""
        client = _fake_redis()
        client.get = AsyncMock(return_value=None)  # allowed

        metrics = MagicMock()
        metrics.skipped = True
        metrics.reason = "memories_per_day_exceeded"
        metrics.to_dict.return_value = {"skipped": True, "reason": "memories_per_day_exceeded"}
        indexer_instance = MagicMock()
        indexer_instance.process_incremental = AsyncMock(return_value=metrics)
        indexer_ctor = MagicMock(return_value=indexer_instance)
        record = AsyncMock()

        with (
            patch("tasks.resource_indexer_job.get_db", _mock_get_db(db_session)),
            patch("tasks.resource_indexer_job.get_redis_client", return_value=client),
            patch("tasks.resource_indexer_job.ResourceIndexer", indexer_ctor),
            patch("tasks.resource_indexer_job.record_indexer_run", record),
        ):
            await run_queued_indexers()

        await db_session.refresh(seeded_state)
        assert seeded_state.job_status == "queued"
        expected = datetime.combine(utcnow().date() + timedelta(days=1), datetime.min.time())
        assert seeded_state.next_run_at == expected
        assert seeded_state.metrics == {"skipped": True, "reason": "memories_per_day_exceeded"}
        # The attempt still counts toward the hourly token bucket.
        record.assert_awaited_once()

    @pytest.mark.parametrize(
        ("reason", "wait"),
        [("plan_suspended", timedelta(hours=24)), ("memory_limit_exceeded", timedelta(hours=1))],
    )
    async def test_suspended_or_full_requeues_later(self, db_session, seeded_state, reason, wait):
        """#1939: a batch held back because the plan suspended ingest or the
        workspace is at its memory limit applied nothing. The row stays queued
        (the events are kept) and is retried later — a plan pause daily (it
        lifts only on a plan change), a full workspace hourly."""
        client = _fake_redis()
        client.get = AsyncMock(return_value=None)  # allowed

        metrics = MagicMock()
        metrics.skipped = True
        metrics.reason = reason
        metrics.to_dict.return_value = {"skipped": True, "reason": reason}
        indexer_instance = MagicMock()
        indexer_instance.process_incremental = AsyncMock(return_value=metrics)
        before = utcnow()

        with (
            patch("tasks.resource_indexer_job.get_db", _mock_get_db(db_session)),
            patch("tasks.resource_indexer_job.get_redis_client", return_value=client),
            patch(
                "tasks.resource_indexer_job.ResourceIndexer",
                MagicMock(return_value=indexer_instance),
            ),
            patch("tasks.resource_indexer_job.record_indexer_run", AsyncMock()),
        ):
            await run_queued_indexers()

        await db_session.refresh(seeded_state)
        assert seeded_state.job_status == "queued"
        assert before + wait - timedelta(minutes=1) <= seeded_state.next_run_at
        assert seeded_state.next_run_at <= utcnow() + wait

    async def test_rate_limited_job_is_skipped_and_stays_queued(self, db_session, seeded_state):
        """can_run_indexer → False: the indexer is never built and state stays queued."""
        client = _fake_redis()
        # Hourly counter at the cap → can_run returns False.
        client.get = AsyncMock(return_value=str(MAX_RUNS_PER_HOUR))
        indexer_ctor = MagicMock()
        record = AsyncMock()

        with (
            patch("tasks.resource_indexer_job.get_db", _mock_get_db(db_session)),
            patch("tasks.resource_indexer_job.get_redis_client", return_value=client),
            patch("tasks.resource_indexer_job.ResourceIndexer", indexer_ctor),
            patch("tasks.resource_indexer_job.record_indexer_run", record),
        ):
            await run_queued_indexers()

        indexer_ctor.assert_not_called()
        record.assert_not_awaited()
        await db_session.refresh(seeded_state)
        assert seeded_state.job_status == "queued"
        # Moved behind the rows that can run now (longest-waiting-first pick).
        assert seeded_state.next_run_at > utcnow() + timedelta(minutes=4)

    async def test_indexer_failure_marks_state_failed_with_error(self, db_session, seeded_state):
        """process_incremental raises → state transitions to failed with the error metric."""
        client = _fake_redis()
        client.get = AsyncMock(return_value=None)  # allowed

        indexer_instance = MagicMock()
        indexer_instance.process_incremental = AsyncMock(side_effect=RuntimeError("boom indexing"))
        indexer_ctor = MagicMock(return_value=indexer_instance)
        record = AsyncMock()

        with (
            patch("tasks.resource_indexer_job.get_db", _mock_get_db(db_session)),
            patch("tasks.resource_indexer_job.get_redis_client", return_value=client),
            patch("tasks.resource_indexer_job.ResourceIndexer", indexer_ctor),
            patch("tasks.resource_indexer_job.record_indexer_run", record),
        ):
            await run_queued_indexers()

        # Failure path does NOT record a successful run.
        record.assert_not_awaited()
        await db_session.refresh(seeded_state)
        assert seeded_state.job_status == "failed"
        assert seeded_state.metrics == {"error": "boom indexing"}

    async def test_not_due_job_is_not_selected(self, db_session, seeded_state):
        """A queued job whose next_run_at is in the future is filtered out (no-op)."""
        # Push the due time into the future so the WHERE clause excludes it.
        seeded_state.next_run_at = utcnow() + timedelta(hours=1)
        db_session.add(seeded_state)
        await db_session.commit()

        client = _fake_redis()
        indexer_ctor = MagicMock()
        with (
            patch("tasks.resource_indexer_job.get_db", _mock_get_db(db_session)),
            patch("tasks.resource_indexer_job.get_redis_client", return_value=client),
            patch("tasks.resource_indexer_job.ResourceIndexer", indexer_ctor),
        ):
            await run_queued_indexers()

        indexer_ctor.assert_not_called()
        await db_session.refresh(seeded_state)
        assert seeded_state.job_status == "queued"

    async def test_outer_exception_is_swallowed(self, db_session):
        """A failure in the SELECT path is caught (job must not escape to APScheduler)."""
        db = MagicMock()
        db.execute = AsyncMock(side_effect=RuntimeError("db gone"))
        db.commit = AsyncMock()

        with (
            patch("tasks.resource_indexer_job.get_db", _mock_get_db(db)),
            patch(
                "tasks.resource_indexer_job.get_redis_client",
                return_value=_fake_redis(),
            ),
        ):
            # Must return normally — the outer try/except logs and returns.
            await run_queued_indexers()

        db.execute.assert_awaited_once()


FAILED_EVENT = {"event_id": 7, "doc_id": "doc_7", "error_class": "RuntimeError"}


def _returns_the_record(reason=None, *, batch_committed=False, rollback=None):
    """A ``process_incremental`` that returns the record it is given, as the
    indexer does on every run that leaves it unchanged (#2477)."""

    async def _run(*_a, failed_events=(), **_k):
        if rollback is not None:
            await rollback()
        return IndexerMetrics(
            skipped=reason is not None,
            reason=reason,
            failed_events=list(failed_events),
            batch_committed=batch_committed,
        )

    return _run


async def _run_job(db_session, process_incremental, record=None):
    """One job cycle with the given ``process_incremental`` side effect.

    Returns the ``process_incremental`` mock.
    """
    client = _fake_redis()
    indexer_instance = MagicMock()
    indexer_instance.process_incremental = AsyncMock(side_effect=process_incremental)
    with (
        patch("tasks.resource_indexer_job.get_db", _mock_get_db(db_session)),
        patch("tasks.resource_indexer_job.get_redis_client", return_value=client),
        patch("tasks.resource_indexer_job.increment_counter", AsyncMock()),
        patch(
            "tasks.resource_indexer_job.ResourceIndexer",
            MagicMock(return_value=indexer_instance),
        ),
        patch("tasks.resource_indexer_job.record_indexer_run", record or AsyncMock()),
    ):
        await run_queued_indexers()
    return indexer_instance.process_incremental


def _operator_writes_then_allows(db_session, statement, params):
    """A ``check_indexer_rate_limit`` that first commits ``statement`` from a session of
    its own, as an operator's psql would.

    The job calls it after the top-of-loop refresh and before the ``running``
    commit, the window a runbook write still passes the
    ``job_status <> 'running'`` guard in (#2477).
    """

    async def _can_run(*_a):
        async with AsyncSession(bind=db_session.bind, expire_on_commit=False) as operator:
            await operator.execute(text(statement), params)
            await operator.commit()
        return IndexerRateLimit(True, "ok")

    return _can_run


class TestFailedEventsSurviveTheJob:
    """#2254 / #2477: the skipped events in ``state.metrics`` are the
    operator's only rewind target. The job hands the row's record to the run,
    whose batch commit writes it, and stores what the run returns."""

    @pytest_asyncio.fixture
    async def state_with_failed_events(self, db_session, seeded_state):
        seeded_state.metrics = {"errors": 1, "failed_events": [FAILED_EVENT]}
        await db_session.commit()
        return seeded_state

    async def test_the_run_is_given_the_rows_record(self, db_session, state_with_failed_events):
        """So the batch commit already holds the merge (#2477)."""
        process = await _run_job(db_session, _returns_the_record())

        assert process.await_args.kwargs["failed_events"] == [FAILED_EVENT]

    @pytest.mark.parametrize(
        "reason",
        [None, "no_pending_events", "schema_not_found", "memory_limit_exceeded", "plan_suspended"],
    )
    async def test_the_row_keeps_the_record_the_run_returns(
        self, db_session, state_with_failed_events, reason
    ):
        """A batch with no failures (``None``) included: only an operator acts
        on the record (#2477)."""
        await _run_job(db_session, _returns_the_record(reason, batch_committed=reason is None))

        await db_session.refresh(state_with_failed_events)
        assert state_with_failed_events.metrics["failed_events"] == [FAILED_EVENT]
        assert state_with_failed_events.metrics["reason"] == reason

    async def test_the_job_does_not_add_the_old_record_back(
        self, db_session, state_with_failed_events
    ):
        """A replay ran the recorded event again and the run dropped it: the
        job's second commit must not merge it back."""
        newer = {"event_id": 12, "doc_id": "doc_12", "error_class": "QdrantError"}
        metrics = IndexerMetrics(errors=1, failed_events=[newer], batch_committed=True)

        await _run_job(db_session, [metrics])

        await db_session.refresh(state_with_failed_events)
        assert state_with_failed_events.metrics["failed_events"] == [newer]

    async def test_a_rolled_back_run_keeps_the_record(self, db_session, state_with_failed_events):
        """The rollback expired the row; the job's write after it still lands."""
        await _run_job(
            db_session, _returns_the_record("commit failed", rollback=db_session.rollback)
        )

        await db_session.refresh(state_with_failed_events)
        assert state_with_failed_events.metrics["failed_events"] == [FAILED_EVENT]
        assert state_with_failed_events.job_status == "idle"

    async def test_a_metrics_value_that_is_no_object_does_not_strand_the_row(
        self, db_session, seeded_state
    ):
        """The column is edited by hand; the read comes after ``running``."""
        seeded_state.metrics = ["hand", "edited"]
        await db_session.commit()

        await _run_job(db_session, [IndexerMetrics(skipped=True, reason="no_pending_events")])

        await db_session.refresh(seeded_state)
        assert seeded_state.job_status == "idle"

    async def test_a_rewind_after_the_pick_is_the_offset_the_run_starts_from(
        self, db_session, seeded_state
    ):
        """The session still holds the row at its old offset."""
        seeded_state.last_offset = 100
        await db_session.commit()
        await db_session.execute(
            update(IndexerState)
            .where(IndexerState.id == seeded_state.id)
            .values(last_offset=6)
            .execution_options(synchronize_session=False)
        )
        await db_session.commit()
        offsets = []

        async def _see_the_offset(*_a, **_k):
            offsets.append(seeded_state.last_offset)
            return IndexerMetrics(skipped=True, reason="no_pending_events")

        await _run_job(db_session, _see_the_offset)

        assert offsets == [6]

    async def test_a_rewind_before_the_running_commit_is_the_offset_the_run_starts_from(
        self, db_session, seeded_state
    ):
        """#2477: the rewind lands after the top-of-loop refresh, while the row
        is not ``running`` yet, so the runbook's guard lets it through."""
        seeded_state.last_offset = 100
        await db_session.commit()
        rewind = (
            "UPDATE indexer_state SET last_offset = 6 WHERE id = :id AND job_status <> 'running'"
        )
        offsets = []

        async def _see_the_offset(*_a, **_k):
            offsets.append(seeded_state.last_offset)
            return IndexerMetrics(skipped=True, reason="no_pending_events")

        with patch(
            "tasks.resource_indexer_job.check_indexer_rate_limit",
            _operator_writes_then_allows(db_session, rewind, {"id": seeded_state.id}),
        ):
            await _run_job(db_session, _see_the_offset)

        assert offsets == [6]

    async def test_a_rolled_back_run_does_not_sink_the_next_row(self, db_session, seeded_state):
        """A rollback expires every row in the session, the next one included."""
        first_context = await db_session.get(Context, seeded_state.context_id)
        other_context = Context(
            id=uuid.uuid4(),
            workspace_id=first_context.workspace_id,
            name=f"ctx-{uuid.uuid4().hex[:8]}",
            created_by=first_context.created_by,
            is_private=False,
        )
        db_session.add(other_context)
        await db_session.flush()
        second = IndexerState(
            resource_pk=seeded_state.resource_pk,
            resource_id=seeded_state.resource_id,
            context_id=other_context.id,
            last_offset=0,
            next_run_at=utcnow() - timedelta(seconds=30),
            job_status="queued",
        )
        db_session.add(second)
        await db_session.commit()
        ran = []

        async def _roll_back_once(_resource_id, context_id, **_k):
            ran.append(context_id)
            if len(ran) == 1:
                await db_session.rollback()
                db_session.expire_all()
            return IndexerMetrics(errors=1, reason="commit failed")

        await _run_job(db_session, _roll_back_once)

        assert len(ran) == 2

    async def test_a_raising_run_keeps_the_record_beside_the_error(
        self, db_session, state_with_failed_events
    ):
        await _run_job(db_session, RuntimeError("boom indexing"))

        await db_session.refresh(state_with_failed_events)
        assert state_with_failed_events.job_status == "failed"
        assert state_with_failed_events.metrics == {
            "error": "boom indexing",
            "failed_events": [FAILED_EVENT],
        }

    async def test_a_failure_after_the_batch_keeps_that_batchs_record(
        self, db_session, state_with_failed_events
    ):
        newer = {"event_id": 12, "doc_id": "doc_12", "error_class": "QdrantError"}
        metrics = IndexerMetrics(
            errors=1, failed_events=[FAILED_EVENT, newer], batch_committed=True
        )

        await _run_job(
            db_session, [metrics], record=AsyncMock(side_effect=RuntimeError("after the run"))
        )

        await db_session.refresh(state_with_failed_events)
        assert state_with_failed_events.metrics == {
            "error": "after the run",
            "failed_events": [FAILED_EVENT, newer],
        }

    async def test_the_status_reader_counts_the_record(self, db_session, seeded_state):
        seeded_state.metrics = {
            "errors": 1,
            "failed_events": [FAILED_EVENT, {**FAILED_EVENT, "event_id": 8}],
        }
        await db_session.commit()
        context = await db_session.get(Context, seeded_state.context_id)
        context.resource_id = seeded_state.resource_id

        status = await get_indexer_status_for_context(db_session, context)

        assert status["state"]["metrics"]["failed_events"] == 2

    @pytest.mark.parametrize(
        "stored", [{"error": "boom"}, {"failed_events": "not a list"}, None, {}]
    )
    async def test_the_status_reader_counts_a_row_without_a_record_as_zero(
        self, db_session, seeded_state, stored
    ):
        """A row written before #2254, or by the job's error path."""
        seeded_state.metrics = stored
        await db_session.commit()
        context = await db_session.get(Context, seeded_state.context_id)
        context.resource_id = seeded_state.resource_id

        status = await get_indexer_status_for_context(db_session, context)

        assert status["state"]["metrics"]["failed_events"] == 0


_RUNBOOK = Path(__file__).resolve().parents[3] / "docs" / "ops" / "resource-indexer-backfill.md"


def _runbook_clear() -> str:
    """The runbook's clear of ``metrics.failed_events`` (#2477), its
    placeholders turned into bind parameters."""
    docs = _RUNBOOK.read_text()
    section = docs[docs.index("### Clear the list") :]
    block = section[section.index("```sql\n") + len("```sql\n") :]
    statement = block[: block.index("```")]
    assert "'<resource_id>'" in statement and "'<context_id>'" in statement, statement
    return statement.replace("'<resource_id>'", ":resource_id").replace(
        "'<context_id>'", ":context_id"
    )


class TestTheOperatorClearsTheRecord:
    """#2477: the record stays until an operator acts on it. The runbook's
    clear, run as written, is how they drop it without a rewind."""

    async def _clear(self, db_session, state) -> None:
        await db_session.execute(
            text(_runbook_clear()),
            {"resource_id": state.resource_id, "context_id": state.context_id},
        )
        await db_session.commit()

    async def test_the_clear_empties_the_record_and_keeps_the_rest(self, db_session, seeded_state):
        seeded_state.metrics = {"errors": 1, "failed_events": [FAILED_EVENT]}
        seeded_state.job_status = "idle"
        await db_session.commit()

        await self._clear(db_session, seeded_state)

        await db_session.refresh(seeded_state)
        assert recorded_failed_events(seeded_state.metrics) == []
        assert seeded_state.metrics["errors"] == 1
        context = await db_session.get(Context, seeded_state.context_id)
        context.resource_id = seeded_state.resource_id
        status = await get_indexer_status_for_context(db_session, context)
        assert status["state"]["metrics"]["failed_events"] == 0

    async def test_the_next_run_is_given_an_empty_record(self, db_session, seeded_state):
        seeded_state.metrics = {"failed_events": [FAILED_EVENT]}
        await db_session.commit()
        await self._clear(db_session, seeded_state)

        process = await _run_job(db_session, _returns_the_record())

        assert process.await_args.kwargs["failed_events"] == []
        await db_session.refresh(seeded_state)
        assert seeded_state.metrics["failed_events"] == []

    async def test_a_clear_before_the_running_commit_is_not_written_over(
        self, db_session, seeded_state
    ):
        """The clear lands after the top-of-loop refresh, while the row is not
        ``running`` yet, so the runbook's guard lets it through: the run must
        start from it, not from the copy the refresh read."""
        seeded_state.metrics = {"failed_events": [FAILED_EVENT]}
        await db_session.commit()
        params = {"resource_id": seeded_state.resource_id, "context_id": seeded_state.context_id}

        with patch(
            "tasks.resource_indexer_job.check_indexer_rate_limit",
            _operator_writes_then_allows(db_session, _runbook_clear(), params),
        ):
            process = await _run_job(db_session, _returns_the_record())

        assert process.await_args.kwargs["failed_events"] == []
        await db_session.refresh(seeded_state)
        assert seeded_state.metrics["failed_events"] == []

    async def test_the_clear_leaves_a_running_row_alone(self, db_session, seeded_state):
        """The run in progress would write its record back over the clear."""
        seeded_state.metrics = {"failed_events": [FAILED_EVENT]}
        seeded_state.job_status = "running"
        await db_session.commit()

        await self._clear(db_session, seeded_state)

        await db_session.refresh(seeded_state)
        assert seeded_state.metrics["failed_events"] == [FAILED_EVENT]


class TestScheduleResourceIndexerJobs:
    """Registration of the interval job with APScheduler."""

    def test_adds_interval_job_with_expected_config(self):
        """schedule registers run_queued_indexers on a 5-minute interval, single-instance."""
        scheduler = MagicMock()
        schedule_resource_indexer_jobs(scheduler)

        scheduler.add_job.assert_called_once()
        args, kwargs = scheduler.add_job.call_args
        assert args[0] is run_queued_indexers
        assert kwargs["id"] == "resource_indexer"
        assert kwargs["coalesce"] is True
        assert kwargs["max_instances"] == 1
        assert kwargs["replace_existing"] is True
        # 5-minute interval trigger.
        trigger = kwargs["trigger"]
        assert trigger.interval == timedelta(minutes=5)


class _Clock:
    """One wall clock for both ``time.time()`` and ``utcnow()`` (#3009).

    The pick query compares ``next_run_at`` with the process clock
    (``utcnow()``) and the interval check reads ``time.time()``; both are
    driven from here. Each read moves the clock on a little, as a real tick
    takes time between its pick and its skip.
    """

    def __init__(self, start: float) -> None:
        self.t = start

    def time(self) -> float:
        self.t += 0.01
        return self.t

    def utcnow(self) -> datetime:
        self.t += 0.01
        return datetime.fromtimestamp(self.t, UTC).replace(tzinfo=None)


class _ClockRedis:
    """An in-memory Redis with TTLs on ``_Clock`` (get / setex / INCR)."""

    def __init__(self, clock: _Clock) -> None:
        self.clock = clock
        self.store: dict[str, tuple[str, float | None]] = {}

    def _live(self, key: str) -> str | None:
        entry = self.store.get(key)
        if entry is None:
            return None
        value, expires = entry
        if expires is not None and self.clock.t >= expires:
            del self.store[key]
            return None
        return value

    async def get(self, key: str) -> str | None:
        return self._live(key)

    async def setex(self, key: str, ttl: int, value: str) -> bool:
        self.store[key] = (value, self.clock.t + ttl)
        return True

    async def increment_counter(self, key: str, ttl: int | None = None) -> int:
        current = self._live(key)
        count = int(current or 0) + 1
        expires = self.store[key][1] if current is not None else None
        if expires is None and ttl:
            expires = self.clock.t + ttl
        self.store[key] = (str(count), expires)
        return count

    async def pttl(self, key: str) -> int:
        """Milliseconds left: -2 for a missing key, -1 for a key without TTL."""
        if self._live(key) is None:
            return -2
        expires = self.store[key][1]
        return -1 if expires is None else int((expires - self.clock.t) * 1000)


@contextmanager
def _clocked(clock: _Clock, redis: _ClockRedis):
    """Patch the job's clocks and Redis onto ``clock`` / ``redis``."""
    with (
        patch("tasks.resource_indexer_job.time.time", clock.time),
        patch("tasks.resource_indexer_job.utcnow", clock.utcnow),
        patch("tasks.resource_indexer_job.get_redis_client", return_value=redis),
        patch("tasks.resource_indexer_job.increment_counter", redis.increment_counter),
    ):
        yield


class TestIntervalSkipNextTick:
    """#3009: a row the minimum interval refuses runs at the first tick at
    which the interval check admits it, not one tick later."""

    async def test_run_then_ticks_at_300_and_600(self, db_session, seeded_state):
        clock = _Clock(time.time())
        start = clock.t
        redis = _ClockRedis(clock)
        runs: list[float] = []

        async def _run(resource_id, *_a, failed_events=(), **_k):
            if resource_id == seeded_state.resource_id:
                runs.append(clock.t)
            clock.t += 5  # the run takes a few seconds; the timestamp is at its end
            return IndexerMetrics(failed_events=list(failed_events))

        indexer = MagicMock()
        indexer.process_incremental = AsyncMock(side_effect=_run)

        async def tick(at: float):
            clock.t = start + at
            with (
                _clocked(clock, redis),
                patch("tasks.resource_indexer_job.get_db", _mock_get_db(db_session)),
                patch(
                    "tasks.resource_indexer_job.ResourceIndexer",
                    MagicMock(return_value=indexer),
                ),
                structlog.testing.capture_logs() as logs,
            ):
                await run_queued_indexers()
            # Rows other tests left queued may be picked too; only this one counts.
            return [e for e in logs if e.get("resource_id") == seeded_state.resource_id]

        # T: the resource is indexed.
        await tick(0)
        assert len(runs) == 1
        await db_session.refresh(seeded_state)
        assert seeded_state.job_status == "idle"

        # T+30: a new change arrives and queues the row (as the sync feed does).
        clock.t = start + 30
        seeded_state.job_status = "queued"
        seeded_state.next_run_at = clock.utcnow()
        await db_session.commit()

        # T+300: too early, skipped; the row stays queued.
        logs = await tick(300)
        assert len(runs) == 1
        await db_session.refresh(seeded_state)
        assert seeded_state.job_status == "queued"
        waiting = [e for e in logs if e["event"] == "indexer_waiting_for_interval"]
        assert len(waiting) == 1
        assert waiting[0]["log_level"] == "info"
        assert waiting[0]["resource_id"] == seeded_state.resource_id
        assert waiting[0]["context_id"] == seeded_state.context_id
        # Admitted at 5 + 600 - 60 = 545 s; the skip ran at about 300 s.
        assert waiting[0]["wait_seconds"] == 245
        assert not [e for e in logs if e["event"] == "indexer_skipped"]

        # T+600: picked and run.
        await tick(600)
        assert len(runs) == 2
        assert runs[1] - start == pytest.approx(600, abs=1)

    @pytest.mark.parametrize(
        ("counter_ttl", "wait", "logged"),
        [(200, 200, 200), (3000, 3000, 3000), (None, 300, 300)],
        ids=["expires-before-next-tick", "expires-later", "no-ttl"],
    )
    async def test_hourly_cap_skip_waits_for_the_counter(
        self, db_session, seeded_state, counter_ttl, wait, logged
    ):
        """A row refused by the hourly cap is due slack before the counter
        expires, however far ahead, so it is not picked and refused at every
        tick in between (a counter without a TTL waits the plain retry)."""
        clock = _Clock(time.time())
        redis = _ClockRedis(clock)
        rid = seeded_state.resource_id
        key = f"indexer:runs:{rid}:{seeded_state.context_id}:hour"
        if counter_ttl is None:
            redis.store[key] = (str(MAX_RUNS_PER_HOUR), None)
        else:
            await redis.setex(key, counter_ttl, str(MAX_RUNS_PER_HOUR))
        with (
            _clocked(clock, redis),
            patch("tasks.resource_indexer_job.get_db", _mock_get_db(db_session)),
            patch("tasks.resource_indexer_job.ResourceIndexer", MagicMock()),
            structlog.testing.capture_logs() as logs,
        ):
            before = clock.utcnow()
            await run_queued_indexers()
        await db_session.refresh(seeded_state)
        assert seeded_state.job_status == "queued"
        due = before + timedelta(seconds=wait) - RATE_LIMITED_RETRY_SLACK
        assert due <= seeded_state.next_run_at <= due + timedelta(seconds=1)
        # Rows other tests left queued may be picked too; only this one counts.
        mine = [e for e in logs if e.get("resource_id") == rid]
        assert [e["event"] for e in mine] == ["indexer_skipped"]
        # The log says how long the cap holds the row, not the retry step.
        assert mine[0]["wait_seconds"] == logged


class TestHourlyCapNextTick:
    """#3009: a row the hourly cap refuses runs at the first tick after the
    hour counter has expired, not one tick later."""

    @pytest.mark.parametrize("refused_at", [3400, 3590])
    async def test_cap_refuses_then_the_next_tick_runs(self, db_session, seeded_state, refused_at):
        clock = _Clock(time.time())
        start = clock.t
        redis = _ClockRedis(clock)
        rid = seeded_state.resource_id
        key = f"indexer:runs:{rid}:{seeded_state.context_id}:hour"
        # The hour's sixth run finished long ago; the counter expires at T+3600.
        await redis.setex(key, 3600, str(MAX_RUNS_PER_HOUR))
        runs: list[float] = []

        async def _run(resource_id, *_a, failed_events=(), **_k):
            if resource_id == rid:
                runs.append(clock.t)
            clock.t += 5
            return IndexerMetrics(failed_events=list(failed_events))

        indexer = MagicMock()
        indexer.process_incremental = AsyncMock(side_effect=_run)

        async def tick(at: float):
            clock.t = start + at
            with (
                _clocked(clock, redis),
                patch("tasks.resource_indexer_job.get_db", _mock_get_db(db_session)),
                patch(
                    "tasks.resource_indexer_job.ResourceIndexer",
                    MagicMock(return_value=indexer),
                ),
                structlog.testing.capture_logs() as logs,
            ):
                await run_queued_indexers()
            return [e for e in logs if e.get("resource_id") == rid]

        logs = await tick(refused_at)
        assert runs == []
        assert [e["event"] for e in logs] == ["indexer_skipped"]
        await db_session.refresh(seeded_state)
        assert seeded_state.job_status == "queued"

        await tick(refused_at + 300)
        assert len(runs) == 1
        assert runs[0] - start == pytest.approx(refused_at + 300, abs=1)


class TestIntervalTolerance:
    """#3009: the interval check admits a run ``MIN_INTERVAL_TOLERANCE_SECONDS``
    early; the hourly cap is unchanged."""

    async def _verdict(self, elapsed: float, runs_this_hour: int = 1):
        clock = _Clock(1_000_000.0)
        redis = _ClockRedis(clock)
        await redis.setex("indexer:runs:res-1:ctx:hour", 3600, str(runs_this_hour))
        await redis.setex("indexer:last_run:res-1:ctx", 1200, "1000000.0")
        clock.t = 1_000_000.0 + elapsed - 0.01  # the read itself moves the clock 0.01 s
        with patch("tasks.resource_indexer_job.get_redis_client", return_value=redis):
            with patch("tasks.resource_indexer_job.time.time", clock.time):
                return await check_indexer_rate_limit("res-1", "ctx")

    async def test_admitted_at_interval_less_tolerance(self):
        verdict = await self._verdict(MIN_INTERVAL_SECONDS - MIN_INTERVAL_TOLERANCE_SECONDS)
        assert verdict == IndexerRateLimit(True, "ok")

    async def test_refused_just_before_with_the_wait(self):
        verdict = await self._verdict(MIN_INTERVAL_SECONDS - MIN_INTERVAL_TOLERANCE_SECONDS - 10)
        assert verdict.allowed is False
        assert verdict.reason == "min_interval_not_met (wait 10s)"
        assert verdict.limit == "min_interval"
        assert verdict.wait_seconds == pytest.approx(10)

    async def test_cap_refuses_whatever_the_interval(self):
        verdict = await self._verdict(MIN_INTERVAL_SECONDS * 2, runs_this_hour=MAX_RUNS_PER_HOUR)
        assert verdict.allowed is False
        assert verdict.limit == "hourly_cap"
        # The counter was set at 1_000_000 with an hour's TTL.
        assert verdict.wait_seconds == pytest.approx(3600 - MIN_INTERVAL_SECONDS * 2, abs=1)
        assert verdict.reason.startswith("hourly_quota_exceeded")

    @pytest.mark.parametrize("step", [60, 300])
    async def test_no_hour_holds_more_runs_than_the_cap(self, step):
        """Ticks every ``step`` s for three hours, runs of 5 s: no window of an
        hour holds more than MAX_RUNS_PER_HOUR runs, with the tolerance on."""
        clock = _Clock(1_000_000.0)
        redis = _ClockRedis(clock)
        runs: list[float] = []
        with (
            patch("tasks.resource_indexer_job.time.time", clock.time),
            patch("tasks.resource_indexer_job.get_redis_client", return_value=redis),
            patch("tasks.resource_indexer_job.increment_counter", redis.increment_counter),
        ):
            for n in range(3 * 3600 // step):
                clock.t = 1_000_000.0 + n * step
                if (await check_indexer_rate_limit("res-1", "ctx")).allowed:
                    runs.append(clock.t)
                    clock.t += 5
                    await record_indexer_run("res-1", "ctx")
        assert runs
        for first in runs:
            in_hour = [r for r in runs if first <= r < first + 3600]
            assert len(in_hour) <= MAX_RUNS_PER_HOUR
        # The cap, not the interval, is what binds in the first hour.
        assert len([r for r in runs if r < runs[0] + 3600]) == MAX_RUNS_PER_HOUR


class TestRateLimitedRetryAt:
    """#3009: the reschedule of a row the rate limit refused."""

    def test_due_slack_before_the_admitting_moment(self):
        now = datetime(2026, 1, 1, 0, 0, 0)
        assert (
            rate_limited_retry_at(now, 245)
            == now + timedelta(seconds=245) - RATE_LIMITED_RETRY_SLACK
        )

    def test_capped_at_the_rate_limited_retry(self):
        now = datetime(2026, 1, 1, 0, 0, 0)
        assert (
            rate_limited_retry_at(now, 10_000)
            == now + RATE_LIMITED_RETRY - RATE_LIMITED_RETRY_SLACK
        )

    def test_no_cap_waits_the_whole_time(self):
        now = datetime(2026, 1, 1, 0, 0, 0)
        assert (
            rate_limited_retry_at(now, 2700, cap=None)
            == now + timedelta(seconds=2700) - RATE_LIMITED_RETRY_SLACK
        )

    def test_never_before_now(self):
        now = datetime(2026, 1, 1, 0, 0, 0)
        assert rate_limited_retry_at(now, 5) == now
        assert rate_limited_retry_at(now, -3) == now
