"""Deleting an inviter's account keeps the invitee's admission record (#2803).

``users.admitted_by_user_id`` is a foreign key to ``users.user_id`` with
``ON DELETE SET NULL``: every path that deletes the inviter's row clears it,
and nothing else. ``admitted_via``, the issuer and the invite id stay:

- account erasure (``AccountErasureService._delete_postgres``);
- ``DELETE /admin/users/{id}`` (the handler, through ``delete_user_rows``);
- ``delete_user_rows`` and the ``delete_admin`` CLI's row deletion;
- an inviter erased between the gate's redemption and ``ensure_user``: the
  new row is written with no inviter (the KEY SHARE pre-check sees the row
  gone), never an FK error;
- erasing the invited account removes its row as before.

Run on a private database only (``REQUIRE_TEST_DATABASE=1 TEST_DATABASE_URL=...``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.routes.admin import delete_user
from auth.admission import AdmissionRecord
from auth.roles import RoleManager
from cli.delete_admin import _delete_admin_rows
from config.settings import get_settings
from models.auth import User
from models.beta_invite import BetaInvite
from models.signup_gate import SignupAllowlistEntry, SignupGateConfig
from services.account_erasure_service import AccountErasureService
from services.signup_gate_service import SignupGateService
from services.user_deletion_service import delete_user_rows
from utils.datetime import utcnow
from utils.hashing import sha256_hex

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:10]}"


@pytest_asyncio.fixture(loop_scope="session")
async def maker(async_engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture(loop_scope="session")
async def people(db_session: AsyncSession) -> AsyncIterator[SimpleNamespace]:
    made: list[str] = []

    async def person(**cols) -> str:
        user_id = cols.pop("user_id", _uid("p"))
        db_session.add(
            User(
                email=f"{user_id.replace(':', '-')}@example.test",
                user_id=user_id,
                role=cols.pop("role", "user"),
                auth_method=cols.pop("auth_method", "oauth"),
                auth_provider=cols.pop("auth_provider", "google"),
                **cols,
            )
        )
        await db_session.commit()
        made.append(user_id)
        return user_id

    async def invitee(inviter: str) -> tuple[str, UUID]:
        invite_id = uuid4()
        user_id = await person(
            admitted_via="beta_invite",
            admitted_beta_invite_id=invite_id,
            admitted_invite_issuer="user",
            admitted_by_user_id=inviter,
        )
        return user_id, invite_id

    async def record(user_id: str) -> tuple:
        row = (
            await db_session.execute(
                select(
                    User.admitted_via,
                    User.admitted_beta_invite_id,
                    User.admitted_invite_issuer,
                    User.admitted_by_user_id,
                )
                .where(User.user_id == user_id)
                .execution_options(populate_existing=True)
            )
        ).one_or_none()
        return tuple(row) if row is not None else ()

    yield SimpleNamespace(db=db_session, person=person, invitee=invitee, record=record)
    await db_session.rollback()
    await db_session.execute(delete(User).where(User.user_id.in_(made)))
    await db_session.commit()


async def _inviter_gone(db: AsyncSession, inviter: str) -> None:
    db.expire_all()
    assert await db.scalar(select(User.user_id).where(User.user_id == inviter)) is None


async def test_erasure_clears_only_the_inviter_link(people, maker) -> None:
    inviter = await people.person()
    invitee, invite_id = await people.invitee(inviter)
    other, other_invite = await people.invitee(inviter)

    async with maker() as s:
        target = await s.scalar(select(User).where(User.user_id == inviter))
        await AccountErasureService(s)._delete_postgres(target)

    await _inviter_gone(people.db, inviter)
    assert await people.record(invitee) == ("beta_invite", invite_id, "user", None)
    assert await people.record(other) == ("beta_invite", other_invite, "user", None)


async def test_admin_api_delete_clears_only_the_inviter_link(people, maker, vectors) -> None:
    inviter = await people.person()
    invitee, invite_id = await people.invitee(inviter)

    async with maker() as s:
        await delete_user(inviter, {"user_id": "operator"}, s)

    await _inviter_gone(people.db, inviter)
    assert await people.record(invitee) == ("beta_invite", invite_id, "user", None)


async def test_delete_user_rows_clears_only_the_inviter_link(people, maker, vectors) -> None:
    inviter = await people.person()
    invitee, invite_id = await people.invitee(inviter)

    async with maker() as s:
        await delete_user_rows(s, inviter)
        await s.commit()

    await _inviter_gone(people.db, inviter)
    assert await people.record(invitee) == ("beta_invite", invite_id, "user", None)


async def test_delete_admin_cli_clears_only_the_inviter_link(people, maker, vectors) -> None:
    login = f"adm{uuid4().hex[:8]}"
    inviter = await people.person(
        user_id=f"local:{login}",
        login_id=login,
        role="admin",
        auth_method="password",
        auth_provider=None,
        admitted_via="admin_cli",
    )
    invitee, invite_id = await people.invitee(inviter)

    async with maker() as s:
        await _delete_admin_rows(s, inviter, [])

    await _inviter_gone(people.db, inviter)
    assert await people.record(invitee) == ("beta_invite", invite_id, "user", None)


async def test_erasing_the_invitee_removes_their_row(people, maker) -> None:
    inviter = await people.person()
    invitee, _ = await people.invitee(inviter)

    async with maker() as s:
        target = await s.scalar(select(User).where(User.user_id == invitee))
        await AccountErasureService(s)._delete_postgres(target)

    assert await people.record(invitee) == ()
    assert await people.record(inviter) == ("unknown", None, None, None)


async def test_inviter_erased_between_redemption_and_insert(
    people, maker, db_session, monkeypatch
) -> None:
    monkeypatch.setattr(get_settings(), "enable_beta_invites", True)
    gate = SignupGateService(db_session)
    config = await gate.get_config()
    previous = (config.enabled, config.mode)
    await gate.update_config(enabled=True, mode="manual")
    sub, email = _uid("go"), f"{_uid('race')}@example.test"
    try:
        inviter = await people.person()
        token = uuid4().hex + uuid4().hex
        invite = BetaInvite(
            token_hash=sha256_hex(token),
            inviter_user_id=inviter,
            expires_at=utcnow() + timedelta(days=7),
        )
        db_session.add(invite)
        await db_session.commit()
        invite_id = invite.id

        async with maker() as s:
            answer = await SignupGateService(s).decide(
                provider="google",
                oauth_sub=sub,
                email=email,
                username=email,
                beta_invite_token_hash=sha256_hex(token),
                email_verified=True,
            )
        assert answer == AdmissionRecord("beta_invite", invite_id, inviter)

        async with maker() as s:
            target = await s.scalar(select(User).where(User.user_id == inviter))
            await AccountErasureService(s)._delete_postgres(target)

        async def _fake_get_db():
            async with maker() as s:
                yield s

        with patch("db.base.get_db", new=_fake_get_db):
            await RoleManager(use_postgres=True).ensure_user(
                email=email,
                user_id=sub,
                auth_provider="google",
                email_verified=True,
                admission=answer,
            )
        assert await people.record(sub) == ("beta_invite", invite_id, None, None)
    finally:
        await db_session.rollback()
        await db_session.execute(
            update(SignupGateConfig)
            .where(SignupGateConfig.id == 1)
            .values(enabled=previous[0], mode=previous[1])
        )
        await db_session.execute(
            delete(SignupAllowlistEntry).where(SignupAllowlistEntry.subject_id == sub)
        )
        await db_session.execute(delete(User).where(User.user_id == sub))
        await db_session.commit()
