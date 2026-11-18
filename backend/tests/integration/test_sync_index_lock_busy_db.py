"""#2904: a busy guard lock never costs the indexer a committed sync event.

Real PostgreSQL sessions: a second session holds the row (or advisory) lock
while the indexer runs, exactly as a worker's status poll does.
"""

import json
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from sqlalchemy import select, text

from models.auth import Context
from models.memory import Memory
from models.resource import IndexerState, Resource, ResourceEvent
from models.sync_feed import SyncIndexOutbox, SyncRecord
from services import resource_indexer
from services.resource_indexer import recorded_failed_events
from services.sync_feed_index import maintain_sync_runs
from services.sync_feed_provenance import guard_sync_event
from services.sync_managed_guard import target_fence
from tests.integration import sync_feed_fixtures as fixtures
from tests.integration.sync_feed_fixtures import committed, human, product, service, staged
from utils.exceptions import ConflictError, SyncLockBusyError

pytestmark = pytest.mark.asyncio(loop_scope="session")
store, worker, feeds = fixtures.store, fixtures.worker, fixtures.feeds


@pytest.fixture
def indexing(monkeypatch):
    """Vector side effects are doubles; every SQL lock and guard is real."""
    qdrant = MagicMock()
    qdrant.upsert, qdrant.delete = AsyncMock(), AsyncMock()
    embed = AsyncMock()
    embed.embed.return_value = [0.1] * 1536
    monkeypatch.setattr(resource_indexer, "get_qdrant_client", lambda: qdrant)
    monkeypatch.setattr(
        resource_indexer,
        "resolve_context_routing",
        AsyncMock(return_value=("test-collection", embed)),
    )
    monkeypatch.setattr(
        resource_indexer.QuotaService,
        "check_memories_per_day",
        AsyncMock(return_value=(True, None)),
    )
    return qdrant


def _target(feeds):
    return UUID(feeds.target["resource_pk"]), UUID(feeds.target["context_id"])


async def _state(feeds, db) -> IndexerState:
    resource_pk, context_id = _target(feeds)
    return await db.scalar(
        select(IndexerState)
        .where(IndexerState.resource_pk == resource_pk, IndexerState.context_id == context_id)
        .execution_options(populate_existing=True)
    )


async def _maintain(feeds) -> None:
    """The tick's outbox replay, before any lock is taken: its IndexerState
    insert checks the Resource foreign key and would wait on the holder."""
    async with feeds.factory() as db:
        await maintain_sync_runs(db)


async def _run(feeds, *, maintain: bool = True):
    """One job run: the row's recorded failures in, the batch's metrics out."""
    if maintain:
        await _maintain(feeds)
    async with feeds.factory() as db:
        state = await _state(feeds, db)
        recorded = recorded_failed_events(state.metrics) if state is not None else []
        indexer = resource_indexer.ResourceIndexer(db)
        indexer._plan_suspended = AsyncMock(return_value=False)
        indexer._workspace_memory_room = AsyncMock(return_value=1000)
        metrics = await indexer.process_incremental(
            feeds.target["resource_id"], feeds_context(feeds), failed_events=recorded
        )
        # What the job does after a batch that is not held back.
        state = await _state(feeds, db)
        state.job_status, state.metrics = "idle", metrics.to_dict()
        await db.commit()
        return metrics


def feeds_context(feeds) -> UUID:
    return UUID(feeds.target["context_id"])


async def _events(feeds, *, after: int = 0) -> list[ResourceEvent]:
    resource_pk, _context_id = _target(feeds)
    async with feeds.factory() as db:
        return list(
            (
                await db.scalars(
                    select(ResourceEvent)
                    .where(ResourceEvent.resource_pk == resource_pk, ResourceEvent.id > after)
                    .order_by(ResourceEvent.id)
                )
            ).all()
        )


async def _offset(feeds) -> tuple[int, list]:
    async with feeds.factory() as db:
        state = await _state(feeds, db)
        return state.last_offset, recorded_failed_events(state.metrics)


async def _index_status(feeds, run_id: str) -> str:
    result = await service(feeds, "GET", f"runs/{run_id}")
    assert result.status_code == 200, result.text
    return result.json()["index_status"]


async def test_busy_resource_lock_keeps_the_cursor_before_the_first_blocked_event(feeds, indexing):
    """A worker's status poll holds the Resource row while the batch runs."""
    row = await committed(feeds, [product("one"), product("two"), product("three")])
    events = await _events(feeds)
    assert len(events) == 3
    resource_pk, _context_id = _target(feeds)
    await _maintain(feeds)
    async with feeds.factory() as holder:
        await holder.scalar(select(Resource).where(Resource.id == resource_pk).with_for_update())
        metrics = await _run(feeds, maintain=False)
        await holder.rollback()
    offset, failed = await _offset(feeds)
    assert offset < events[0].id
    assert failed == [] and metrics.failed_events == []
    assert metrics.applied_upserts == 0 and metrics.errors == 0
    assert await _index_status(feeds, row["run_id"]) == "pending"


