"""Password writes racing a password removal (Issue #1678).

``PasswordAccountService.remove`` locks the user row before it checks and
clears the password. ``change`` and the link paths (``complete_reset`` /
``complete_setup``) take the same lock before they re-check the account and
write, so a change or a reset running beside a removal can never write a
password back over the removal (or overwrite a password another request just
changed): exactly one of the two succeeds, and the final state is the
winner's.

Each session's ``commit`` waits (briefly) for the other session to reach its
own commit, as in ``test_password_last_method_race_db.py``: without the lock
both arrive and both commit; with it, the second is still blocked on the row
lock, the first commits after the wait, and the second then sees the
committed state and is refused.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from auth.password import hash_password, verify_password
from models.auth import AuditLog, User, UserOAuthProvider
from services.password_account_service import PasswordAccountService
from utils.datetime import utcnow
from utils.exceptions import (
    ConflictError,
    CurrentPasswordMismatchError,
    PasswordLinkInvalidError,
)

OLD = "Race-Password-123!"
NEW = "Race-Password-456?"
_COMMIT_RENDEZVOUS_SECONDS = 1.0
_REFUSALS = (ConflictError, CurrentPasswordMismatchError, PasswordLinkInvalidError)

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
            password_hash=hash_password(OLD),
            email_verified_at=utcnow(),
        )
    )
    # A provider stays linked, so removing the password is allowed.
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
                # Expected when the lock works: the other session is still
                # blocked on the row lock and never reaches its commit, so
                # this one stops waiting and commits alone.
                pass
            await original()

        session.commit = commit  # type: ignore[method-assign]


async def _final_hash(db: AsyncSession, uid: str) -> str | None:
    db.expire_all()
    return await db.scalar(select(User.password_hash).where(User.user_id == uid))


def _outcome(results: list[object]) -> tuple[object, object]:
    """Return (the one success's result, the one refusal) or fail."""
    successes = [r for r in results if not isinstance(r, BaseException)]
    refusals = [r for r in results if isinstance(r, _REFUSALS)]
    assert len(successes) == 1, results
    assert len(refusals) == 1, results
    return successes[0], refusals[0]


async def test_change_racing_remove_never_resurrects_the_password(
    async_engine: AsyncEngine, db_session: AsyncSession, user_id: str
) -> None:
    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as change_db, factory() as remove_db:
        _hold_commits_until_both_arrive(change_db, remove_db)
        results = await asyncio.gather(
            PasswordAccountService(change_db).change(
                user_id=user_id, current_password=OLD, new_password=NEW
            ),
            PasswordAccountService(remove_db).remove(user_id=user_id, current_password=OLD),
            return_exceptions=True,
        )

    _outcome(results)
    final = await _final_hash(db_session, user_id)
    change_won = not isinstance(results[0], BaseException)
    if change_won:
        assert final is not None and verify_password(NEW, final)
    else:
        assert final is None


async def test_reset_racing_remove_never_resurrects_the_password(
    async_engine: AsyncEngine, db_session: AsyncSession, user_id: str
) -> None:
    email = await db_session.scalar(select(User.email).where(User.user_id == user_id))
    assert email is not None
    pending = await PasswordAccountService(db_session).request_reset(email=email)
    assert pending is not None
    token = parse_qs(urlparse(pending.reset_url).query)["token"][0]

    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as reset_db, factory() as remove_db:
        _hold_commits_until_both_arrive(reset_db, remove_db)
        results = await asyncio.gather(
            PasswordAccountService(reset_db).complete_reset(raw_token=token, new_password=NEW),
            PasswordAccountService(remove_db).remove(user_id=user_id, current_password=OLD),
            return_exceptions=True,
        )

    _outcome(results)
    final = await _final_hash(db_session, user_id)
    reset_won = not isinstance(results[0], BaseException)
    if reset_won:
        assert final is not None and verify_password(NEW, final)
    else:
        assert final is None
