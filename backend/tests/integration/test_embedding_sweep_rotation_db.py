"""Failed embedding rows take turns in the sweep by their last attempt (#2908).

The sweep takes 20 candidates a tick (every 30 s), pending and stale
processing rows first. A failed row is eligible again 60 s after its attempt.
A failure that keeps the retry budget (a configuration failure, a row of a
feed-managed target the pipeline refuses) leaves the row eligible for good.
Taken oldest-created first, enough of those rows filled every tick, and a
newer failed row that still had budget was never retried. Ordered by their
last attempt, never-attempted rows first, all of them rotate: every eligible
failed row is taken within a bounded number of ticks.

The real candidate query runs against PostgreSQL on one connection, inside a
transaction that is rolled back at the end. Every other row of the table is
hidden from the sweep in that transaction, so only this test's rows are
candidates and nothing it writes outlives it. ``process_pending_embedding``
is replaced by a stand-in that fails or completes each row as the scenario
needs. Skipped when PostgreSQL is unreachable.
"""

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from config.constants import EMBEDDING_RETRY_BACKOFF_SECONDS, MAX_EMBEDDING_RETRIES
from models.memory import Memory
from tasks.embedding_tasks import sweep_pending_embeddings
from utils.datetime import utcnow

pytestmark = pytest.mark.asyncio(loop_scope="session")

_USER = "embedding-sweep-rotation-it"
TICK = timedelta(seconds=30)  # the sweep's schedule
BATCH = 20  # candidates per tick
# Enough rows that keep their budget for BATCH of them to be eligible on every
# tick: a row is eligible again two ticks after its attempt.
ZERO_BUDGET = 3 * BATCH


@pytest_asyncio.fixture(loop_scope="session")
async def sessions(async_engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Sessions on one connection whose outer transaction is rolled back at the end.

    A session's commit releases a savepoint of that transaction. The rows
    other tests left in the table are suppressed in it first, so the sweep
    sees only the rows this test inserts.
    """
    async with async_engine.connect() as conn:
        outer = await conn.begin()
        try:
            await conn.execute(
                update(Memory)
                .where(Memory.embedding_suppressed_at.is_(None))
                .values(embedding_suppressed_at=utcnow())
            )
            yield async_sessionmaker(
                bind=conn,
                class_=AsyncSession,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            )
        finally:
            await outer.rollback()


def _memory(created_at: datetime, **embedding) -> Memory:
    return Memory(
        id=uuid4(),
        workspace_id=uuid4(),
        user_id=_USER,
        summary="sweep rotation",
        content="sweep rotation",
        type="note",
        scope="working",
        client="test",
        created_at=created_at,
        **embedding,
    )


async def _ticks(
    sessions: async_sessionmaker[AsyncSession],
    clock: list[datetime],
    count: int,
    succeed: set[UUID],
) -> list[list[UUID]]:
    """Run ``count`` sweep ticks, 30 s apart; the ids each tick handed to the pipeline.

    The stand-in completes the rows in ``succeed`` and fails every other one
    without touching its retry count, as a configuration failure or a refused
    feed-managed row does. It writes through the sweep's own session: a
    second session's savepoint would sit inside the sweep's open one and go
    with it when that one is rolled back at close.
    """
    claims: list[list[UUID]] = []
    current: list[AsyncSession] = []

    def get_db():
        async def one_session():
            session = sessions()
            current.append(session)
            yield session

        return one_session()

    async def process(memory_id: UUID) -> None:
        claims[-1].append(memory_id)
        db = current[-1]
        row = await db.get(Memory, memory_id)
        assert row is not None
        row.embedding_status = "success" if memory_id in succeed else "failed"
        row.embedding_attempted_at = clock[0]
        await db.commit()

    with (
        patch("db.base.get_db", side_effect=get_db),
        patch("utils.datetime.utcnow", side_effect=lambda: clock[0]),
        patch("services.memory_service.process_pending_embedding", new=process),
    ):
        for _ in range(count):
            claims.append([])
            await sweep_pending_embeddings()
            await current[-1].close()
            clock[0] += TICK
    return claims


async def test_a_newer_failed_row_is_retried_while_rows_that_keep_their_budget_fill_every_tick(
    sessions,
):
    assert (EMBEDDING_RETRY_BACKOFF_SECONDS, MAX_EMBEDDING_RETRIES) == (60, 3)
    t0 = utcnow()
    # Old rows whose failure never spends the budget, all eligible at the start.
    keep_budget = [
        _memory(
            t0 - timedelta(days=2) + timedelta(seconds=index),
            embedding_status="failed",
            embedding_retry_count=0,
            embedding_attempted_at=t0 - timedelta(hours=1),
            embedding_error="CFG-001: no embedding credential",
        )
        for index in range(ZERO_BUDGET)
    ]
    # A newer row that failed on a transient error and has budget left.
    transient = _memory(
        t0 - timedelta(hours=1),
        embedding_status="failed",
        embedding_retry_count=1,
        embedding_attempted_at=t0 - timedelta(minutes=10),
        embedding_error="provider timeout",
    )
    # A new memory waiting for its first embedding.
    pending = _memory(t0 - timedelta(minutes=1), embedding_status="pending")
    async with sessions() as db:
        db.add_all([*keep_budget, transient, pending])
        await db.commit()

    claims = await _ticks(sessions, [t0], 8, {transient.id, pending.id})

    assert all(len(ids) <= BATCH for ids in claims)
    # #979: a pending row still goes before every failed one.
    assert pending.id in claims[0]
    retried = [tick for tick, ids in enumerate(claims) if transient.id in ids]
    # Once round every eligible row (ZERO_BUDGET + 2 rows, BATCH a tick) and it is taken.
    assert retried and retried[0] <= -(-(ZERO_BUDGET + 2) // BATCH), claims
    # The rows that keep their budget rotate too: none is left out.
    assert {row.id for row in keep_budget} <= {mid for ids in claims for mid in ids}
