"""Competing workers cannot redeem the same MFA recovery code twice."""

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from auth.recovery_codes import claim_login_code, issue_codes, settle_login_code
from models.auth import AuditLog, User
from services.password_account_service import credential_fingerprint, password_unchanged


@pytest.mark.asyncio
async def test_concurrent_redemption_and_signin_invalidation(db_session, async_engine):
    user_id = f"mfa-{uuid4().hex}"
    codes, hashes = issue_codes()
    user = User(
        user_id=user_id,
        email=f"{user_id}@example.test",
        password_hash="stored-hash",
        mfa_generation=uuid4().hex,
        totp_enabled=True,
        totp_recovery_hashes=hashes,
    )
    db_session.add(user)
    await db_session.commit()
    try:
        factory = async_sessionmaker(async_engine, expire_on_commit=False)

        async def redeem() -> bool:
            async with factory() as db:
                claim = await claim_login_code(db, user_id, codes[0])
                if not claim:
                    return False
                audit = AuditLog(
                    user_id=user_id,
                    user_email="self-service",
                    resource="user:mfa",
                    action="recovery_code_used",
                )
                return await settle_login_code(db, user_id, claim, audit=audit)

        assert sorted(await asyncio.gather(redeem(), redeem())) == [False, True]
        await db_session.refresh(user)
        assert len(user.totp_recovery_hashes) == 7
        count = await db_session.scalar(
            select(func.count(AuditLog.id)).where(
                AuditLog.user_id == user_id, AuditLog.action == "recovery_code_used"
            )
        )
        assert count == 1
        # A sign-in that read the account before enrollment cannot complete after it.
        assert not await password_unchanged(
            db_session, user_id, credential_fingerprint(user.password_hash)
        )
        assert await password_unchanged(
            db_session, user_id, credential_fingerprint(user.password_hash, user.mfa_generation)
        )
    finally:
        await db_session.execute(delete(AuditLog).where(AuditLog.user_id == user_id))
        await db_session.execute(delete(User).where(User.user_id == user_id))
        await db_session.commit()


@pytest.mark.asyncio
async def test_crash_expiry_reclaim_and_rotation_are_fenced(db_session, monkeypatch):
    import auth.recovery_codes as recovery

    user_id = f"mfa-{uuid4().hex}"
    codes, hashes = issue_codes()
    user = User(
        user_id=user_id,
        email=f"{user_id}@example.test",
        totp_enabled=True,
        totp_recovery_hashes=hashes,
    )
    db_session.add(user)
    await db_session.commit()
    try:
        first = await claim_login_code(db_session, user_id, codes[0])
        assert first and not await claim_login_code(db_session, user_id, codes[0])
        monkeypatch.setattr(
            recovery, "time", type("Clock", (), {"time": staticmethod(lambda: first.expires + 1)})
        )
        current = await claim_login_code(db_session, user_id, codes[0])
        assert current and current.marker != first.marker
        assert not await settle_login_code(db_session, user_id, first, audit=None)
        assert await settle_login_code(db_session, user_id, current, audit=None)
        retry = await claim_login_code(db_session, user_id, codes[0])
        assert retry
        _, replacement = issue_codes()
        user.totp_recovery_hashes = replacement
        await db_session.commit()
        assert not await settle_login_code(db_session, user_id, retry, audit=None)
        assert not await claim_login_code(db_session, user_id, codes[0])
        await db_session.refresh(user)
        assert user.totp_recovery_hashes == replacement
    finally:
        await db_session.execute(delete(User).where(User.user_id == user_id))
        await db_session.commit()
