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


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unavailable", "rotated"])
async def test_mfa_route_rolls_back_only_its_own_claim(
    db_session, async_engine, monkeypatch, fernet_api_key_secret, failure
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from fastapi import HTTPException

    from api.routes import auth
    from tests.api.test_mfa_attempt_limit import FakeRedis
    from utils.encryption import get_encryptor
    from utils.exceptions import AuthenticationError

    uid = f"recovery-race-{uuid4().hex}"
    codes, hashes = issue_codes()
    _, replacement = issue_codes()
    user = User(
        user_id=uid,
        email=f"{uid}@example.test",
        password_hash="pw-hash",
        totp_enabled=True,
        totp_secret=get_encryptor().encrypt("TESTSECRET"),
        totp_recovery_hashes=hashes,
    )
    db_session.add(user)
    await db_session.commit()
    factory = async_sessionmaker(async_engine, expire_on_commit=False)

    async def get_db():
        async with factory() as db:
            yield db

    async def complete(user_id, _email, _session_id):
        if failure == "unavailable":
            raise TimeoutError("session sweep timed out")
        # Another DB session rotates the set after the password re-check but
        # before the route finalizes the recovery-code sign-in.
        async with factory() as db:
            locked = await db.scalar(select(User).where(User.user_id == user_id).with_for_update())
            locked.totp_recovery_hashes = replacement
            await db.commit()
        return True

    manager = MagicMock()
    manager._redis = FakeRedis()
    token = "test-token"
    manager._redis.setex(f"mfa_pending:{token}", 300, uid)
    manager._redis.setex(
        f"mfa_pending_cred:{token}", 300, credential_fingerprint(user.password_hash)
    )
    monkeypatch.setattr(auth, "_session_manager", manager)
    monkeypatch.setattr(auth, "get_db", get_db)
    monkeypatch.setattr(auth, "_create_password_session", AsyncMock(return_value="new-session"))
    monkeypatch.setattr(auth, "_complete_password_sign_in", complete)
    notice = MagicMock()
    monkeypatch.setattr(auth, "spawn_security_notification", notice)
    req = SimpleNamespace(headers={}, cookies={}, client=SimpleNamespace(host="127.0.0.1"))
    body = auth.MfaVerifyRequest(mfa_session_token=token, totp_code=codes[0])
    try:
        if failure == "unavailable":
            with pytest.raises(HTTPException) as failed:
                await asyncio.wait_for(auth.mfa_verify(body, req, None), timeout=8)
            assert failed.value.status_code == 503
        else:
            with pytest.raises(AuthenticationError):
                await asyncio.wait_for(auth.mfa_verify(body, req, None), timeout=8)
        manager.delete_session.assert_called_once_with("new-session")
        notice.assert_not_called()
        await db_session.refresh(user)
        assert set(user.totp_recovery_hashes) == set(
            hashes if failure == "unavailable" else replacement
        )
        assert not await db_session.scalar(select(AuditLog.id).where(AuditLog.user_id == uid))
    finally:
        await db_session.execute(delete(User).where(User.user_id == uid))
        await db_session.commit()
