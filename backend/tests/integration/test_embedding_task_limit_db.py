"""#3034: a queued embedding task and the sweep against a real Postgres.

With every slot taken, a spawned task waits and its row stays ``pending``:

* when the process stops before the task gets a slot (the task is cancelled),
  the sweep completes the row;
* when the sweep takes the row while the task waits, the task's run finds the
  row claimed and embeds nothing: the provider is called once.

The real ``process_pending_embedding`` runs on its own sessions; the vector
store and the embedding provider are stubbed. The sweep's candidate list is
narrowed to this test's row (other rows of the table are left alone).
Skipped when Postgres is unreachable.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import ExitStack, contextmanager
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from config.settings import Settings
from models.memory import Memory
from utils.datetime import utcnow

pytestmark = pytest.mark.asyncio(loop_scope="session")

_USER = "embedding-task-limit-it"


def _maker(async_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture(loop_scope="session")
async def row(async_engine: AsyncEngine) -> AsyncIterator[Memory]:
    """A committed pending row old enough for the sweep (its 10 s threshold).

    Older than any row another file can leave behind: the sweep takes the
    oldest 20 candidates, so this row is always among them.
    """
    memory = Memory(
        id=uuid4(),
        workspace_id=uuid4(),
        user_id=_USER,
        summary="queued embedding",
        content="queued embedding",
        type="note",
        scope="working",
        client="test",
        created_at=utcnow() - timedelta(days=36500),
        embedding_status="pending",
    )
    async with _maker(async_engine)() as session:
        session.add(memory)
        await session.commit()
    yield memory
    async with _maker(async_engine)() as session:
        await session.execute(delete(Memory).where(Memory.user_id == _USER))
        await session.commit()


async def _status(async_engine: AsyncEngine, memory_id: UUID) -> str:
    async with _maker(async_engine)() as session:
        return (
            await session.execute(select(Memory.embedding_status).where(Memory.id == memory_id))
        ).scalar_one()


@contextmanager
def _pipeline(async_engine: AsyncEngine, only: UUID) -> Iterator[MagicMock]:
    """The real pipeline on fresh sessions with a stubbed provider and store.

    Yields the provider stub (its ``embed`` counts the embeddings). The
    module's ``process_pending_embedding`` runs the real pipeline for ``only``
    and nothing for any other id, so the sweep leaves rows other tests left
    behind alone.
    """
    from services import memory_service

    real = memory_service.process_pending_embedding
    maker = _maker(async_engine)
    sessions: list[AsyncSession] = []

    def _get_db() -> AsyncIterator[AsyncSession]:
        async def _gen() -> AsyncIterator[AsyncSession]:
            session = maker()
            sessions.append(session)
            try:
                yield session
            finally:
                await session.close()

        return _gen()

    async def _only_this_row(memory_id: UUID) -> None:
        if memory_id == only:
            await real(memory_id)

    provider = MagicMock(model="m")
    provider.embed = AsyncMock(return_value=[0.1])
    settings = Settings(_env_file=None, embedding_task_concurrency=1)
    with ExitStack() as stack:
        stack.enter_context(patch("db.base.get_db", side_effect=_get_db))
        stack.enter_context(patch("config.settings.get_settings", lambda: settings))
        stack.enter_context(patch("services.embedding_service.EmbeddingService", MagicMock()))
        stack.enter_context(
            patch(
                "services.context_routing.resolve_context_routing",
                AsyncMock(return_value=("kagura_memories", provider)),
            )
        )
        stack.enter_context(
            patch.object(memory_service, "process_pending_embedding", _only_this_row)
        )
        stack.enter_context(patch.object(memory_service, "add_memory_to_qdrant", AsyncMock()))
        stack.enter_context(
            patch.object(memory_service, "build_memory_point", return_value=({}, [], []))
        )
        stack.enter_context(patch.object(memory_service, "_create_knn_seed_edges", AsyncMock()))
        stack.enter_context(
            patch.object(memory_service, "_create_tag_cooccurrence_seed_edges", AsyncMock())
        )
        memory_service._reset_embedding_tasks_for_tests()
        yield provider
        memory_service._reset_embedding_tasks_for_tests()


async def _hold_the_only_slot() -> tuple[asyncio.Task[None], asyncio.Event]:
    """Spawn a task that keeps the single slot until the event is set."""
    from services import memory_service

    release = asyncio.Event()

    async def _blocker(memory_id: UUID) -> None:
        await release.wait()

    with patch.object(memory_service, "process_pending_embedding", _blocker):
        holder = memory_service.spawn_embedding_task(uuid4())
    for _ in range(10):
        await asyncio.sleep(0)
    return holder, release


async def test_a_task_that_never_got_a_slot_leaves_its_row_to_the_sweep(
    async_engine: AsyncEngine, row: Memory
) -> None:
    from services import memory_service
    from tasks.embedding_tasks import sweep_pending_embeddings

    with _pipeline(async_engine, row.id) as provider:
        holder, release = await _hold_the_only_slot()
        with patch.object(
            memory_service,
            "process_pending_embedding",
            AsyncMock(side_effect=AssertionError("the queued task must not run")),
        ):
            queued = memory_service.spawn_embedding_task(row.id)
        for _ in range(10):
            await asyncio.sleep(0)
        assert not queued.done(), "waits for the slot"
        assert await _status(async_engine, row.id) == "pending"

        # The process stops: the queued task is cancelled before it runs.
        queued.cancel()
        await asyncio.gather(queued, return_exceptions=True)
        assert await _status(async_engine, row.id) == "pending"

        await asyncio.wait_for(sweep_pending_embeddings(), timeout=30)
        release.set()
        await holder

    assert await _status(async_engine, row.id) == "success"
    assert provider.embed.await_count == 1


async def test_a_row_the_sweep_claimed_while_its_task_waited_is_embedded_once(
    async_engine: AsyncEngine, row: Memory
) -> None:
    from services import memory_service
    from tasks.embedding_tasks import sweep_pending_embeddings

    with _pipeline(async_engine, row.id) as provider:
        holder, release = await _hold_the_only_slot()
        # The request's task: the real pipeline, queued behind the holder.
        queued = memory_service.spawn_embedding_task(row.id)
        for _ in range(10):
            await asyncio.sleep(0)
        assert not queued.done()

        # The sweep takes the waiting row and embeds it (no slot needed).
        await asyncio.wait_for(sweep_pending_embeddings(), timeout=30)
        assert await _status(async_engine, row.id) == "success"

        # The queued task gets its slot: the claim finds nothing to do.
        release.set()
        await holder
        await asyncio.wait_for(queued, timeout=30)

    assert await _status(async_engine, row.id) == "success"
    assert provider.embed.await_count == 1, "embedded once, by the sweep"
