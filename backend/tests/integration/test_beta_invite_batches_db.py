"""Batch transactions and bounded listing against Postgres (#2704)."""

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from models.auth import AuditLog, User
from models.beta_invite import BetaInvite
from services.beta_invite_service import BetaInviteService
from utils.datetime import utcnow


@pytest_asyncio.fixture
async def admin(db_session):
    uid = f"batch-{uuid4().hex}"
    db_session.add(
        User(
            user_id=uid,
            email=f"{uid}@example.invalid",
            name="Batch test",
            role="admin",
            is_initial_admin=False,
            auth_method="oauth",
            auth_provider="google",
        )
    )
    await db_session.commit()
    yield uid
    await db_session.rollback()
    await db_session.execute(delete(AuditLog).where(AuditLog.user_id == uid))
    await db_session.execute(delete(User).where(User.user_id == uid))
    await db_session.commit()


async def test_stock_paging_revoke_and_audit(db_session, admin):
    svc = BetaInviteService(db_session)
    expiry = utcnow() + timedelta(days=20)
    stock = await svc.create_batch(admin, "batch-a", expiry, 4)
    other = await svc.create_batch(admin, "batch-b", expiry, 1)
    await db_session.execute(
        update(BetaInvite).where(BetaInvite.id == stock[0].invite.id).values(redeemed_at=utcnow())
    )
    await db_session.commit()
    first = await svc.get_summary(admin, limit=2)
    second = await svc.get_summary(admin, limit=2, offset=first.next_offset)
    third = await svc.get_summary(admin, limit=2, offset=second.next_offset)
    assert len({i.id for s in (first, second, third) for i in s.invites}) == 5
    assert third.next_offset is None
    assert all(s.used == 5 and s.redeemed == 1 for s in (first, second, third))
    assert await svc.revoke_unused_by_label(admin, "batch-a", dry_run=True) == 3
    assert await svc.revoke_unused_by_label(admin, "batch-a") == 3
    assert await svc.revoke_unused_by_label(admin, "batch-a") == 0
    db_session.expire_all()
    summary = await svc.get_summary(admin)
    assert summary.active == 1 and summary.redeemed == 1
    assert next(i for i in summary.invites if i.id == other[0].invite.id).status == "active"
    audits = (await db_session.scalars(select(AuditLog).where(AuditLog.user_id == admin))).all()
    assert len(audits) == 8
    assert "batch-a" not in str([a.user_metadata for a in audits])
    with pytest.raises(ValueError):
        await svc.create_batch(admin, "batch-a", expiry, 1)


async def test_dry_run_and_collision_across_days_are_atomic(db_session, admin):
    svc = BetaInviteService(db_session)
    expiry = utcnow() + timedelta(days=10)
    batches = [("day-a", expiry, 2), ("day-b", expiry + timedelta(days=1), 2)]
    assert await svc.create_batches(admin, batches, dry_run=True) == []
    assert (
        await db_session.scalar(
            select(func.count()).select_from(BetaInvite).where(BetaInvite.inviter_user_id == admin)
        )
        == 0
    )
    await svc.create_batch(admin, "day-b", expiry, 1)
    with pytest.raises(ValueError):
        await svc.create_batches(admin, batches)
    assert (
        await db_session.scalar(
            select(func.count()).select_from(BetaInvite).where(BetaInvite.inviter_user_id == admin)
        )
        == 1
    )


async def test_concurrent_batch_mint_only_one_wins(async_engine, db_session, admin):
    sessions = async_sessionmaker(async_engine, expire_on_commit=False)
    expiry = utcnow() + timedelta(days=1)

    async def mint():
        async with sessions() as session:
            try:
                return await BetaInviteService(session).create_batch(admin, "same", expiry, 2)
            except ValueError:
                return []

    results = await asyncio.gather(mint(), mint())
    assert sorted(map(len, results)) == [0, 2]


async def test_demoted_admin_cannot_mint_or_revoke(db_session, admin):
    await db_session.execute(update(User).where(User.user_id == admin).values(role="user"))
    await db_session.commit()
    svc = BetaInviteService(db_session)
    with pytest.raises(PermissionError):
        await svc.create_batch(admin, "batch", utcnow() + timedelta(days=1), 1)
    with pytest.raises(PermissionError):
        await svc.revoke_unused_by_label(admin, "batch")
