"""A provider address sync never makes a case-variant twin, on Postgres (#1734).

``users.email`` is UNIQUE but case-sensitive. When an existing account's
provider reports a new verified address that ANOTHER account already holds
in any letter case (for example an email sign-up account holding
``victim@...`` while the provider now says ``Victim@...``), the sync keeps
the old address instead: the sign-in still succeeds, nothing is written to
the address, no ``oauth_user_email_synced`` row, and the other account
keeps the only copy of the address (its sign-in by address keeps working).
An address nobody else holds still syncs.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import patch
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from auth.roles import Role, RoleManager
from models.auth import AuditLog, User, UserOAuthProvider
from utils.datetime import utcnow

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _fresh_get_db(async_engine):
    maker = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)

    @asynccontextmanager
    async def _scope():
        async with maker() as s:
            yield s

    async def _fake():
        async with _scope() as s:
            yield s

    return patch("db.base.get_db", new=_fake)


@pytest_asyncio.fixture(loop_scope="session")
async def accounts(db_session: AsyncSession) -> dict[str, str]:
    suffix = uuid4().hex[:8]
    holder = User(
        email=f"victim-{suffix}@example.test",
        user_id=f"email:{uuid4()}",
        role="user",
        auth_method="password",
        password_hash="x",
        email_verified_at=utcnow(),
    )
    oauth = User(
        email=f"old-{suffix}@example.test",
        user_id=f"g-sync-{suffix}",
        name="Old",
        role="user",
        auth_method="oauth",
        auth_provider="google",
        email_verified_at=utcnow(),
    )
    db_session.add_all([holder, oauth])
    db_session.add(
        UserOAuthProvider(user_id=oauth.user_id, provider="google", oauth_sub=oauth.user_id)
    )
    await db_session.commit()
    return {
        "holder_email": holder.email,
        "holder_id": holder.user_id,
        "oauth_email": oauth.email,
        "oauth_id": oauth.user_id,
        "suffix": suffix,
    }


async def _sign_in(db_session: AsyncSession, accounts: dict[str, str], email: str) -> Role:
    with (
        _fresh_get_db(db_session.bind),
        patch("services.security_notification_service.spawn_email_change_notification"),
    ):
        return await RoleManager(use_postgres=True).ensure_user(
            email=email,
            user_id=accounts["oauth_id"],
            name="New Name",
            auth_provider="google",
            email_verified=True,
        )


async def _row(db_session: AsyncSession, user_id: str) -> User:
    db_session.expire_all()
    return (await db_session.execute(select(User).where(User.user_id == user_id))).scalar_one()


async def _synced(db_session: AsyncSession, user_id: str) -> int:
    return int(
        await db_session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.user_id == user_id, AuditLog.action == "oauth_user_email_synced")
        )
        or 0
    )


@pytest.mark.parametrize("variant", [str.capitalize, str.upper, str.lower])
async def test_address_held_by_another_account_is_not_synced(
    db_session: AsyncSession, accounts, variant
) -> None:
    claimed = variant(accounts["holder_email"])

    role = await _sign_in(db_session, accounts, claimed)

    assert role == Role.USER
    oauth = await _row(db_session, accounts["oauth_id"])
    assert oauth.email == accounts["oauth_email"]
    assert oauth.name == "New Name"  # the rest of the sync still happens
    assert await _synced(db_session, accounts["oauth_id"]) == 0
    holders = (
        await db_session.scalars(
            select(User.user_id).where(func.lower(User.email) == accounts["holder_email"].lower())
        )
    ).all()
    assert holders == [accounts["holder_id"]]


async def test_a_free_address_still_syncs(db_session: AsyncSession, accounts) -> None:
    new_email = f"renamed-{accounts['suffix']}@example.test"

    await _sign_in(db_session, accounts, new_email)

    assert (await _row(db_session, accounts["oauth_id"])).email == new_email
    assert await _synced(db_session, accounts["oauth_id"]) == 1
