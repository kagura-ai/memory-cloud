"""The last sign-in method survives concurrent removals (Issue #1678).

An account with exactly one password and one linked OAuth provider may drop
either, never both. Removing the password (``PasswordAccountService.remove``)
and unlinking the provider (``AccountLinkingService.unlink``) each check that
the other method remains; run on two sessions at once, both checks can pass
before either commits. Both paths therefore lock the user row first and check
under the lock.

To make the race deterministic, each session's ``commit`` waits (briefly) for
the other session to reach its own commit: without the lock both arrive and
both commit; with it, the second is still blocked on the row lock, the first
commits after the wait, and the second then sees the committed state and is
refused.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from auth.password import hash_password
from models.auth import AuditLog, User, UserOAuthProvider
from services.account_linking_service import AccountLinkingService
from services.password_account_service import PasswordAccountService
from utils.datetime import utcnow
from utils.exceptions import ConflictError

PASSWORD = "Race-Password-123!"
_COMMIT_RENDEZVOUS_SECONDS = 1.0

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest_asyncio.fixture(loop_scope="session")
async def user_id(db_session: AsyncSession) -> AsyncIterator[str]:
    uid = f"u_{uuid4().hex[:10]}"
    db_session.add(
        User(
            user_id=uid,
            email=f"{uid}@race.example",
            name="Race",
            role="user",
            is_initial_admin=False,
            auth_method="oauth",
            auth_provider="github",
            password_hash=hash_password(PASSWORD),
            email_verified_at=utcnow(),
        )
    )
    db_session.add(UserOAuthProvider(user_id=uid, provider="github", oauth_sub=f"sub-{uid}"))
    await db_session.commit()
    yield uid
    await db_session.rollback()
    await db_session.execute(delete(AuditLog).where(AuditLog.user_id == uid))
    await db_session.execute(delete(User).where(User.user_id == uid))  # CASCADE
    await db_session.commit()


def _hold_commits_until_both_arrive(*sessions: AsyncSession) -> None:
    arrived = 0
    both = asyncio.Event()

    for session in sessions:
        original = session.commit

        async def commit(original=original) -> None:
            nonlocal arrived
            arrived += 1
            if arrived >= len(sessions):
                both.set()
            try:
                await asyncio.wait_for(both.wait(), _COMMIT_RENDEZVOUS_SECONDS)
            except TimeoutError:
                pass
            await original()

        session.commit = commit  # type: ignore[method-assign]


async def test_removing_the_password_and_the_provider_at_once_keeps_one(
    async_engine: AsyncEngine, db_session: AsyncSession, user_id: str
) -> None:
    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as remove_db, factory() as unlink_db:
        _hold_commits_until_both_arrive(remove_db, unlink_db)
        results = await asyncio.gather(
            PasswordAccountService(remove_db).remove(user_id=user_id, current_password=PASSWORD),
            AccountLinkingService(unlink_db).unlink(user_id=user_id, provider="github"),
            return_exceptions=True,
        )

    successes = [r for r in results if r is None]
    refusals = [r for r in results if isinstance(r, ConflictError)]
    assert len(successes) == 1, results
    assert len(refusals) == 1, results

    db_session.expire_all()
    password_hash = await db_session.scalar(
        select(User.password_hash).where(User.user_id == user_id)
    )
    providers = await db_session.scalar(
        select(func.count())
        .select_from(UserOAuthProvider)
        .where(UserOAuthProvider.user_id == user_id)
    )
    assert (1 if password_hash is not None else 0) + int(providers or 0) >= 1