async def test_busy_batch_that_applied_nothing_ends_cleanly_and_is_requeued(feeds, indexing):
    """No exception, a committed (empty) batch, and the outbox wakes the row again."""
    await committed(feeds, [product("one")])
    resource_pk, context_id = _target(feeds)
    await _maintain(feeds)
    async with feeds.factory() as holder:
        await holder.scalar(select(Resource).where(Resource.id == resource_pk).with_for_update())
        metrics = await _run(feeds, maintain=False)
        await holder.rollback()
    assert metrics.batch_committed and metrics.reason is None, metrics.to_dict()
    assert metrics.applied_upserts == metrics.applied_deletes == 0
    async with feeds.factory() as db:
        await maintain_sync_runs(db)
        state = await _state(feeds, db)
        assert state.job_status == "queued"
        outbox = await db.scalar(
            select(SyncIndexOutbox).where(
                SyncIndexOutbox.resource_pk == resource_pk,
                SyncIndexOutbox.context_id == context_id,
            )
        )
        assert outbox.delivered_at is None


@pytest.mark.parametrize(
    ("held", "applied_first"),
    [
        # Every event blocked: the Resource row a status poll holds.
        ("resource", 0),
        # Only the middle event blocked, so the one after it would apply: the
        # case where a cursor moving past the blocked event loses it.
        ("record", 1),
    ],
)
async def test_released_lock_lets_the_next_run_apply_every_event_and_report_complete(
    feeds, indexing, held, applied_first
):
    row = await committed(feeds, [product("one"), product("two"), product("three")])
    events = await _events(feeds)
    resource_pk, _context_id = _target(feeds)
    lock = {
        "resource": select(Resource).where(Resource.id == resource_pk),
        "record": select(SyncRecord).where(
            SyncRecord.resource_pk == events[1].resource_pk,
            SyncRecord.doc_id == events[1].doc_id,
        ),
    }[held]
    await _maintain(feeds)
    async with feeds.factory() as holder:
        await holder.scalar(lock.with_for_update())
        first = await _run(feeds, maintain=False)
        await holder.rollback()
    assert first.applied_upserts == applied_first and first.failed_events == []
    assert await _index_status(feeds, row["run_id"]) == "pending"
    metrics = await _run(feeds)
    assert metrics.applied_upserts == 3 - applied_first and metrics.failed_events == []
    offset, failed = await _offset(feeds)
    assert offset == events[-1].id and failed == []
    assert await _index_status(feeds, row["run_id"]) == "complete"


async def test_busy_lock_after_an_applied_prefix_commits_the_prefix_and_stops(feeds, indexing):
    """A later event's busy row ends the batch; the events before it stay applied."""
    await committed(feeds, [product("one"), product("two"), product("three")])
    events = await _events(feeds)
    await _maintain(feeds)
    async with feeds.factory() as holder:
        await holder.scalar(
            select(SyncRecord)
            .where(
                SyncRecord.resource_pk == events[1].resource_pk,
                SyncRecord.doc_id == events[1].doc_id,
            )
            .with_for_update()
        )
        metrics = await _run(feeds, maintain=False)
        await holder.rollback()
    assert metrics.batch_committed and metrics.applied_upserts == 1
    offset, failed = await _offset(feeds)
    assert offset == events[0].id
    assert failed == []
    async with feeds.factory() as db:
        docs = set(
            (
                await db.scalars(
                    select(Memory.resource_doc_id).where(
                        Memory.context_id == feeds_context(feeds), Memory.deleted_at.is_(None)
                    )
                )
            ).all()
        )
    assert docs == {events[0].doc_id}


async def test_deterministic_guard_conflict_is_still_recorded_and_the_batch_continues(
    feeds, indexing
):
    first = await committed(feeds, [product("one"), product("two")])
    await _run(feeds)
    assert await _index_status(feeds, first["run_id"]) == "complete"
    offset, _failed = await _offset(feeds)
    await committed(feeds, [product("one", title="one v2"), product("two", title="two v2")])
    events = await _events(feeds, after=offset)
    assert [event.op for event in events] == ["upsert", "upsert"]
    async with feeds.factory() as db:
        edited = await db.scalar(
            select(Memory).where(
                Memory.context_id == feeds_context(feeds),
                Memory.resource_doc_id == events[0].doc_id,
                Memory.deleted_at.is_(None),
            )
        )
        edited.content = json.dumps({"title": "independently edited"})
        await db.commit()
    metrics = await _run(feeds)
    assert metrics.applied_upserts == 1 and metrics.errors == 1
    offset, failed = await _offset(feeds)
    assert offset == events[1].id
    assert [(entry["event_id"], entry["error_class"]) for entry in failed] == [
        (events[0].id, "ConflictError")
    ]


