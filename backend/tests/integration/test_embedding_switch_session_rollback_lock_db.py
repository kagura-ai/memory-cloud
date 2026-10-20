"""A session rollback and the embedding switch share the point-writer lock (#2157).

1. ``rollback_session`` un-forgets two memories; the first item's re-embed
   fails and its SAVEPOINT is rolled back. The switch, started in the gap
   before the second item writes, must wait: the rollback took the lock in the
   outer transaction, so the failed item's ``ROLLBACK TO SAVEPOINT`` did not
   release it, and the second item restores under the route it resolved once.
   After the rollback commits, the switch flips the route and re-queues the
   restored row.
2. A shared holder keeps the switch out: with a short wait the switch is
   refused through ``wait_for_point_writers``'s ``lock_timeout`` path, the
   route is unchanged, and the same session can run the switch afterwards.

Needs the DB container (private ``TEST_DATABASE_URL``); the vector store, the
embedding service and the permission check are stubbed.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from db.point_writer_lock import hold_point_writer_lock
from db.qdrant import get_collection_name
from models.auth import Context, User, Workspace, WorkspaceMember, WorkspaceRole
from models.config import ContextSearchConfig
from models.memory import Memory, MemoryRevision, MemorySessionRollback
from services import embedding_migration_service as migration
from services import session_rollback as sr
from services.write_provenance import SessionPrincipal
from utils.datetime import utcnow
from utils.exceptions import ValidationError

pytestmark = pytest.mark.asyncio(loop_scope="session")

OLD_MODEL = "text-embedding-3-small"
OLD_DIMENSIONS = 1536
NEW_MODEL = "qwen3-embedding:4b"
NEW_DIMENSIONS = 2560
SESSION_ID = "S-lock"
TIMEOUT = 10
LOCK_WAIT = 2

# The point-writer key only (db/point_writer_lock.py): a bigint advisory key
# shows up in pg_locks as classid = high 32 bits, objid = low 32 bits.
_WAITING_POINT_WRITER_EXCLUSIVE = text(
    "SELECT count(*) FROM pg_locks, "
    "(SELECT hashtextextended(:key, 0) AS k) AS key "
    "WHERE locktype = 'advisory' AND objsubid = 1 "
    "AND classid::bigint = ((key.k >> 32) & 4294967295) "
    "AND objid::bigint = (key.k & 4294967295) "
    "AND mode = 'ExclusiveLock' AND NOT granted"
).bindparams(key="orphan_vector_sweep:point_writers")


def _maker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def _seed_context(
    maker: async_sessionmaker[AsyncSession], owner: str, workspace_id: UUID, context_id: UUID
) -> None:
    async with maker() as seed:
        seed.add(User(email=f"{owner}@test.example", user_id=owner, role="user"))
        await seed.flush()
        seed.add(
            Workspace(
                id=workspace_id,
                name=f"rollback-ws-{uuid4().hex[:8]}",
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
                name=f"rollback-ctx-{uuid4().hex[:8]}",
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
        await seed.commit()


async def _seed_forgotten(
    maker: async_sessionmaker[AsyncSession],
    owner: str,
    workspace_id: UUID,
    context_id: UUID,
    memory_ids: list[UUID],
) -> None:
    """Live rows the session ``SESSION_ID`` forgot, one ``forgotten`` event each.

    The events are inserted in ``memory_ids`` order, so their ids ascend and
    ``_read_batch`` (newest event first) plans the last memory first.
    """
    written_at = utcnow() - timedelta(days=7)
    deleted_at = written_at + timedelta(days=1)
    async with maker() as seed:
        for memory_id in memory_ids:
            seed.add(
                Memory(
                    id=memory_id,
                    workspace_id=workspace_id,
                    context_id=context_id,
                    user_id=owner,
                    summary="session rollback lock regression",
                    content="session rollback lock regression",
                    type="note",
                    client="pytest",
                    scope="working",
                    version=1,
                    embedding_status="success",
                    embedding_attempted_at=written_at,
                    created_at=written_at,
                    deleted_at=deleted_at,
                    deleted_by=owner,
                    session_id=SESSION_ID,
                )
            )
        await seed.flush()
        for memory_id in memory_ids:
            seed.add(
                MemoryRevision(
                    memory_id=memory_id,
                    context_id=context_id,
                    version=1,
                    kind="forgotten",
                    writer="api",
                    actor_user_id=owner,
                    agent_id=None,
                    session_id=SESSION_ID,
                    at=deleted_at,
                )
            )
            await seed.flush()
        await seed.commit()


async def _cleanup(
    maker: async_sessionmaker[AsyncSession], owner: str, workspace_id: UUID, context_id: UUID
) -> None:
    async with maker() as cleanup:
        await cleanup.execute(
            delete(MemorySessionRollback).where(MemorySessionRollback.context_id == context_id)
        )
        await cleanup.execute(delete(MemoryRevision).where(MemoryRevision.context_id == context_id))
        await cleanup.execute(delete(Memory).where(Memory.context_id == context_id))
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


async def _config_model(maker: async_sessionmaker[AsyncSession], context_id: UUID) -> str:
    async with maker() as db:
        config = (
            await db.execute(
                select(ContextSearchConfig).where(ContextSearchConfig.context_id == context_id)
            )
        ).scalar_one()
    return config.embedding_model


async def _memory(maker: async_sessionmaker[AsyncSession], memory_id: UUID) -> Memory:
    async with maker() as db:
        return (await db.execute(select(Memory).where(Memory.id == memory_id))).scalar_one()


async def _wait_until_switch_blocks(
    maker: async_sessionmaker[AsyncSession], switch_task: asyncio.Task[Any]
) -> bool:
    """True once the switch waits for the point-writer lock; False if it finished first."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + TIMEOUT
    async with maker() as probe:
        while loop.time() < deadline:
            if switch_task.done():
                return False
            waiting = (await probe.execute(_WAITING_POINT_WRITER_EXCLUSIVE)).scalar_one()
            await probe.rollback()
            if waiting:
                return True
            await asyncio.sleep(0.05)
    pytest.fail("switch neither blocked on the point-writer lock nor completed")


