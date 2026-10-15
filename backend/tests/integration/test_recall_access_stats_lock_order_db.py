"""Recall's statistics writes never wait on a row lock (Issue #1985).

Two concurrent recalls whose pages overlap used to bump ``access_count`` row
by row in ranking order: one locked m1 then m2, the other m2 then m1, and
Postgres killed one of them with ``DeadlockDetectedError`` (503 DB-002).
``MemoryRepository.bulk_update_access_stats`` now locks the page in one
statement through an id-ordered ``FOR UPDATE SKIP LOCKED`` subquery: a row
another transaction holds is skipped, never waited for, so the write cannot
join a deadlock cycle. Recall's promotion does the same; Sleep's waits.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from models.memory import Memory
from models.schemas import MemoryResponse
from repositories.memory import AccessStats, MemoryRepository
from services.memory_service import MemoryService

pytestmark = pytest.mark.asyncio(loop_scope="session")

_WAIT_SECONDS = 10.0
_NO_WAIT_SECONDS = 3.0
_PAIRS = 20


@pytest_asyncio.fixture(loop_scope="session")
async def memory_pair(db_session: AsyncSession) -> AsyncIterator[tuple[UUID, UUID]]:
    """Two committed memories, returned as (lower id, higher id)."""
    user_id = f"lock-order-{uuid4().hex[:8]}"
    rows = [
        Memory(
            id=uuid4(),
            workspace_id=uuid4(),
            user_id=user_id,
            summary=f"lock order {i}",
            content="lock order content",
            type="note",
            scope="working",
            client="test",
            created_at=datetime.utcnow(),
        )
        for i in range(2)
    ]
    db_session.add_all(rows)
    await db_session.commit()
    low, high = sorted(row.id for row in rows)
    yield low, high
    await db_session.rollback()
    await db_session.execute(delete(Memory).where(Memory.user_id == user_id))
    await db_session.commit()


async def _pid(session: AsyncSession) -> int:
    return int(await session.scalar(text("SELECT pg_backend_pid()")) or 0)


async def _wait_until_blocked(probe: AsyncSession, waiter_pid: int, holder_pid: int) -> None:
    """Poll until ``waiter_pid`` is waiting on a lock ``holder_pid`` holds."""
    deadline = asyncio.get_running_loop().time() + _WAIT_SECONDS
    while True:
        blockers = await probe.scalar(text("SELECT pg_blocking_pids(:pid)"), {"pid": waiter_pid})
        await probe.rollback()
        if holder_pid in (blockers or []):
            return
        if asyncio.get_running_loop().time() > deadline:
            pytest.fail(f"pid {waiter_pid} never blocked on {holder_pid}: {blockers}")
        await asyncio.sleep(0.05)


async def _row(probe: AsyncSession, memory_id: UUID) -> tuple[int, list[str] | None]:
    probe.expire_all()
    row = (
        await probe.execute(
            select(Memory.access_count, Memory.accessed_by_clients).where(Memory.id == memory_id)
        )
    ).one()
    await probe.rollback()
    return row[0], row[1]


async def test_access_stats_skip_a_held_row_instead_of_waiting(
    async_engine: AsyncEngine, memory_pair: tuple[UUID, UUID]
) -> None:
    low, high = memory_pair
    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as holder, factory() as writer, factory() as probe:
        await holder.execute(text("SELECT id FROM memories WHERE id = :id FOR UPDATE"), {"id": low})
        try:
            # Ranking order puts the higher id first. The held row is skipped,
            # the free one bumped, and the call returns without waiting.
            counts = await asyncio.wait_for(
                MemoryRepository(writer).bulk_update_access_stats([high, low], client="api"),
                _NO_WAIT_SECONDS,
            )
            await writer.commit()
        finally:
            await holder.commit()

        assert {k: v.access_count for k, v in counts.items()} == {high: 1}
        assert await _row(probe, low) == (0, None)

        # Released: the next call bumps both. Same client again: the count
        # moves, the client list does not grow.
        again = await MemoryRepository(writer).bulk_update_access_stats([low, high], client="api")
        assert {k: v.access_count for k, v in again.items()} == {low: 1, high: 2}
        await writer.commit()
        assert await _row(probe, low) == (1, ["api"])
        assert await _row(probe, high) == (2, ["api"])


async def test_recall_promotion_skips_a_held_row_and_sleep_promotion_waits(
    async_engine: AsyncEngine, memory_pair: tuple[UUID, UUID]
) -> None:
    low, high = memory_pair
    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    async with (
        factory() as holder,
        factory() as writer,
        factory() as sleeper,
        factory() as probe,
    ):
        # The pid is read inside the transaction that will wait (a NullPool
        # session gets a new backend after each commit).
        holder_pid, sleeper_pid = await _pid(holder), await _pid(sleeper)
        await holder.execute(text("SELECT id FROM memories WHERE id = :id FOR UPDATE"), {"id": low})
        try:
            promoted = await asyncio.wait_for(
                MemoryRepository(writer).bulk_promote_to_persistent([high, low], skip_locked=True),
                _NO_WAIT_SECONDS,
            )
            await writer.commit()
            assert promoted == [high]

            # The authoritative (Sleep) variant waits for the held row.
            sleep_promote = asyncio.create_task(
                MemoryRepository(sleeper).bulk_promote_to_persistent([low])
            )
            await _wait_until_blocked(probe, sleeper_pid, holder_pid)
        finally:
            await holder.commit()
        assert await asyncio.wait_for(sleep_promote, _WAIT_SECONDS) == [low]
        await sleeper.commit()


async def test_reversed_pages_on_two_sessions_never_deadlock(
    async_engine: AsyncEngine, memory_pair: tuple[UUID, UUID]
) -> None:
    low, high = memory_pair
    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)

    async def bump(ids: list[UUID]) -> dict[UUID, AccessStats]:
        async with factory() as session:
            counts = await MemoryRepository(session).bulk_update_access_stats(ids, client="api")
            # Hold the locks across a scheduling point so the two sessions
            # really overlap.
            await asyncio.sleep(0)
            await session.commit()
            return counts

    results = await asyncio.wait_for(
        asyncio.gather(
            *(bump(ids) for _ in range(_PAIRS) for ids in ([low, high], [high, low])),
            return_exceptions=True,
        ),
        60,
    )

    errors = [r for r in results if isinstance(r, BaseException)]
    assert not [e for e in errors if isinstance(e, DBAPIError)], errors
    assert not errors, errors
    # Some bumps may be skipped under contention, but every bump a call
    # reports is in the row, and nothing else is.
    reported = [r for r in results if isinstance(r, dict)]
    async with factory() as probe:
        for memory_id in (low, high):
            expected = sum(1 for counts in reported if memory_id in counts)
            assert expected >= 1
            assert await _row(probe, memory_id) == (expected, ["api"])


async def test_recall_access_write_commits_on_an_isolated_session(
    db_session: AsyncSession, memory_pair: tuple[UUID, UUID]
) -> None:
    """The post-commit write lands in the database without touching the
    request session's transaction, and promotes a row that now qualifies."""
    low, high = memory_pair
    rows = {
        row.id: row
        for row in (await db_session.execute(select(Memory).where(Memory.id.in_([low, high]))))
        .scalars()
        .all()
    }
    rows[low].importance = 0.9  # qualifies for promotion on its next surfacing
    await db_session.commit()
    responses = [
        MemoryResponse(
            memory_id=row.id,
            summary=row.summary,
            context_summary=None,
            type=row.type,
            importance=row.importance,
            scope=row.scope,
            created_at=row.created_at,
            client=row.client,
            tags=[],
            context=None,
        )
        for row in rows.values()
    ]

    await MemoryService(db_session)._recall_record_access(
        responses, {str(k): v for k, v in rows.items()}, client="api"
    )

    assert {r.memory_id: r.scope for r in responses} == {low: "persistent", high: "working"}
    async with async_sessionmaker(db_session.bind, class_=AsyncSession)() as probe:
        assert await _row(probe, low) == (1, ["api"])
        assert await _row(probe, high) == (1, ["api"])
        scope = await probe.scalar(select(Memory.scope).where(Memory.id == low))
        assert scope == "persistent"
