"""Access-stat row locks are taken in id order (Issue #1985).

Two concurrent recalls whose pages overlap used to bump ``access_count`` row
by row in ranking order: one locked m1 then m2, the other m2 then m1, and
Postgres killed one of them with ``DeadlockDetectedError`` (503 DB-002).
``MemoryRepository.bulk_update_access_stats`` locks the whole page in one
statement through an ordered ``FOR UPDATE`` subquery, so a second caller
queues on the lowest shared row and holds nothing above it while it waits.
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
from repositories.memory import MemoryRepository
from services.memory_service import MemoryService

pytestmark = pytest.mark.asyncio(loop_scope="session")

_WAIT_SECONDS = 10.0
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


async def test_waiter_holds_no_lock_above_the_contended_row(
    async_engine: AsyncEngine, memory_pair: tuple[UUID, UUID]
) -> None:
    low, high = memory_pair
    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as holder, factory() as waiter, factory() as probe:
        holder_pid, waiter_pid = await _pid(holder), await _pid(waiter)
        await holder.execute(text("SELECT id FROM memories WHERE id = :id FOR UPDATE"), {"id": low})

        # Ranking order puts the higher id first; the statement must still
        # lock the lower id first.
        bulk = asyncio.create_task(
            MemoryRepository(waiter).bulk_update_access_stats([high, low], client="api")
        )
        try:
            await _wait_until_blocked(probe, waiter_pid, holder_pid)

            # While waiting on ``low``, the waiter must not hold ``high``:
            # another transaction can lock it without waiting.
            await probe.execute(
                text("SELECT id FROM memories WHERE id = :id FOR UPDATE NOWAIT"), {"id": high}
            )
            await probe.rollback()
        finally:
            await holder.commit()
        counts = await asyncio.wait_for(bulk, _WAIT_SECONDS)
        await waiter.commit()

        assert counts == {low: 1, high: 1}
        # Same client again: the count moves, the client list does not grow.
        await MemoryRepository(waiter).bulk_update_access_stats([low, high], client="api")
        await waiter.commit()
        assert await _row(probe, low) == (2, ["api"])
        assert await _row(probe, high) == (2, ["api"])


async def test_reversed_pages_on_two_sessions_never_deadlock(
    async_engine: AsyncEngine, memory_pair: tuple[UUID, UUID]
) -> None:
    low, high = memory_pair
    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)

    async def bump(ids: list[UUID]) -> None:
        async with factory() as session:
            await MemoryRepository(session).bulk_update_access_stats(ids, client="api")
            # Hold the locks across a scheduling point so the two sessions
            # really overlap.
            await asyncio.sleep(0)
            await session.commit()

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
    async with factory() as probe:
        assert await _row(probe, low) == (2 * _PAIRS, ["api"])
        assert await _row(probe, high) == (2 * _PAIRS, ["api"])


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
