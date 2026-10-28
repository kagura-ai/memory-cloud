"""Erasure's first lock set includes edited-only and workspace cascade rows."""

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from models.auth import Context, User
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
