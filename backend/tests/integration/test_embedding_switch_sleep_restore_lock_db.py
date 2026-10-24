"""Sleep restores and embedding switches share the point-writer lock."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from db.qdrant import get_collection_name
from models.auth import Context, User, Workspace, WorkspaceMember, WorkspaceRole
from models.config import ContextSearchConfig
from models.memory import DELETED_BY_SLEEP_MERGE, Memory
from models.sleep import SleepAction, SleepReport
from services import embedding_migration_service as migration
from utils.datetime import utcnow

OLD_MODEL = "text-embedding-3-small"
OLD_DIMENSIONS = 1536
NEW_MODEL = "qwen3-embedding:4b"
NEW_DIMENSIONS = 2560
TIMEOUT = 10
LOCK_WAIT = 2


@pytest.mark.parametrize("restore_path", ["merge_undo", "run_rollback"])
async def test_switch_waits_for_sleep_restore_and_requeues_it(
    async_engine: AsyncEngine, restore_path: str
) -> None:
    maker = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    owner = f"switch-restore-{uuid4().hex}"
    workspace_id = uuid4()
    context_id = uuid4()
    report_id = uuid4()
    memory_id = uuid4()
    winner_id = uuid4()
    written_at = utcnow() - timedelta(days=7)
    requeue_since = utcnow() - timedelta(seconds=1)
    restored_collection: list[str] = []
    route_resolved = asyncio.Event()
    release_restore = asyncio.Event()
    switch_lock_attempted = asyncio.Event()
    restore_task: asyncio.Task[object] | None = None
    switch_task: asyncio.Task[object] | None = None
    restore_db = maker()

    async def pause_after_route() -> None:
        route_resolved.set()
        await release_restore.wait()

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
        restored_collection.append(collection_name)
        await db.execute(
            update(Memory).where(Memory.id == memory.id).values(embedding_status="success")
        )

    async with maker() as seed:
        seed.add(User(email=f"{owner}@test.example", user_id=owner, role="user"))
        await seed.flush()
        workspace = Workspace(
            id=workspace_id,
            name=f"switch-ws-{uuid4().hex[:8]}",
            plan_name="pro",
            owner_user_id=owner,
            daily_api_limit=5000,
            weekly_api_limit=25000,
        )
        seed.add(workspace)
        await seed.flush()
        seed.add(
            WorkspaceMember(workspace_id=workspace_id, user_id=owner, role=WorkspaceRole.OWNER)
        )
        context = Context(
            id=context_id,
            workspace_id=workspace_id,
            name=f"switch-ctx-{uuid4().hex[:8]}",
            created_by=owner,
            is_private=False,
            trust_tier="trusted",
        )
        seed.add(context)
        seed.add(
            ContextSearchConfig(
                context_id=context_id,
                embedding_model=OLD_MODEL,
                embedding_dimensions=OLD_DIMENSIONS,
            )
        )
        memory = Memory(
            id=memory_id,
            workspace_id=workspace_id,
            context_id=context_id,
            user_id=owner,
            summary="sleep restore lock regression",
            content="sleep restore lock regression",
            type="note",
            client="pytest",
            scope="working",
            embedding_status="success",
            embedding_attempted_at=written_at,
            created_at=written_at,
            deleted_at=written_at + timedelta(days=1),
            deleted_by=DELETED_BY_SLEEP_MERGE,
        )
        report = SleepReport(
            id=report_id,
            user_id=owner,
            workspace_id=workspace_id,
            context_id=context_id,
            status="completed",
        )
        seed.add_all([memory, report])
        await seed.flush()
        seed.add(
            SleepAction(
                report_id=report_id,
                phase="dedup_merge",
                action_type="merge",
                memory_id=winner_id,
                target_id=memory_id,
                details={},
            )
        )
        await seed.commit()

    real_wait = migration.wait_for_point_writers  # bound before it is patched

    async def attempting_wait(db: AsyncSession, *, timeout_seconds: float) -> bool:
        switch_lock_attempted.set()
        return await real_wait(db, timeout_seconds=timeout_seconds)

    async def run_switch() -> object:
        async with maker() as db:
            return await migration.switch_context_embedding(
                db,
                context_id,
                NEW_MODEL,
                NEW_DIMENSIONS,
                requeue_since=requeue_since,
            )

    async def verify_switch_waits() -> None:
        nonlocal switch_task
        switch_task = asyncio.create_task(run_switch())
        lock_waiter = asyncio.create_task(switch_lock_attempted.wait())
        try:
            done, _ = await asyncio.wait(
                {lock_waiter, switch_task},
                timeout=LOCK_WAIT,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if lock_waiter not in done:
                if switch_task in done:
                    await switch_task
                    async with maker() as check:
                        config = (
                            await check.execute(
                                select(ContextSearchConfig).where(
                                    ContextSearchConfig.context_id == context_id
                                )
                            )
                        ).scalar_one()
                    release_restore.set()
                    await asyncio.wait_for(restore_task, TIMEOUT)
                    assert config.embedding_model == NEW_MODEL
                    assert restored_collection == [
                        get_collection_name(OLD_MODEL, OLD_DIMENSIONS),
                        get_collection_name(OLD_MODEL, OLD_DIMENSIONS),
                    ]
                    pytest.fail(
                        "switch committed the new route while Sleep restore kept the old collection"
                    )
                pytest.fail("switch neither acquired the exclusive lock nor completed")
            await _assert_switch_waiting(maker, context_id, OLD_MODEL, switch_task)
        finally:
            if not lock_waiter.done():
                lock_waiter.cancel()
                await asyncio.gather(lock_waiter, return_exceptions=True)

    try:
        if restore_path == "merge_undo":
            import services.sleep.undo as undo_service
            from services.sleep.undo import undo_merge_action

            original_resolve = undo_service._resolve_embedding_info

            async def pause_resolve(
                db: AsyncSession, context_id: UUID | None
            ) -> tuple[str | None, str]:
                result = await original_resolve(db, context_id)
                restored_collection.append(result[1])
                await pause_after_route()
                return result

            with (
                patch("services.sleep.undo._resolve_embedding_info", side_effect=pause_resolve),
                patch("services.sleep.undo.re_embed_memory_to_qdrant", side_effect=fake_reembed),
                patch("services.embedding_service.EmbeddingService", MagicMock()),
            ):
                restore_task = asyncio.create_task(
                    undo_merge_action(
                        restore_db,
                        await _action_id(maker, report_id),
                        acting_user_id=owner,
                    )
                )
                await asyncio.wait_for(route_resolved.wait(), TIMEOUT)
                with (
                    patch.object(migration, "wait_for_point_writers", new=attempting_wait),
                    patch.object(migration, "POINT_WRITER_WAIT_SECONDS", 6.0),
                    patch.object(migration, "delete_points_from_qdrant", new=AsyncMock()),
                    patch.object(migration, "delete_chunk_points", new=AsyncMock()),
                ):
                    await verify_switch_waits()
                    release_restore.set()
                    await asyncio.wait_for(restore_task, TIMEOUT)
                    await asyncio.wait_for(switch_task, TIMEOUT)
        else:
            from mcp_server.tools import sleep as sleep_tools

            original_prefetch = sleep_tools._prefetch_rollback_memories

            async def pause_prefetch(
                db: AsyncSession, actions: list[SleepAction], user_id: str
            ) -> dict[UUID, Memory]:
                memories = await original_prefetch(db, actions, user_id)
                config = (
                    await db.execute(
                        select(ContextSearchConfig).where(
                            ContextSearchConfig.context_id == context_id
                        )
                    )
                ).scalar_one()
                restored_collection.append(
                    get_collection_name(config.embedding_model, config.embedding_dimensions)
                )
                await pause_after_route()
                return memories

            async def get_db() -> AsyncIterator[AsyncSession]:
                yield restore_db

            with (
                patch("db.base.get_db", return_value=get_db()),
                patch.object(
                    sleep_tools, "_authorize_report_rollback", new=AsyncMock(return_value=None)
                ),
                patch.object(sleep_tools, "_log_tool_usage", new=AsyncMock()),
                patch.object(
                    sleep_tools, "_prefetch_rollback_memories", side_effect=pause_prefetch
                ),
                patch.object(sleep_tools, "_re_embed_to_qdrant", side_effect=fake_reembed),
                patch("services.embedding_service.EmbeddingService", MagicMock()),
            ):
                restore_task = asyncio.create_task(
                    sleep_tools.handle_rollback_sleep_run(
                        {"report_id": str(report_id)}, owner, workspace_id
                    )
                )
                await asyncio.wait_for(route_resolved.wait(), TIMEOUT)
                with (
                    patch.object(migration, "wait_for_point_writers", new=attempting_wait),
                    patch.object(migration, "POINT_WRITER_WAIT_SECONDS", 6.0),
                    patch.object(migration, "delete_points_from_qdrant", new=AsyncMock()),
                    patch.object(migration, "delete_chunk_points", new=AsyncMock()),
                ):
                    await verify_switch_waits()
                    release_restore.set()
                    await asyncio.wait_for(restore_task, TIMEOUT)
                    await asyncio.wait_for(switch_task, TIMEOUT)

        assert restored_collection == [
            get_collection_name(OLD_MODEL, OLD_DIMENSIONS),
            get_collection_name(OLD_MODEL, OLD_DIMENSIONS),
        ]
        async with maker() as check:
            config = (
                await check.execute(
                    select(ContextSearchConfig).where(ContextSearchConfig.context_id == context_id)
                )
            ).scalar_one()
            restored = (
                await check.execute(select(Memory).where(Memory.id == memory_id))
            ).scalar_one()
            assert config.embedding_model == NEW_MODEL
            assert restored.deleted_at is None
            assert restored.embedding_status == "pending"
    finally:
        release_restore.set()
        for task in (restore_task, switch_task):
            if task is not None and not task.done():
                try:
                    await asyncio.wait_for(task, TIMEOUT)
                except Exception:
                    pass
        await restore_db.close()
        async with maker() as cleanup:
            await cleanup.execute(delete(SleepAction).where(SleepAction.report_id == report_id))
            await cleanup.execute(delete(SleepReport).where(SleepReport.id == report_id))
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


async def test_switch_waits_for_new_route_restore_before_pruning(async_engine: AsyncEngine) -> None:
    maker = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    owner = f"switch-prune-{uuid4().hex}"
    workspace_id = uuid4()
    context_id = uuid4()
    report_id = uuid4()
    memory_id = uuid4()
    written_at = utcnow() - timedelta(days=7)
    requeue_since = utcnow() - timedelta(days=8)
    restored_collections: list[str] = []
    deleted_points: list[tuple[list[str], str]] = []
    route_committed = asyncio.Event()
    allow_prune_lock = asyncio.Event()
    prune_lock_attempted = asyncio.Event()
    point_written = asyncio.Event()
    allow_restore_commit = asyncio.Event()
    restore_db = maker()
    switch_task: asyncio.Task[object] | None = None
    restore_task: asyncio.Task[object] | None = None
    exclusive_calls = 0

    async with maker() as seed:
        seed.add(User(email=f"{owner}@test.example", user_id=owner, role="user"))
        await seed.flush()
        workspace = Workspace(
            id=workspace_id,
            name=f"prune-ws-{uuid4().hex[:8]}",
            plan_name="pro",
            owner_user_id=owner,
            daily_api_limit=5000,
            weekly_api_limit=25000,
        )
        seed.add(workspace)
        await seed.flush()
        seed.add(
            WorkspaceMember(workspace_id=workspace_id, user_id=owner, role=WorkspaceRole.OWNER)
        )
        seed.add(
            Context(
                id=context_id,
                workspace_id=workspace_id,
                name=f"prune-ctx-{uuid4().hex[:8]}",
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
                summary="restore while the new collection is pruned",
                content="restore while the new collection is pruned",
                type="note",
                client="pytest",
                scope="working",
                embedding_status="success",
                embedding_attempted_at=written_at,
                created_at=written_at,
                deleted_at=written_at + timedelta(days=1),
                deleted_by=DELETED_BY_SLEEP_MERGE,
            )
        )
        seed.add(
            SleepReport(
                id=report_id,
                user_id=owner,
                workspace_id=workspace_id,
                context_id=context_id,
                status="completed",
            )
        )
        await seed.flush()
        seed.add(
            SleepAction(
                report_id=report_id,
                phase="dedup_merge",
                action_type="merge",
                memory_id=uuid4(),
                target_id=memory_id,
                details={},
            )
        )
        await seed.commit()

    real_wait = migration.wait_for_point_writers  # bound before it is patched

    async def switch_lock(db: AsyncSession, *, timeout_seconds: float) -> bool:
        nonlocal exclusive_calls
        exclusive_calls += 1
        if exclusive_calls > 1:
            route_committed.set()
            await allow_prune_lock.wait()
            prune_lock_attempted.set()
        return await real_wait(db, timeout_seconds=timeout_seconds)

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
        restored_collections.append(collection_name)
        point_written.set()
        await allow_restore_commit.wait()
        await db.execute(
            update(Memory).where(Memory.id == memory.id).values(embedding_status="success")
        )

    async def delete_points(point_ids: list[str], collection_name: str) -> None:
        deleted_points.append((point_ids, collection_name))

    async def run_switch() -> object:
        async with maker() as db:
            return await migration.switch_context_embedding(
                db,
                context_id,
                NEW_MODEL,
                NEW_DIMENSIONS,
                requeue_since=requeue_since,
            )

    try:
        from services.sleep.undo import undo_merge_action

        with (
            patch.object(migration, "wait_for_point_writers", new=switch_lock),
            patch.object(migration, "POINT_WRITER_WAIT_SECONDS", 6.0),
            patch.object(migration, "delete_points_from_qdrant", new=delete_points),
            patch.object(migration, "delete_chunk_points", new=AsyncMock()),
        ):
            switch_task = asyncio.create_task(run_switch())
            await asyncio.wait_for(route_committed.wait(), TIMEOUT)

            action_id = await _action_id(maker, report_id)
            with (
                patch("services.sleep.undo.re_embed_memory_to_qdrant", new=fake_reembed),
                patch("services.embedding_service.EmbeddingService", MagicMock()),
            ):
                restore_task = asyncio.create_task(
                    undo_merge_action(restore_db, action_id, acting_user_id=owner)
                )
                await asyncio.wait_for(point_written.wait(), TIMEOUT)
                allow_prune_lock.set()
                await asyncio.wait_for(prune_lock_attempted.wait(), TIMEOUT)
                assert not switch_task.done()
                allow_restore_commit.set()
                await asyncio.wait_for(restore_task, TIMEOUT)
                result = await asyncio.wait_for(switch_task, TIMEOUT)

        assert restored_collections == [get_collection_name(NEW_MODEL, NEW_DIMENSIONS)]
        assert deleted_points == []
        assert result.stale_removed == 0
        # The real wait acquired the lock after the restore committed; a
        # timeout would defer pruning and pass the two checks above vacuously.
        assert result.prune_pending is False
        async with maker() as check:
            config = (
                await check.execute(
                    select(ContextSearchConfig).where(ContextSearchConfig.context_id == context_id)
                )
            ).scalar_one()
            restored = (
                await check.execute(select(Memory).where(Memory.id == memory_id))
            ).scalar_one()
            assert config.embedding_model == NEW_MODEL
            assert restored.deleted_at is None
            assert restored.embedding_status == "success"
    finally:
        allow_prune_lock.set()
        allow_restore_commit.set()
        for task in (restore_task, switch_task):
            if task is not None and not task.done():
                try:
                    await asyncio.wait_for(task, TIMEOUT)
                except Exception:
                    pass
        await restore_db.close()
        async with maker() as cleanup:
            await cleanup.execute(delete(SleepAction).where(SleepAction.report_id == report_id))
            await cleanup.execute(delete(SleepReport).where(SleepReport.id == report_id))
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


async def _action_id(maker: async_sessionmaker[AsyncSession], report_id: UUID) -> int:
    async with maker() as db:
        action = (
            await db.execute(select(SleepAction.id).where(SleepAction.report_id == report_id))
        ).scalar_one()
    return int(action)


async def _assert_switch_waiting(
    maker: async_sessionmaker[AsyncSession],
    context_id: UUID,
    expected_model: str,
    switch_task: asyncio.Task[object],
) -> None:
    assert not switch_task.done()
    async with maker() as db:
        config = (
            await db.execute(
                select(ContextSearchConfig).where(ContextSearchConfig.context_id == context_id)
            )
        ).scalar_one()
    assert config.embedding_model == expected_model
