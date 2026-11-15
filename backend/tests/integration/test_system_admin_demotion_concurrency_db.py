"""Concurrent demotions leave one system admin (#2828).

``PUT /api/v1/admin/users/{user_id}/role`` and
``DELETE /api/v1/admin/system-admins/{user_id}`` share
``SystemAdminService.lock_for_role_change``: one statement locks the admin
rows and the target in ``user_id`` order, ``FOR NO KEY UPDATE``, and the
admin count is read after it. Two demotions therefore run one after the
other, and the second one counts what the first committed.

Each test drives real Postgres sessions and waits on ``pg_blocking_pids``
before it releases the paused one.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.routes.admin import RoleUpdateRequest, update_user_role
from api.routes.system_admins import demote_system_admin
from models.auth import AuditLog, User
from utils.exceptions import AdminProtectionError, NotFoundException

# A service-mode operator: no users row, so no self-demotion check matches.
OPERATOR = {"user_id": "operator:ops@example.com", "email": "operator:ops@example.com"}
LAST_ADMIN_MESSAGE = (
    "Cannot demote the last remaining system administrator. At least one admin must exist."
)
ROLE_ACTIONS = ("system_role_update", "system_admin_demote")


async def _demote(route: str, db: AsyncSession, user_id: str):
    """Take the admin role from ``user_id`` through one of the two routes."""
    if route == "PUT":
        return await update_user_role(user_id, RoleUpdateRequest(role="user"), OPERATOR, db)
    return await demote_system_admin(user_id, OPERATOR, db)


async def wait_for_block(maker, pid: int) -> None:
    async with maker() as observer:
        while not await observer.scalar(
            text("SELECT cardinality(pg_blocking_pids(:pid)) > 0"), {"pid": pid}
        ):
            await asyncio.sleep(0.01)


@pytest_asyncio.fixture
async def admins(db_session: AsyncSession) -> AsyncIterator:
    """A factory of admin rows that are the ONLY admins for the test.

    Admin rows other tests left behind are set aside (role ``user``) and
    restored afterwards; the rows and audit rows this test wrote are removed.
    """
    await db_session.rollback()
    others = list(
        (await db_session.execute(select(User.user_id).where(User.role == "admin"))).scalars().all()
    )
    if others:
        await db_session.execute(update(User).where(User.user_id.in_(others)).values(role="user"))
        await db_session.commit()
    created: list[str] = []
    marker = uuid4().hex[:8]

    async def _create(count: int) -> list[str]:
        ids = [f"admin-{marker}-{index}" for index in range(len(created), len(created) + count)]
        for user_id in ids:
            db_session.add(
                User(
                    email=f"{user_id}@example.com",
                    user_id=user_id,
                    role="admin",
                    is_initial_admin=False,
                )
            )
        created.extend(ids)
        await db_session.commit()
        return ids

    yield _create

    await db_session.rollback()
    await db_session.execute(
        delete(AuditLog).where(
            AuditLog.action.in_(ROLE_ACTIONS),
            AuditLog.user_email == OPERATOR["email"],
        )
    )
    await db_session.execute(delete(User).where(User.user_id.in_(created)))
    if others:
        await db_session.execute(update(User).where(User.user_id.in_(others)).values(role="admin"))
    await db_session.commit()


async def _admin_ids(db_session: AsyncSession) -> list[str]:
    await db_session.rollback()
    return list(
        (
            await db_session.execute(
                select(User.user_id).where(User.role == "admin").order_by(User.user_id)
            )
        )
        .scalars()
        .all()
    )


async def _role_audit_targets(db_session: AsyncSession) -> list[str]:
    """The account each role audit row of this test's operator is about."""
    rows = (
        await db_session.execute(
            select(AuditLog.action, AuditLog.user_id, AuditLog.user_metadata).where(
                AuditLog.action.in_(ROLE_ACTIONS), AuditLog.user_email == OPERATOR["email"]
            )
        )
    ).all()
    return sorted(
        row.user_metadata["target_user_id"] if row.action == "system_role_update" else row.user_id
        for row in rows
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first_route", "second_route"),
    [("PUT", "PUT"), ("DELETE", "DELETE"), ("PUT", "DELETE"), ("DELETE", "PUT")],
)
@pytest.mark.parametrize("first_holds", ["lower_user_id", "higher_user_id"])
async def test_two_demotions_of_the_last_two_admins_leave_one_admin(
    db_session, admins, first_route, second_route, first_holds
):
    lower, higher = await admins(2)
    first_target, second_target = (
        (lower, higher) if first_holds == "lower_user_id" else (higher, lower)
    )
    maker = async_sessionmaker(db_session.bind, expire_on_commit=False)
    gap, resume, started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    second_pid = None

    async def first():
        async with maker() as db:
            commit = db.commit

            async def paused_commit():
                # The role and its audit row are written; the locks are held.
                gap.set()
                await asyncio.wait_for(resume.wait(), 15)
                await commit()

            db.commit = paused_commit
            return await _demote(first_route, db, first_target)

    async def second():
        nonlocal second_pid
        async with maker() as db:
            second_pid = await db.scalar(text("SELECT pg_backend_pid()"))
            started.set()
            return await _demote(second_route, db, second_target)

    tasks = []
    try:
        one = asyncio.create_task(first())
        tasks.append(one)
        await asyncio.wait_for(gap.wait(), 15)
        two = asyncio.create_task(second())
        tasks.append(two)
        await asyncio.wait_for(started.wait(), 15)
        # The second demotion is seen waiting on the first one's row locks.
        await asyncio.wait_for(wait_for_block(maker, second_pid), 5)
        assert not two.done()
        resume.set()
        await asyncio.wait_for(one, 15)
        with pytest.raises(AdminProtectionError) as refused:
            await asyncio.wait_for(two, 15)
    finally:
        resume.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    assert refused.value.error_code == "ADMIN-001"
    assert refused.value.status_code == 403
    assert refused.value.reason == "last_admin"
    assert refused.value.message == LAST_ADMIN_MESSAGE
    # Exactly one succeeded: one admin is left, and only the demotion that
    # went through left an audit row.
    assert await _admin_ids(db_session) == [second_target]
    assert await _role_audit_targets(db_session) == [first_target]


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["PUT", "DELETE"])
async def test_a_demotion_does_not_wait_on_a_key_share_lock_of_an_admin_row(
    db_session, admins, route
):
    """An open insert that references a user holds ``FOR KEY SHARE`` on the row."""
    ids = await admins(3)
    target = ids[1]
    maker = async_sessionmaker(db_session.bind, expire_on_commit=False)
    async with maker() as holder, maker() as demoting:
        held = (
            (
                await holder.execute(
                    select(User.user_id)
                    .where(User.user_id.in_(ids))
                    .with_for_update(read=True, key_share=True)
                )
            )
            .scalars()
            .all()
        )
        assert sorted(held) == ids
        # Still held by the other session while the demotion runs and commits.
        await asyncio.wait_for(_demote(route, demoting, target), 5)
        await holder.rollback()

    assert await _admin_ids(db_session) == [ids[0], ids[2]]
    assert await _role_audit_targets(db_session) == [target]


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["PUT", "DELETE"])
async def test_a_target_deleted_while_the_demotion_waits_is_404_and_nothing_is_written(
    db_session, admins, route
):
    ids = await admins(3)
    target = ids[1]
    maker = async_sessionmaker(db_session.bind, expire_on_commit=False)
    started = asyncio.Event()
    demotion_pid = None

    async def demote():
        nonlocal demotion_pid
        async with maker() as db:
            demotion_pid = await db.scalar(text("SELECT pg_backend_pid()"))
            started.set()
            return await _demote(route, db, target)

    task = None
    try:
        async with maker() as deleting:
            await deleting.execute(delete(User).where(User.user_id == target))
            task = asyncio.create_task(demote())
            await asyncio.wait_for(started.wait(), 15)
            await asyncio.wait_for(wait_for_block(maker, demotion_pid), 5)
            assert not task.done()
            await deleting.commit()
        with pytest.raises((HTTPException, NotFoundException)) as missing:
            await asyncio.wait_for(task, 15)
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert missing.value.status_code == 404
    assert await _admin_ids(db_session) == [ids[0], ids[2]]
    assert await _role_audit_targets(db_session) == []
    assert (
        await db_session.scalar(
            select(func.count()).select_from(User).where(User.user_id == target)
        )
        == 0
    )
