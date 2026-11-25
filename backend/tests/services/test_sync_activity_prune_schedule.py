"""#2915: the activity retention runs hourly in its own transaction and never costs a tick.

The SQL side (indexed selector, rollback beside the run sweep, statement
count with every switch off) is in
``tests/integration/test_sync_activity_prune_db.py``.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import get_settings
from services import sync_audit, sync_feed_index
from services.resource_indexer import IndexerMetrics
from services.sync_feed_index import ActivityPruneSchedule, maintain_sync_runs
from tasks.resource_indexer_job import IndexerRateLimit, run_queued_indexers


@pytest.fixture
def clock(monkeypatch):
    """A process that has not pruned yet, on a monotonic clock the test moves."""
    now = [5_000.0]
    monkeypatch.setattr(sync_feed_index, "activity_prune_schedule", ActivityPruneSchedule())
    monkeypatch.setattr(sync_feed_index, "monotonic", lambda: now[0])
    monkeypatch.setattr(get_settings(), "sync_runs_enabled", False)
    return now


@pytest.fixture
def prune(monkeypatch):
    calls = AsyncMock()
    monkeypatch.setattr(sync_audit, "prune_activity", calls)
    return calls


def _session() -> AsyncMock:
    return AsyncMock(spec=AsyncSession)


@pytest.mark.asyncio
async def test_first_tick_after_start_prunes_and_commits_its_own_transaction(clock, prune):
    db = _session()
    await maintain_sync_runs(db)
    prune.assert_awaited_once_with(db)
    db.commit.assert_awaited()
    db.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_prune_runs_at_most_once_an_hour_per_process(clock, prune):
    for _tick in range(12):
        await maintain_sync_runs(_session())
        clock[0] += 299
    assert prune.await_count == 1
    clock[0] += 300
    await maintain_sync_runs(_session())
    assert prune.await_count == 2
    assert sync_feed_index.ACTIVITY_PRUNE_INTERVAL_SECONDS == 3600


@pytest.mark.asyncio
async def test_failing_prune_is_rolled_back_logged_and_waits_for_the_next_interval(clock, prune):
    prune.side_effect = RuntimeError("prune failed")
    db = _session()
    with structlog.testing.capture_logs() as logs:
        await maintain_sync_runs(db)
    db.rollback.assert_awaited()
    failed = [entry for entry in logs if entry["event"] == "sync_activity_prune_failed"]
    assert [(entry["log_level"], entry["error_class"]) for entry in failed] == [
        ("error", "RuntimeError")
    ]
    clock[0] += 300
    await maintain_sync_runs(_session())
    assert prune.await_count == 1


@pytest.mark.asyncio
async def test_failing_prune_is_logged_and_the_same_tick_still_processes_queued_indexers(
    clock, prune
):
    prune.side_effect = RuntimeError("prune failed")
    state = SimpleNamespace(
        resource_id="products",
        context_id="ctx",
        metrics={},
        job_status="queued",
        next_run_at=None,
        last_run_at=None,
    )
    picked = MagicMock()
    picked.scalars.return_value.all.return_value = [state]
    db = _session()
    db.execute.return_value = picked

    async def get_db():
        yield db

    indexer = MagicMock()
    indexer.process_incremental = AsyncMock(return_value=IndexerMetrics())
    with (
        patch("tasks.resource_indexer_job.get_db", get_db),
        patch("tasks.resource_indexer_job.ResourceIndexer", return_value=indexer),
        patch(
            "tasks.resource_indexer_job.check_indexer_rate_limit",
            AsyncMock(return_value=IndexerRateLimit(True, "ok")),
        ),
        patch("tasks.resource_indexer_job.record_indexer_run", AsyncMock()),
        structlog.testing.capture_logs() as logs,
    ):
        result = await run_queued_indexers()
    assert result == {"picked": 1, "failed": 0, "rate_limited": 0}
    indexer.process_incremental.assert_awaited_once()
    assert state.job_status == "idle"
    assert [entry["event"] for entry in logs if entry["event"].startswith("sync_activity")] == [
        "sync_activity_prune_failed"
    ]
