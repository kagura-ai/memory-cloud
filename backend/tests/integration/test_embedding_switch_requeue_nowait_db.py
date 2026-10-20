"""The embedding switch's re-queue never waits on a memory row (#2156).

The switch holds the point-writer advisory lock exclusively while it
re-queues. A session rollback locks its batch rows ``FOR UPDATE`` first and
asks for the same lock *shared* later, per un-forget item: if the switch's
re-queue then waited on one of those rows, each side waited on the other
(40P01 for whichever the deadlock detector picked). The re-queue locks its
rows ``FOR NO KEY UPDATE NOWAIT`` instead: a held row refuses the switch at
once with :class:`EmbeddingSwitchBusyError`, the switch rolls back (route
unchanged, advisory lock released) and the other transaction goes on.

The real ``wait_for_point_writers`` runs, with the 30 s wait cut to 2 s.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from db.point_writer_lock import wait_for_point_writers
from models.auth import Context, User, Workspace, WorkspaceMember, WorkspaceRole
from models.config import ContextSearchConfig
from models.memory import Memory
from services import embedding_migration_service as migration
from services.embedding_migration_service import EmbeddingSwitchBusyError
from utils.datetime import utcnow

OLD_MODEL = "text-embedding-3-small"
OLD_DIMENSIONS = 1536
NEW_MODEL = "qwen3-embedding:4b"
NEW_DIMENSIONS = 2560
TIMEOUT = 10
POLL_SECONDS = 0.05
_ROW_REFUSAL = r"^Another transaction holds memories .*; the context route was not changed\.$"

_LOCK_ROW = text("SELECT id FROM memories WHERE id = :id FOR UPDATE")
_KEY_SHARE_ROW = text("SELECT id FROM memories WHERE id = :id FOR KEY SHARE")
_SHARED_POINT_WRITER_LOCK = text(
    "SELECT pg_advisory_xact_lock_shared(hashtextextended('orphan_vector_sweep:point_writers', 0))"
)
_WAITING_ON_ADVISORY = text(
    "SELECT EXISTS (SELECT 1 FROM pg_locks "
    "WHERE pid = :pid AND locktype = 'advisory' AND NOT granted)"
)


@dataclass
class _Seeded:
    maker: async_sessionmaker[AsyncSession]
    context_id: UUID
    memory_id: UUID
    requeue_since: datetime


@pytest.fixture
async def seeded(async_engine: AsyncEngine) -> AsyncIterator[_Seeded]:
    """A context routed to OLD_MODEL with one live, embedded memory the
    switch must re-queue (written a day ago, ``requeue_since`` an hour before)."""
    maker = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    owner = f"switch-nowait-{uuid4().hex}"
    workspace_id = uuid4()
    context_id = uuid4()
    memory_id = uuid4()
    written_at = utcnow() - timedelta(days=1)

    async with maker() as seed:
        seed.add(User(email=f"{owner}@test.example", user_id=owner, role="user"))
        await seed.flush()
        seed.add(
            Workspace(
                id=workspace_id,
                name=f"nowait-ws-{uuid4().hex[:8]}",
                plan_name="pro",
                owner_user_id=owner,
                daily_api_limit=5000,
                weekly_api_limit=25000,
            )
        )
        await seed.flush()
        seed.add(
            WorkspaceMember(workspace_id=workspace_id, user_id=owner, role=WorkspaceRole.OWNER)
        )
        seed.add(
            Context(
                id=context_id,
                workspace_id=workspace_id,
                name=f"nowait-ctx-{uuid4().hex[:8]}",
                created_by=owner,
                is_private=False,
                trust_tier="trusted",
            )
        )
        seed.add(
            ContextSearchConfig(
                context_id=context_id,
                embedding_model=OLD_MODEL,
                embedding_dimensions=OLD_DIMENSIONS,
            )
        )
        seed.add(
            Memory(
                id=memory_id,
                workspace_id=workspace_id,
                context_id=context_id,
                user_id=owner,
                summary="switch re-queue nowait regression",
                content="switch re-queue nowait regression",
                type="note",
                client="pytest",
                scope="working",
                embedding_status="success",
                embedding_attempted_at=written_at,
                created_at=written_at,
            )
        )
        await seed.commit()

    try:
        yield _Seeded(maker, context_id, memory_id, written_at - timedelta(hours=1))
    finally:
        async with maker() as cleanup:
            await cleanup.execute(delete(Memory).where(Memory.id == memory_id))
            await cleanup.execute(
                delete(ContextSearchConfig).where(ContextSearchConfig.context_id == context_id)
            )
            await cleanup.execute(
                delete(WorkspaceMember).where(WorkspaceMember.workspace_id == workspace_id)
            )
            await cleanup.execute(delete(Context).where(Context.id == context_id))
            await cleanup.execute(delete(Workspace).where(Workspace.id == workspace_id))
            await cleanup.execute(delete(User).where(User.user_id == owner))
            await cleanup.commit()


async def _switch(seeded: _Seeded) -> migration.SwitchResult:
    async with seeded.maker() as db:
        return await migration.switch_context_embedding(
            db,
            seeded.context_id,
            NEW_MODEL,
            NEW_DIMENSIONS,
            requeue_since=seeded.requeue_since,
        )


async def _assert_untouched(seeded: _Seeded) -> None:
    async with seeded.maker() as check:
        config = (
            await check.execute(
                select(ContextSearchConfig).where(
                    ContextSearchConfig.context_id == seeded.context_id
                )
            )
        ).scalar_one()
        memory = (
            await check.execute(select(Memory).where(Memory.id == seeded.memory_id))
        ).scalar_one()
    assert config.embedding_model == OLD_MODEL
    assert config.embedding_dimensions == OLD_DIMENSIONS
    assert config.embedding_switched_at is None
    assert memory.embedding_status == "success"


async def _wait_until_waiting_on_advisory(seeded: _Seeded, pid: int) -> None:
    async with seeded.maker() as probe:
        for _ in range(int(TIMEOUT / POLL_SECONDS)):
            if await probe.scalar(_WAITING_ON_ADVISORY, {"pid": pid}):
                return
            await probe.rollback()  # a fresh snapshot of pg_locks each round
            await asyncio.sleep(POLL_SECONDS)
    pytest.fail("the holder never queued for the shared point-writer lock")


async def _cancel(task: asyncio.Task[object] | None) -> None:
    if task is not None and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.fixture
def _no_vector_store():
    """The switch reaches neither Qdrant path here (nothing was forgotten);
    patched so a regression cannot reach out of the test."""
    with (
        patch.object(migration, "POINT_WRITER_WAIT_SECONDS", 2.0),
        patch.object(migration, "delete_points_from_qdrant", new=AsyncMock()),
        patch.object(migration, "delete_chunk_points", new=AsyncMock()),
    ):
        yield


@pytest.mark.usefixtures("_no_vector_store")
async def test_switch_refuses_instead_of_deadlocking_with_a_session_rollback(
    seeded: _Seeded,
) -> None:
    """The pre-#2156 cycle: the holder has a row FOR UPDATE (session rollback's
    ``_read_batch``) and asks for the shared point-writer lock while the switch
    holds it exclusively and is about to re-queue that row."""
    holder = seeded.maker()
    switch_task: asyncio.Task[migration.SwitchResult] | None = None
    shared_task: asyncio.Task[object] | None = None
    switch_holds_exclusive = asyncio.Event()
    holder_queued = asyncio.Event()
    try:
        await holder.execute(_LOCK_ROW, {"id": seeded.memory_id})
        holder_pid = int(await holder.scalar(text("SELECT pg_backend_pid()")) or 0)

        async def exclusive_then_let_the_holder_queue(
            db: AsyncSession, *, timeout_seconds: float
        ) -> bool:
            # The real bounded wait; then hold the exclusive lock until the
            # holder is queued behind it, so the re-queue runs in the cycle.
            acquired = await wait_for_point_writers(db, timeout_seconds=timeout_seconds)
            if acquired:
                switch_holds_exclusive.set()
                await asyncio.wait_for(holder_queued.wait(), TIMEOUT)
            return acquired

        with patch.object(
            migration, "wait_for_point_writers", new=exclusive_then_let_the_holder_queue
        ):
            switch_task = asyncio.create_task(_switch(seeded))
            # Only once the switch holds the lock exclusively: the holder's
            # shared request then queues behind it.
            await asyncio.wait_for(switch_holds_exclusive.wait(), TIMEOUT)
            shared_task = asyncio.create_task(holder.execute(_SHARED_POINT_WRITER_LOCK))
            await _wait_until_waiting_on_advisory(seeded, holder_pid)
            holder_queued.set()

            # The re-queue's refusal, not the advisory wait's.
            with pytest.raises(EmbeddingSwitchBusyError, match=_ROW_REFUSAL):
                await asyncio.wait_for(switch_task, TIMEOUT)

        # The refusal rolled the switch back, releasing its exclusive lock.
        await asyncio.wait_for(shared_task, TIMEOUT)
        await holder.rollback()
        await _assert_untouched(seeded)
    finally:
        holder_queued.set()
        await holder.close()  # releases the row (and any lock) first
        await _cancel(switch_task)
        await _cancel(shared_task)


@pytest.mark.usefixtures("_no_vector_store")
async def test_switch_refuses_when_an_ordinary_writer_holds_a_row(seeded: _Seeded) -> None:
    """A row held without the advisory lock (an API write): refused at once,
    not after waiting for it."""
    holder = seeded.maker()
    switch_task: asyncio.Task[migration.SwitchResult] | None = None
    try:
        await holder.execute(_LOCK_ROW, {"id": seeded.memory_id})
        switch_task = asyncio.create_task(_switch(seeded))
        with pytest.raises(EmbeddingSwitchBusyError, match=_ROW_REFUSAL):
            await asyncio.wait_for(switch_task, TIMEOUT)
        await _assert_untouched(seeded)
        # The holder's transaction was never disturbed.
        await holder.execute(_SHARED_POINT_WRITER_LOCK)
        await holder.rollback()
    finally:
        await holder.close()
        await _cancel(switch_task)


@pytest.mark.usefixtures("_no_vector_store")
async def test_a_foreign_key_share_lock_does_not_refuse_the_switch(seeded: _Seeded) -> None:
    """FOR NO KEY UPDATE, not FOR UPDATE: an FK insert's KEY SHARE on the
    memory (a revision or chunk row being written) is compatible."""
    holder = seeded.maker()
    try:
        await holder.execute(_KEY_SHARE_ROW, {"id": seeded.memory_id})
        result = await asyncio.wait_for(_switch(seeded), TIMEOUT)
        await holder.rollback()
    finally:
        await holder.close()

    assert result.requeued == 1
    async with seeded.maker() as check:
        config = (
            await check.execute(
                select(ContextSearchConfig).where(
                    ContextSearchConfig.context_id == seeded.context_id
                )
            )
        ).scalar_one()
        memory = (
            await check.execute(select(Memory).where(Memory.id == seeded.memory_id))
        ).scalar_one()
    assert config.embedding_model == NEW_MODEL
    assert config.embedding_switched_at is not None
    assert memory.embedding_status == "pending"
