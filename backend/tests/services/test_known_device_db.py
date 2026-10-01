"""Known-device rows against a real database (#1769): the sign-in rule, the
password-reset sweep and the retention DELETE."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import Settings
from models.auth import User, UserKnownDevice
from services import known_device_service as svc
from services.known_device_service import SignIn, record_sign_in, stale_devices_delete
from utils.datetime import utcnow

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    cfg = Settings(_env_file=None, audit_hmac_key="k" * 32, known_device_max_per_user=3)
    monkeypatch.setattr(svc, "get_settings", lambda: cfg)
    return cfg


async def _user(db: AsyncSession) -> str:
    uid = f"u_{uuid4().hex[:10]}"
    db.add(
        User(
            user_id=uid,
            email=f"{uid}@dev.example",
            name="Dev",
            role="user",
            is_initial_admin=False,
            auth_method="oauth",
        )
    )
    await db.flush()
    return uid


async def _hashes(db: AsyncSession, uid: str) -> list[str]:
    rows = (
        await db.execute(
            select(UserKnownDevice.device_hash)
            .where(UserKnownDevice.user_id == uid)
            .order_by(UserKnownDevice.last_seen)
        )
    ).scalars()
    return list(rows)


async def test_first_then_known_then_new(db_session: AsyncSession) -> None:
    uid = await _user(db_session)
    now = utcnow()

    assert (
        await record_sign_in(db_session, user_id=uid, digest="a" * 64, now=now)
        is SignIn.FIRST_DEVICE
    )
    assert (
        await record_sign_in(db_session, user_id=uid, digest="a" * 64, now=now + timedelta(hours=1))
        is SignIn.KNOWN
    )
    assert (
        await record_sign_in(db_session, user_id=uid, digest="b" * 64, now=now + timedelta(hours=2))
        is SignIn.NEW_DEVICE
    )
    await db_session.flush()

    rows = (
        (
            await db_session.execute(
                select(UserKnownDevice)
                .where(UserKnownDevice.user_id == uid)
                .order_by(UserKnownDevice.first_seen)
            )
        )
        .scalars()
        .all()
    )
    assert [r.device_hash for r in rows] == ["a" * 64, "b" * 64]
    # The known row's last_seen slid; first_seen did not.
    assert rows[0].first_seen == now
    assert rows[0].last_seen == now + timedelta(hours=1)


async def test_cap_keeps_the_most_recently_seen(db_session: AsyncSession) -> None:
    uid = await _user(db_session)
    now = utcnow()
    for i, digest in enumerate(("a", "b", "c")):
        await record_sign_in(
            db_session, user_id=uid, digest=digest * 64, now=now + timedelta(hours=i)
        )
    # "a" is the oldest; a 4th browser pushes it out (cap = 3).
    outcome = await record_sign_in(
        db_session, user_id=uid, digest="d" * 64, now=now + timedelta(hours=9)
    )
    await db_session.flush()

    assert outcome is SignIn.NEW_DEVICE
    assert await _hashes(db_session, uid) == ["b" * 64, "c" * 64, "d" * 64]


async def test_devices_are_per_account(db_session: AsyncSession) -> None:
    one, two = await _user(db_session), await _user(db_session)
    now = utcnow()
    await record_sign_in(db_session, user_id=one, digest="a" * 64, now=now)

    # The same browser signing in to another account is that account's first device.
    assert (
        await record_sign_in(db_session, user_id=two, digest="a" * 64, now=now)
        is SignIn.FIRST_DEVICE
    )


async def test_retention_delete_forgets_stale_rows_only(db_session: AsyncSession) -> None:
    uid = await _user(db_session)
    now = utcnow()
    await record_sign_in(
        db_session, user_id=uid, digest="old" + "0" * 61, now=now - timedelta(days=200)
    )
    await record_sign_in(
        db_session, user_id=uid, digest="new" + "0" * 61, now=now - timedelta(days=1)
    )
    await db_session.flush()

    result = await db_session.execute(stale_devices_delete(now - timedelta(days=180)))

    assert result.rowcount == 1
    assert await _hashes(db_session, uid) == ["new" + "0" * 61]


async def test_rows_cascade_with_the_account(db_session: AsyncSession) -> None:
    uid = await _user(db_session)
    await record_sign_in(db_session, user_id=uid, digest="a" * 64, now=utcnow())
    await db_session.flush()

    user = (await db_session.execute(select(User).where(User.user_id == uid))).scalar_one()
    await db_session.delete(user)
    await db_session.flush()

    count = (
        await db_session.execute(
            select(func.count()).select_from(UserKnownDevice).where(UserKnownDevice.user_id == uid)
        )
    ).scalar_one()
    assert count == 0


async def test_reset_keeps_the_account_armed(db_session: AsyncSession) -> None:
    # Rows deleted (as complete_reset does) but known_devices_since kept: the
    # very next sign-in — from any browser — is a new device, not the first.
    uid = await _user(db_session)
    now = utcnow()
    await record_sign_in(db_session, user_id=uid, digest="a" * 64, now=now)
    await db_session.execute(svc.known_devices_delete(uid))

    outcome = await record_sign_in(db_session, user_id=uid, digest="b" * 64, now=now)

    assert outcome is SignIn.NEW_DEVICE
    user = (await db_session.execute(select(User).where(User.user_id == uid))).scalar_one()
    assert user.known_devices_since == now


async def test_concurrent_first_sign_ins_cannot_both_be_first(async_engine) -> None:
    """Two independent transactions: the second waits on the users row lock
    and, once the first commits, sees the marker — so it is NEW_DEVICE, not a
    second FIRST_DEVICE. Would fail without ``with_for_update()``."""
    import asyncio

    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker

    maker = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as setup:
        uid = await _user(setup)
        await setup.commit()
    now = utcnow()
    try:
        async with maker() as a, maker() as b:
            first = await record_sign_in(a, user_id=uid, digest="a" * 64, now=now)
            assert first is SignIn.FIRST_DEVICE
            # b blocks on a's FOR UPDATE lock until a commits.
            second_task = asyncio.create_task(
                record_sign_in(b, user_id=uid, digest="b" * 64, now=now)
            )
            await asyncio.sleep(0.2)
            assert not second_task.done()
            await a.commit()
            second = await asyncio.wait_for(second_task, timeout=10)
            await b.commit()
        assert second is SignIn.NEW_DEVICE
    finally:
        async with maker() as cleanup:
            await cleanup.execute(delete(User).where(User.user_id == uid))
            await cleanup.commit()