async def test_switch_waits_for_a_session_rollback_whose_first_item_failed(
    async_engine: AsyncEngine,
) -> None:
    maker = _maker(async_engine)
    owner = f"rollback-lock-{uuid4().hex}"
    workspace_id = uuid4()
    context_id = uuid4()
    first_id = uuid4()
    second_id = uuid4()
    requeue_since = utcnow() - timedelta(seconds=1)
    restored_collection: list[str] = []
    gap_reached = asyncio.Event()
    release_gap = asyncio.Event()
    rollback_task: asyncio.Task[sr.RollbackResult] | None = None
    switch_task: asyncio.Task[Any] | None = None

    rollback_db = maker()

    async def fake_reembed(
        memory: Memory,
        user_id: str,
        embedding_svc: object,
        workspace_id: str,
        context_id: str,
        collection_name: str,
        *,
        db: AsyncSession,
    ) -> None:
        if memory.id == second_id:
            raise ConnectionError("vector store down")
        restored_collection.append(collection_name)
        await db.execute(
            update(Memory).where(Memory.id == memory.id).values(embedding_status="success")
        )

    original_restore = sr._Executor._restore

    async def paused_restore(self: sr._Executor, items: list[sr.PlanItem]) -> None:
        # After item 1's ROLLBACK TO SAVEPOINT, before item 2 writes.
        if items[0].memory_id == first_id:
            gap_reached.set()
            await release_gap.wait()
        await original_restore(self, items)

    async def run_switch() -> Any:
        async with maker() as db:
            return await migration.switch_context_embedding(
                db, context_id, NEW_MODEL, NEW_DIMENSIONS, requeue_since=requeue_since
            )

    try:
        await _seed_context(maker, owner, workspace_id, context_id)
        # first_id's event first: the batch runs second_id (item 1), then first_id.
        await _seed_forgotten(maker, owner, workspace_id, context_id, [first_id, second_id])
        with (
            patch(
                "services.permission_service.PermissionService.can_access_memory",
                AsyncMock(return_value=True),
            ),
            patch("services.embedding_service.EmbeddingService", MagicMock()),
            patch("services.sleep.undo.re_embed_memory_to_qdrant", new=fake_reembed),
            patch.object(sr._Executor, "_restore", paused_restore),
            patch.object(migration, "POINT_WRITER_WAIT_SECONDS", 6.0),
            patch.object(migration, "delete_points_from_qdrant", new=AsyncMock()),
            patch.object(migration, "delete_chunk_points", new=AsyncMock()),
        ):
            rollback_task = asyncio.create_task(
                sr.rollback_session(
                    rollback_db,
                    scope=sr.SessionScope(context_id, SESSION_ID, SessionPrincipal(owner, None)),
                    workspace_id=workspace_id,
                    requested_by=owner,
                    dry_run=False,
                    classify=lambda _exc: "service_unavailable",
                    new_correlation_id=lambda: "cid-2157",
                )
            )
            await asyncio.wait_for(gap_reached.wait(), TIMEOUT)

            switch_task = asyncio.create_task(run_switch())
            if not await _wait_until_switch_blocks(maker, switch_task):
                await switch_task
                pytest.fail(
                    "switch committed the new route while the session rollback, past a "
                    f"failed item, still restores under the old one "
                    f"(config now {await _config_model(maker, context_id)!r})"
                )
            await asyncio.wait({switch_task}, timeout=LOCK_WAIT)
            assert not switch_task.done()
            assert await _config_model(maker, context_id) == OLD_MODEL

            release_gap.set()
            result = await asyncio.wait_for(rollback_task, TIMEOUT)
            await rollback_db.commit()  # what the tool does after rollback_session
            switch_result = await asyncio.wait_for(switch_task, TIMEOUT)

        assert result.status == "partial"
        assert result.summary["failed"] == 1
        assert result.summary["restored"] == 1
        assert len(result.errors) == 1 and str(second_id) in result.errors[0]
        assert restored_collection == [get_collection_name(OLD_MODEL, OLD_DIMENSIONS)]
        assert switch_result.requeued == 1
        assert await _config_model(maker, context_id) == NEW_MODEL
        first = await _memory(maker, first_id)
        assert first.deleted_at is None
        # The switch re-queued the row restored under the old route.
        assert first.embedding_status == "pending"
        second = await _memory(maker, second_id)
        assert second.deleted_at is not None
        assert second.deleted_by == owner
        assert second.embedding_status == "success"
    finally:
        release_gap.set()
        for task in (rollback_task, switch_task):
            if task is not None and not task.done():
                try:
                    await asyncio.wait_for(task, TIMEOUT)
                except Exception:
                    pass
        await rollback_db.close()
        await _cleanup(maker, owner, workspace_id, context_id)


