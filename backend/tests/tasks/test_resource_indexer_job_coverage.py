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

import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import numpy  # noqa: F401  isort: skip
import pydantic.root_model  # noqa: F401  isort: skip
import pytest_asyncio

# Importing ``tasks.resource_indexer_job`` triggers ``tasks/__init__`` →
# ``mcp_tasks`` → ``mcp.types``, whose ``RootModel[...]`` generic submodel
# creation does ``sys.modules['pydantic.root_model']`` — which raises a
# ``KeyError`` under coverage instrumentation if that module was never imported
# eagerly. The ``import pydantic.root_model`` above pins it into ``sys.modules``
# first so the import chain is stable with and without ``--cov``.
from models.auth import Context, Workspace
from models.resource import IndexerState, Resource
from tasks.resource_indexer_job import (
    MAX_RUNS_PER_HOUR,
    MIN_INTERVAL_SECONDS,
    can_run_indexer,
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
