"""Point deletes after the commit, against a real Postgres (#2480).

``forget_bulk``, ``cleanup_old_working_memories`` and ``rollback_session``
follow ``forget`` (#2201, #2476): each point is queued in
``deferred_point_deletes`` in the transaction that removes its row, and the
vector store is touched only after that commit, under the point-writer lock,
for points no live row names. A transaction that never commits therefore
leaves its rows live with their points and no queue row.

The vector store is stubbed (``delete_points_from_qdrant`` through the
session rollback module's ``_stubs``, ``delete_chunk_points`` here); the lock,
the live-owner look and the queue are real. Not executed in the local unit
run — needs the DB container.
"""

# ruff: noqa: F811 — the fixtures are shared with test_session_rollback_db
from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import delete, func, select

from models.deferred_point_delete import DeferredPointDelete
from models.memory import Memory
from services import session_rollback as sr
from services.memory_service import MemoryService
from services.write_provenance import SessionPrincipal
from tests.integration.test_session_rollback_db import (  # noqa: F401 — fixtures
    World,
    _rollback,
    _stubs,
    world,
)
from utils.datetime import utcnow

pytestmark = pytest.mark.asyncio(loop_scope="session")

_COLLECTION = "kagura_memories_post_commit_it"


@pytest.fixture
def chunks() -> Iterator[AsyncMock]:
    with patch("db.qdrant.delete_chunk_points", AsyncMock()) as mock:
        yield mock


async def _queued(world: World, ids: list[uuid.UUID]) -> set[uuid.UUID]:
    return set(
        (
            await world.session.execute(
                select(DeferredPointDelete.memory_id).where(DeferredPointDelete.memory_id.in_(ids))
            )
        ).scalars()
    )


async def _live(world: World, ids: list[uuid.UUID]) -> int:
    return (
        await world.session.execute(
            select(func.count()).where(Memory.id.in_(ids), Memory.deleted_at.is_(None))
        )
    ).scalar_one()


async def _exist(world: World, ids: list[uuid.UUID]) -> int:
    return (
        await world.session.execute(select(func.count()).where(Memory.id.in_(ids)))
    ).scalar_one()


def _deleted_points(points: AsyncMock) -> set[str]:
    return {p for call in points.await_args_list for p in call.args[0]}


# ------------------------------------------------------------------ forget_bulk


def _bulk_service(world: World) -> MemoryService:
    service = MemoryService(world.session)
    # The context gate is forget's (tests/services/test_forget_bulk.py).
    service._get_context_isolation_params = AsyncMock(  # type: ignore[method-assign]
        return_value=(MagicMock(), str(world.workspace_id), str(world.context_id))
    )
    return service


def _bulk_patches() -> Any:
    return (
        patch(
            "services.memory_service.resolve_collection_name",
            AsyncMock(return_value=_COLLECTION),
        ),
        patch("services.memory_access_event_writer.emit_memory_access_event", AsyncMock()),
    )


async def test_forget_bulk_whose_commit_fails_keeps_the_rows_and_their_points(
    world: World, _stubs: dict[str, Any], chunks: AsyncMock
) -> None:
    ids = [await world.create(world.owner, None) for _ in range(3)]
    await world.session.commit()
    service = _bulk_service(world)
    collection, audit = _bulk_patches()
    with (
        collection,
        audit,
        patch.object(world.session, "commit", AsyncMock(side_effect=RuntimeError("commit failed"))),
        pytest.raises(RuntimeError, match="commit failed"),
    ):
        await service.forget_bulk(world.owner, context_id=world.context_id, dry_run=False)

    # forget_bulk rolled the chunk back: the rows are live and keep their
    # points, and no queue row names them.
    assert await _live(world, ids) == 3
    _stubs["points"].assert_not_awaited()
    chunks.assert_not_awaited()
    assert await _queued(world, ids) == set()


async def test_forget_bulk_deletes_the_points_after_its_commits(
    world: World, _stubs: dict[str, Any], chunks: AsyncMock
) -> None:
    ids = [await world.create(world.owner, None) for _ in range(5)]
    await world.session.commit()
    service = _bulk_service(world)
    collection, audit = _bulk_patches()
    with collection, audit:
        count, remaining, _ = await service.forget_bulk(
            world.owner, context_id=world.context_id, dry_run=False, chunk_size=2
        )

    assert (count, remaining) == (5, False)
    assert await _live(world, ids) == 0
    # Once per request, after the three chunk commits (#2201's one lock).
    _stubs["points"].assert_awaited_once()
    assert _deleted_points(_stubs["points"]) == {str(i) for i in ids}
    assert _stubs["points"].await_args.args[1] == _COLLECTION
    chunks.assert_awaited_once()
    # The fast path dequeued what it handled, under the lock.
    assert await _queued(world, ids) == set()


async def test_forget_bulk_with_the_lock_taken_leaves_the_points_queued(
    world: World, _stubs: dict[str, Any], chunks: AsyncMock, async_engine: Any
) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from db.point_writer_lock import hold_point_writer_lock

    ids = [await world.create(world.owner, None) for _ in range(2)]
    await world.session.commit()
    service = _bulk_service(world)
    collection, audit = _bulk_patches()
    maker = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as writer:
        await hold_point_writer_lock(writer)
        with collection, audit:
            count, _, _ = await service.forget_bulk(
                world.owner, context_id=world.context_id, dry_run=False
            )
        await writer.rollback()

    assert count == 2
    assert await _live(world, ids) == 0
    _stubs["points"].assert_not_awaited()
    # The drain (#2476) deletes them once the writer is done.
    assert await _queued(world, ids) == set(ids)
    # No foreign key to memories: the rows outlive the workspace teardown.
    await world.session.execute(
        delete(DeferredPointDelete).where(DeferredPointDelete.memory_id.in_(ids))
    )
    await world.session.commit()


