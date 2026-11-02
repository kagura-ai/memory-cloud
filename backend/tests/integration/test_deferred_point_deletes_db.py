"""forget's deferred point deletes against Postgres and a live Qdrant (#2476).

A point writer (the resource indexer for a whole batch) holds the point-writer
lock shared; ``forget`` does not wait for it and queues the point in
``deferred_point_deletes``; the drain deletes it once the writer is done,
without the orphan sweep. Skipped when Postgres (``db_session``) or
``QDRANT_URL`` is unreachable, like ``test_forget_batch_timing_db``, whose
fixtures this module reuses.
"""

# ruff: noqa: F811 — the ``world`` fixture is shared with test_forget_batch_timing_db
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from models.deferred_point_delete import DeferredPointDelete
from tests.integration.test_forget_batch_timing_db import (  # noqa: F401 — fixture
    World,
    _forget,
    _seed,
    world,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _clear_queue(maker) -> None:
    # Rows other modules left behind would be drained here too.
    async with maker() as s:
        await s.execute(delete(DeferredPointDelete))
        await s.commit()


async def _queued(maker, ids: list[uuid.UUID]) -> set[uuid.UUID]:
    async with maker() as s:
        return set(
            (
                await s.execute(
                    select(DeferredPointDelete.memory_id).where(
                        DeferredPointDelete.memory_id.in_(ids)
                    )
                )
            ).scalars()
        )


async def _points(world: World, ids: list[uuid.UUID]) -> set[str]:
    return {
        str(p.id) for p in await world.qdrant.retrieve(world.collection, ids=[str(i) for i in ids])
    }


async def _chunks(world: World) -> int:
    from qdrant_client.models import Filter, IsEmptyCondition, PayloadField

    counted = await world.qdrant.count(
        world.collection,
        count_filter=Filter(must_not=[IsEmptyCondition(is_empty=PayloadField(key="chunk_of"))]),
        exact=True,
    )
    return counted.count


async def _drain(maker, wait_seconds: float):
    from services.deferred_point_deletes import drain_deferred_point_deletes

    async with maker() as s:
        return await drain_deferred_point_deletes(s, wait_seconds=wait_seconds)


@pytest.fixture
def maker(async_engine):
    return async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)


async def test_a_forget_during_a_writer_batch_is_drained_after_it(world: World, maker):
    """The issue's case: forget while a point writer is mid-transaction. The
    points stay and are queued; once the writer ends, the drain deletes them
    and their chunks and empties the queue — no sweep involved."""
    from db.point_writer_lock import hold_point_writer_lock

    await _clear_queue(maker)
    ids = await _seed(world, 2)
    async with maker() as writer:
        await hold_point_writer_lock(writer)
        body, _ = await _forget(world, ids)

        assert [r["status"] for r in body["results"]] == ["deleted", "deleted"]
        assert await _points(world, ids) == {str(i) for i in ids}
        assert await _queued(maker, ids) == set(ids)
        await writer.rollback()

    result = await _drain(maker, wait_seconds=2)

    assert (result.drained, result.kept, result.busy, result.failed) == (2, 0, False, None)
    assert await _points(world, ids) == set()
    assert await _chunks(world) == 0
    assert await _queued(maker, ids) == set()


async def test_the_drain_waits_for_a_writer_that_finishes(world: World, maker):
    from db.point_writer_lock import hold_point_writer_lock

    await _clear_queue(maker)
    ids = await _seed(world, 1)
    async with maker() as writer:
        await hold_point_writer_lock(writer)
        await _forget(world, ids)
        draining = asyncio.ensure_future(_drain(maker, wait_seconds=10))
        try:
            done, _ = await asyncio.wait({draining}, timeout=1.0)
            assert not done, "the drain deleted while a writer was mid-transaction"
            await writer.rollback()
            result = await asyncio.wait_for(draining, timeout=10)
        finally:
            draining.cancel()

    assert (result.drained, result.busy) == (1, False)
    assert await _points(world, ids) == set()
    assert await _queued(maker, ids) == set()


async def test_a_writer_past_the_wait_keeps_the_rows_and_the_session_usable(world: World, maker):
    from db.point_writer_lock import hold_point_writer_lock
    from services.deferred_point_deletes import drain_deferred_point_deletes

    await _clear_queue(maker)
    ids = await _seed(world, 1)
    async with maker() as writer:
        await hold_point_writer_lock(writer)
        try:
            await _forget(world, ids)
            async with maker() as s:
                result = await drain_deferred_point_deletes(s, wait_seconds=0.3)
                # Rolled back on the timeout, not left aborted.
                assert (await s.execute(select(func.count(DeferredPointDelete.id)))).scalar_one()
        finally:
            await writer.rollback()

    assert (result.busy, result.pending, result.drained) == (True, 1, 0)
    assert await _points(world, ids) == {str(ids[0])}
    assert await _queued(maker, ids) == set(ids)

    # The next run, with the writer gone, finishes the job.
    assert (await _drain(maker, wait_seconds=2)).drained == 1
    assert await _points(world, ids) == set()


async def test_a_row_restored_before_the_drain_keeps_its_point_and_leaves_the_queue(
    world: World, maker
):
    """A restore (session rollback / Sleep undo) under the shared lock between
    the forget and the drain: the live owner keeps the point; its queue row is
    handled, not re-checked forever."""
    from db.point_writer_lock import hold_point_writer_lock
    from models.memory import Memory

    await _clear_queue(maker)
    ids = await _seed(world, 2)
    restored = ids[0]
    async with maker() as writer:
        await hold_point_writer_lock(writer)
        await _forget(world, ids)
        await writer.execute(
            update(Memory).where(Memory.id == restored).values(deleted_at=None, deleted_by=None)
        )
        await writer.commit()

    result = await _drain(maker, wait_seconds=2)

    assert (result.drained, result.kept) == (1, 1)
    assert await _points(world, ids) == {str(restored)}
    assert await _queued(maker, ids) == set()


async def test_with_the_lock_free_the_fast_path_leaves_no_queue_row(world: World, maker):
    await _clear_queue(maker)
    ids = await _seed(world, 3)

    body, _ = await _forget(world, ids)

    assert body["deleted_count"] == 3
    assert await _points(world, ids) == set()
    assert await _queued(maker, ids) == set()


async def test_a_requeued_row_survives_a_drain_that_read_the_old_version(world: World, maker):
    """Restore and forget again after a reader saw the row live: the reader's
    dequeue matches the old version only, so the new forget's row stays."""
    from services.deferred_point_deletes import (
        QueuedRow,
        dequeue,
        queue_point_deletes,
        queued_rows_for,
    )

    await _clear_queue(maker)
    entry = (uuid.uuid4(), uuid.uuid4(), world.collection)
    async with maker() as s:
        await queue_point_deletes(s, [entry])
        await s.commit()
    async with maker() as reader:
        old: list[QueuedRow] = await queued_rows_for(reader, [entry])
        async with maker() as forget_again:
            await queue_point_deletes(forget_again, [entry])
            await forget_again.commit()
        await dequeue(reader, old)
        await reader.commit()

    assert await _queued(maker, [entry[0]]) == {entry[0]}
    await _clear_queue(maker)
