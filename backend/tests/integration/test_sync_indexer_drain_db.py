"""#3038: a committed snapshot is drained by the indexer job, with progress.

Real PostgreSQL: the feed commit writes the events and the outbox, the job
picks the target, the indexer applies the events batch after batch, and the
worker's run status and the owner's indexer status read the progress. Vector
side effects are doubles (``indexing``); the batch size is lowered so a few
events make several batches.
"""

from uuid import UUID

import pytest
import structlog
from sqlalchemy import select

from models.auth import Context
from models.resource import IndexerState, ResourceEvent
from services import resource_indexer
from services.resource_indexer import get_indexer_status_for_context
from tasks import resource_indexer_job as job
from tests.integration import sync_feed_fixtures as fixtures
from tests.integration.sync_feed_fixtures import committed, product, service
from tests.integration.test_sync_index_lock_busy_db import indexing  # noqa: F401  (fixture)

pytestmark = pytest.mark.asyncio(loop_scope="session")
store, worker, feeds = fixtures.store, fixtures.worker, fixtures.feeds


@pytest.fixture
def ticks(feeds, indexing, monkeypatch):  # noqa: F811
    """One job tick per call, picking only this test's workspace."""
    wid = feeds.store.wid
    monkeypatch.setattr(job, "BATCH_SIZE", 2)
    monkeypatch.setattr(job, "DRAIN_YIELD_SECONDS", 0)
    # The pick's slot hook narrowed to this workspace: rows other tests left
    # queued are not this test's to run.
    monkeypatch.setattr(job, "active_workspace_predicate", lambda column: column == wid)
    monkeypatch.setattr(
        resource_indexer.ResourceIndexer, "_plan_suspended", _returns(False), raising=True
    )
    monkeypatch.setattr(
        resource_indexer.ResourceIndexer, "_workspace_memory_room", _returns(1000), raising=True
    )

    async def get_db():
        async with feeds.factory() as db:
            yield db

    monkeypatch.setattr(job, "get_db", get_db)

    async def tick() -> list[dict]:
        with structlog.testing.capture_logs() as logs:
            await job.run_queued_indexers()
        return [entry for entry in logs if entry["event"] == "indexer_completed"]

    return tick


def _returns(value):
    async def method(self, *_args, **_kwargs):
        return value

    return method


def _target(feeds) -> tuple[UUID, UUID]:
    return UUID(feeds.target["resource_pk"]), UUID(feeds.target["context_id"])


async def _progress(feeds, run_id: str) -> tuple[str, dict]:
    result = await service(feeds, "GET", f"runs/{run_id}")
    assert result.status_code == 200, result.text
    body = result.json()
    return body["index_status"], body["index_progress"]


async def _owner(feeds) -> dict:
    _resource_pk, context_id = _target(feeds)
    async with feeds.factory() as db:
        context = await db.get(Context, context_id)
        return await get_indexer_status_for_context(db, context)


async def _state(feeds) -> IndexerState:
    resource_pk, context_id = _target(feeds)
    async with feeds.factory() as db:
        return await db.scalar(
            select(IndexerState).where(
                IndexerState.resource_pk == resource_pk, IndexerState.context_id == context_id
            )
        )


async def _last_event_id(feeds) -> int:
    resource_pk, _context_id = _target(feeds)
    async with feeds.factory() as db:
        return await db.scalar(
            select(ResourceEvent.id)
            .where(ResourceEvent.resource_pk == resource_pk)
            .order_by(ResourceEvent.id.desc())
            .limit(1)
        )


async def test_a_backlog_is_applied_in_one_job_call(feeds, ticks):
    run = await committed(feeds, [product(f"sku-{n}") for n in range(5)])
    assert await _progress(feeds, run["run_id"]) == ("pending", {"indexed": 0, "total": 5})
    owner = await _owner(feeds)
    assert owner["state"] is None
    assert owner["pending_events"] == 5

    [line] = await ticks()

    assert line["batches"] == 3
    assert line["events"] == 5
    assert line["stop"] == "drained"
    state = await _state(feeds)
    assert state.last_offset == await _last_event_id(feeds)
    assert state.job_status == "idle"
    assert await _progress(feeds, run["run_id"]) == ("complete", {"indexed": 5, "total": 5})
    assert (await _owner(feeds))["pending_events"] == 0


async def test_a_drain_cut_by_its_budget_continues_at_the_next_tick(feeds, ticks, monkeypatch):
    """With no time budget each tick runs one batch; the continuation is
    admitted although the interval has not passed (the ticks follow each
    other at once, the limiter reads the test Redis), and progress reads the
    same at both endpoints after every step."""
    monkeypatch.setattr(job, "DRAIN_TARGET_BUDGET_SECONDS", 0)
    run = await committed(feeds, [product(f"sku-{n}") for n in range(5)])

    steps = []
    for _tick in range(3):
        [line] = await ticks()
        state = await _state(feeds)
        status, progress = await _progress(feeds, run["run_id"])
        steps.append(
            (
                line["stop"],
                state.job_status,
                progress["indexed"],
                progress["total"],
                (await _owner(feeds))["pending_events"],
                status,
            )
        )

    assert steps == [
        ("target_budget", "queued", 2, 5, 3, "pending"),
        ("target_budget", "queued", 4, 5, 1, "pending"),
        ("drained", "idle", 5, 5, 0, "complete"),
    ]
    state = await _state(feeds)
    assert job.DRAIN_CONTINUES_KEY not in state.metrics
    # The backlog is applied: a new run of this target is limited again.
    with structlog.testing.capture_logs() as logs:
        state_ready = await _state(feeds)
        async with feeds.factory() as db:
            row = await db.get(IndexerState, state_ready.id)
            row.job_status = "queued"
            await db.commit()
        await job.run_queued_indexers()
    assert [entry["event"] for entry in logs if entry["event"].startswith("indexer_")][-1] == (
        "indexer_waiting_for_interval"
    )


async def test_a_run_without_index_events_reports_zero_of_zero(feeds, ticks):
    """A re-sent snapshot creates no index events: nothing to count."""
    await committed(feeds, [product("sku-0")])
    await ticks()
    resent = await committed(feeds, [product("sku-0")])

    assert await _progress(feeds, resent["run_id"]) == ("unchanged", {"indexed": 0, "total": 0})