# ------------------------------------------------ cleanup_old_working_memories


async def _old_working(world: World, count: int) -> list[uuid.UUID]:
    ids = [uuid.uuid4() for _ in range(count)]
    world.session.add_all(
        [
            Memory(
                id=mid,
                user_id=world.owner,
                workspace_id=world.workspace_id,
                context_id=world.context_id,
                summary="old working memory",
                content="body",
                type="note",
                scope="working",
                importance=0.2,
                client="test",
                embedding_status="success",
                created_at=utcnow() - timedelta(days=40),
            )
            for mid in ids
        ]
    )
    await world.session.commit()
    return ids


async def test_cleanup_whose_commit_fails_keeps_the_rows_and_their_points(
    world: World, _stubs: dict[str, Any], chunks: AsyncMock
) -> None:
    ids = await _old_working(world, 2)
    service = MemoryService(world.session)
    with (
        patch(
            "services.memory_service.resolve_collection_name",
            AsyncMock(return_value=_COLLECTION),
        ),
        # The per-row delete the old order ran before the commit.
        patch("services.memory_service.delete_memory_from_qdrant", AsyncMock()) as one_point,
        patch.object(world.session, "commit", AsyncMock(side_effect=RuntimeError("commit failed"))),
        pytest.raises(RuntimeError, match="commit failed"),
    ):
        await service.cleanup_old_working_memories(world.owner)
    await world.session.rollback()

    assert await _exist(world, ids) == 2
    one_point.assert_not_awaited()
    _stubs["points"].assert_not_awaited()
    assert await _queued(world, ids) == set()


async def test_cleanup_deletes_the_points_after_its_commit(
    world: World, _stubs: dict[str, Any], chunks: AsyncMock
) -> None:
    ids = await _old_working(world, 2)
    service = MemoryService(world.session)
    with patch(
        "services.memory_service.resolve_collection_name",
        AsyncMock(return_value=_COLLECTION),
    ):
        assert await service.cleanup_old_working_memories(world.owner) == 2

    assert await _exist(world, ids) == 0
    assert _deleted_points(_stubs["points"]) == {str(i) for i in ids}
    # The queue has no foreign key to memories: the hard delete keeps it,
    # and the fast path dequeued it.
    assert await _queued(world, ids) == set()


# ------------------------------------------------------------ rollback_session


async def test_a_rollback_whose_commit_never_lands_keeps_the_rows_and_their_points(
    world: World, _stubs: dict[str, Any], chunks: AsyncMock
) -> None:
    w = world
    created = [await w.create(w.owner, "S2480") for _ in range(2)]
    await w.session.commit()

    result = await _rollback(w, "S2480", SessionPrincipal(w.owner, None), dry_run=False)
    assert {entry[0] for entry in result.forgotten_points} == set(created)
    # Queued in the rollback's own transaction, beside the tombstones.
    assert await _queued(w, created) == set(created)
    await w.session.rollback()  # the caller's commit never lands: no finish()

    assert await _live(w, created) == 2
    assert await _queued(w, created) == set()
    _stubs["points"].assert_not_awaited()


async def test_finish_deletes_the_rolled_back_rows_points_after_the_commit(
    world: World, _stubs: dict[str, Any], chunks: AsyncMock
) -> None:
    w = world
    created = [await w.create(w.owner, "S2480") for _ in range(2)]
    await w.session.commit()

    result = await _rollback(w, "S2480", SessionPrincipal(w.owner, None), dry_run=False)
    await w.session.commit()
    _stubs["points"].assert_not_awaited()
    assert await _queued(w, created) == set(created)

    await sr.finish(result, w.session)

    assert await _live(w, created) == 0
    assert _deleted_points(_stubs["points"]) == {str(i) for i in created}
    chunks.assert_awaited_once()
    assert set(chunks.await_args.args[0]) == set(created)
    assert await _queued(w, created) == set()


async def test_a_forget_chunk_rolled_back_to_its_savepoint_keeps_its_points(
    world: World, _stubs: dict[str, Any], chunks: AsyncMock
) -> None:
    """The chunk's SAVEPOINT rolls back after its forget ran: its rows are
    live again, so finish() must not delete their points."""
    w = world
    created = [await w.create(w.owner, "S2480") for _ in range(2)]
    await w.session.commit()
    real_mark = sr._mark_handled

    async def mark(db: Any, event_ids: list[int], rollback_id: int) -> None:
        if event_ids:
            raise RuntimeError("mark failed")
        await real_mark(db, event_ids, rollback_id)

    with patch.object(sr, "_mark_handled", mark):
        result = await _rollback(w, "S2480", SessionPrincipal(w.owner, None), dry_run=False)
    await w.session.commit()
    await sr.finish(result, w.session)

    assert result.summary["failed"] == 2 and result.summary["forgotten"] == 0
    assert result.forgotten_points == [] and result.forgotten_ids == []
    assert await _live(w, created) == 2
    _stubs["points"].assert_not_awaited()
    assert await _queued(w, created) == set()
