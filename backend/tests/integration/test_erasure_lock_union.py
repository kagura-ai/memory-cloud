"""Erasure's first lock set includes edited-only and workspace cascade rows."""

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from models.auth import Context, User, WorkspaceMember
from models.memory import Memory, MemoryRevision
from services.account_erasure_service import AccountErasureService

from .test_user_deletion_concurrency import wait_for_block


@pytest.mark.asyncio
async def test_erasure_does_not_lock_high_before_edited_only_low(db_session, connector_world):
    world = connector_world
    target = await world.user("erased")
    owner = await world.user("survivor")
    shared = await world.workspace(owner, **{target: "admin"})
    doomed = await world.workspace(target)
    context = Context(id=uuid4(), name="shared", workspace_id=shared.id, created_by=owner)
    cascade = Context(id=uuid4(), name="cascade", workspace_id=doomed.id, created_by=owner)
    db_session.add_all([context, cascade])
    await db_session.flush()
    # A rollback already holds its first (lower) edited row. Erasure must wait
    # there BEFORE taking any higher authored/cascade row, so rollback can finish.
    low, high = sorted([uuid4(), uuid4()])
    rows = [
        Memory(
            id=low,
            context_id=context.id,
            workspace_id=shared.id,
            user_id=owner,
            summary="edited by subject",
            content="body",
            type="note",
            client="test",
            version=2,
        ),
        Memory(
            id=high,
            context_id=cascade.id,
            workspace_id=doomed.id,
            user_id=owner,
            summary="cascade",
            content="body",
            type="note",
            client="test",
        ),
    ]
    db_session.add_all(rows)
    await db_session.flush()
    db_session.add(
        MemoryRevision(
            memory_id=low,
            context_id=context.id,
            version=1,
            kind="updated",
            writer="api",
            actor_user_id=target,
            prior={"summary": "before"},
        )
    )
    await db_session.commit()
    maker = async_sessionmaker(db_session.bind, expire_on_commit=False)
    started = asyncio.Event()
    erase_pid = None

    async def erase():
        nonlocal erase_pid
        async with maker() as db:
            erase_pid = await db.scalar(text("SELECT pg_backend_pid()"))
            subject = (await db.execute(select(User).where(User.user_id == target))).scalar_one()
            started.set()
            return await AccountErasureService(db)._delete_postgres(subject)

    task = None
    try:
        async with maker() as rollback:
            await rollback.execute(select(Memory.id).where(Memory.id == low).with_for_update())
            task = asyncio.create_task(erase())
            await asyncio.wait_for(started.wait(), 15)
            await asyncio.wait_for(wait_for_block(maker, erase_pid), 5)
            # NOWAIT makes an accidental high->low acquisition fail deterministically.
            await rollback.execute(
                select(Memory.id).where(Memory.id == high).with_for_update(nowait=True)
            )
            await rollback.commit()
        await asyncio.wait_for(task, 20)
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert await db_session.scalar(select(Memory.id).where(Memory.id == high)) is None
    assert await db_session.scalar(select(User.user_id).where(User.user_id == target)) is None


@pytest.mark.asyncio
async def test_erasure_locks_a_removed_workspace_s_contextless_memory_with_the_others(
    db_session, connector_world
):
    """A memory with no context in a workspace the erasure removes is in its
    first memory lock set (#2445), not locked first by its DELETE after the
    child rows: a transaction holding that row stops the erasure before it
    touches any child row."""
    world = connector_world
    target = await world.user("erased")
    former = await world.user("former")
    doomed = await world.workspace(target)
    orphan = Memory(
        id=uuid4(),
        context_id=None,
        workspace_id=doomed.id,
        user_id=former,
        summary="no context",
        content="body",
        type="note",
        client="test",
    )
    db_session.add(orphan)
    await db_session.commit()
    orphan_id = orphan.id
    maker = async_sessionmaker(db_session.bind, expire_on_commit=False)
    started = asyncio.Event()
    erase_pid = None

    async def erase():
        nonlocal erase_pid
        async with maker() as db:
            erase_pid = await db.scalar(text("SELECT pg_backend_pid()"))
            subject = (await db.execute(select(User).where(User.user_id == target))).scalar_one()
            started.set()
            return await AccountErasureService(db)._delete_postgres(subject)

    task = None
    try:
        async with maker() as holder:
            await holder.execute(select(Memory.id).where(Memory.id == orphan_id).with_for_update())
            task = asyncio.create_task(erase())
            await asyncio.wait_for(started.wait(), 15)
            await asyncio.wait_for(wait_for_block(maker, erase_pid), 5)
            # NOWAIT: a child row the erasure deletes after its memory lock
            # phase is still free while it waits on the context-less row.
            await holder.execute(
                select(WorkspaceMember.id)
                .where(WorkspaceMember.user_id == target)
                .with_for_update(nowait=True)
            )
            await holder.commit()
        await asyncio.wait_for(task, 20)
        assert await db_session.scalar(select(Memory.id).where(Memory.id == orphan_id)) is None
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await db_session.rollback()
        # no foreign key takes it with the workspace the fixture deletes
        await db_session.execute(delete(Memory).where(Memory.id == orphan_id))
        await db_session.commit()
