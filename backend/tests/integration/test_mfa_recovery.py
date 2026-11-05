"""Competing workers cannot redeem the same MFA recovery code twice."""

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker

from auth.recovery_codes import consume_login_code, issue_codes
from models.auth import User
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
                return await consume_login_code(db, user_id, codes[0])

        assert sorted(await asyncio.gather(redeem(), redeem())) == [False, True]
        await db_session.refresh(user)
        assert len(user.totp_recovery_hashes) == 7
        # A sign-in that read the account before enrollment cannot complete after it.
        assert not await password_unchanged(
            db_session, user_id, credential_fingerprint(user.password_hash)
        )
        assert await password_unchanged(
            db_session, user_id, credential_fingerprint(user.password_hash, user.mfa_generation)
        )
    finally:
        await db_session.execute(delete(User).where(User.user_id == user_id))
        await db_session.commit()