async def test_switch_is_refused_while_a_session_rollback_style_holder_keeps_the_lock(
    async_engine: AsyncEngine,
) -> None:
    maker = _maker(async_engine)
    owner = f"rollback-holder-{uuid4().hex}"
    workspace_id = uuid4()
    context_id = uuid4()
    memory_id = uuid4()

    holder = maker()
    try:
        await _seed_context(maker, owner, workspace_id, context_id)
        await _seed_forgotten(maker, owner, workspace_id, context_id, [memory_id])
        # Shared: what every point writer (the session rollback included) holds.
        await hold_point_writer_lock(holder)
        async with maker() as db:
            with (
                patch.object(migration, "POINT_WRITER_WAIT_SECONDS", 0.3),
                patch.object(migration, "delete_points_from_qdrant", new=AsyncMock()),
                patch.object(migration, "delete_chunk_points", new=AsyncMock()),
            ):
                with pytest.raises(ValidationError, match="route was not changed"):
                    await migration.switch_context_embedding(
                        db, context_id, NEW_MODEL, NEW_DIMENSIONS
                    )
                assert await _config_model(maker, context_id) == OLD_MODEL
                untouched = await _memory(maker, memory_id)
                assert untouched.deleted_at is not None
                assert untouched.embedding_status == "success"

                await holder.rollback()
                # wait_for_point_writers rolled the session back: it is usable.
                switched = await migration.switch_context_embedding(
                    db, context_id, NEW_MODEL, NEW_DIMENSIONS
                )
        assert switched.model == NEW_MODEL
        assert await _config_model(maker, context_id) == NEW_MODEL
    finally:
        await holder.rollback()
        await holder.close()
        await _cleanup(maker, owner, workspace_id, context_id)