async def test_busy_candidate_lock_on_a_delete_ends_the_batch_without_a_record(feeds, indexing):
    """A raw lock_not_available under the delete's QdrantError is a retry too."""
    await committed(feeds, [product("one")])
    await _run(feeds)
    offset, _failed = await _offset(feeds)
    row = await staged(feeds, [])
    body = {"diff_hash": row["diff_hash"]}
    approval = (await human(feeds, "POST", f"runs/{row['run_id']}/approvals", body=body)).json()
    result = await service(
        feeds,
        "POST",
        f"runs/{row['run_id']}/commit",
        body={**body, "approval_id": approval["approval_id"]},
    )
    assert result.status_code == 200, result.text
    events = await _events(feeds, after=offset)
    assert [event.op for event in events] == ["delete"] and events[0].version is not None
    await _maintain(feeds)
    async with feeds.factory() as holder:
        # KEY SHARE (a referencing row's lock) leaves the guard's FOR SHARE free
        # and blocks only the delete's own FOR UPDATE NOWAIT candidate lock.
        await holder.scalar(
            select(Memory)
            .where(
                Memory.context_id == feeds_context(feeds),
                Memory.resource_doc_id == events[0].doc_id,
            )
            .with_for_update(read=True, key_share=True)
        )
        metrics = await _run(feeds, maintain=False)
        await holder.rollback()
    assert metrics.applied_deletes == 0 and metrics.failed_events == []
    assert await _offset(feeds) == (offset, [])
    metrics = await _run(feeds)
    assert metrics.applied_deletes == 1 and metrics.failed_events == []
    assert await _offset(feeds) == (events[0].id, [])
    assert await _index_status(feeds, row["run_id"]) == "complete"


@pytest.mark.parametrize("held", ["resource", "context", "record", "receipt"])
async def test_guard_lock_collisions_raise_the_retryable_conflict(feeds, held):
    row = await committed(feeds, [product("one")])
    (event,) = await _events(feeds)
    resource_pk, context_id = _target(feeds)
    lock = {
        "resource": select(Resource).where(Resource.id == resource_pk),
        "context": select(Context).where(Context.id == context_id),
        "record": select(SyncRecord).where(
            SyncRecord.resource_pk == resource_pk, SyncRecord.doc_id == event.doc_id
        ),
        "receipt": select(SyncIndexOutbox).where(SyncIndexOutbox.run_id == UUID(row["run_id"])),
    }[held]
    async with feeds.factory() as holder, feeds.factory() as db:
        await holder.scalar(lock.with_for_update())
        context = await db.get(Context, context_id)
        event = await db.get(ResourceEvent, event.id)
        with pytest.raises(SyncLockBusyError) as busy:
            await guard_sync_event(db, event, context)
        assert busy.value.message == "Synchronization state is changing; retry"
        await db.rollback()
        await holder.rollback()


async def test_guard_memory_lock_collision_raises_the_retryable_conflict(feeds, indexing):
    await committed(feeds, [product("one")])
    await _run(feeds)
    offset, _failed = await _offset(feeds)
    await committed(feeds, [product("one", title="one v2")])
    (event,) = await _events(feeds, after=offset)
    async with feeds.factory() as holder, feeds.factory() as db:
        await holder.scalar(
            select(Memory)
            .where(
                Memory.context_id == feeds_context(feeds),
                Memory.resource_doc_id == event.doc_id,
            )
            .with_for_update()
        )
        context = await db.get(Context, feeds_context(feeds))
        event = await db.get(ResourceEvent, event.id)
        with pytest.raises(SyncLockBusyError):
            await guard_sync_event(db, event, context)
        await db.rollback()
        await holder.rollback()


async def test_target_fence_try_lock_collision_raises_the_retryable_conflict(feeds):
    context_id = feeds_context(feeds)
    async with feeds.factory() as holder, feeds.factory() as db:
        await holder.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": "sync-management:" + str(context_id)},
        )
        with pytest.raises(SyncLockBusyError) as busy:
            await target_fence(db, context_id)
        assert isinstance(busy.value, ConflictError)
        assert busy.value.message == "Feed management is changing; retry"
        await db.rollback()
        await holder.rollback()


async def test_http_callers_still_get_the_same_409_body(feeds):
    """The human records read reaches ``locked`` on the held Resource row."""
    await committed(feeds, [product("one")])
    resource_pk, _context_id = _target(feeds)
    async with feeds.factory() as holder:
        await holder.scalar(select(Resource).where(Resource.id == resource_pk).with_for_update())
        result = await human(feeds, "GET", "records")
        await holder.rollback()
    # The synchronization routes answer a conflict with its code alone (as
    # before this change); the subclass keeps the code.
    assert result.status_code == 409
    assert result.json() == {"error": "RES-002"}
