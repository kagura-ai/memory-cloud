"""The OAuth new-user path asks new_account_role for the first account's role (#2767).

In ``off`` and ``service`` the first account on an empty users table is an
ordinary ``user`` (not initial admin, so its personal workspace is Free); in
``session`` it is the initial admin, as before.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from auth.roles import Role, RoleManager
from config.settings import get_settings


def _db(user_count: int, *, first_lock: bool = False) -> AsyncMock:
    link_miss = MagicMock()
    link_miss.scalar_one_or_none = MagicMock(return_value=None)
    count = MagicMock()
    count.scalar = MagicMock(return_value=user_count)
    # #2788: on an empty table in session mode, the first-account lock and
    # the recount follow the count.
    first_account = [MagicMock(), count] if first_lock else []
    # #1734: the address lock, then the (empty) lookup of accounts holding it.
    lock = MagicMock()
    no_holders = MagicMock()
    no_holders.scalars.return_value.all.return_value = []
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[link_miss, count, *first_account, lock, no_holders])
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.add = MagicMock()
    return db


def _patch_get_db(db):
    async def _fake_get_db():
        yield db

    return patch("db.base.get_db", new=_fake_get_db)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "role", "initial_admin"),
    [("session", Role.ADMIN, True), ("off", Role.USER, False), ("service", Role.USER, False)],
)
@pytest.mark.parametrize("provider", ["google", "github"])
async def test_first_oauth_account(monkeypatch, mode, role, initial_admin, provider):
    monkeypatch.setattr(get_settings(), "system_admin_mode", mode)
    session = mode == "session"
    db = _db(0, first_lock=session)
    with _patch_get_db(db):
        got = await RoleManager(use_postgres=True).ensure_user(
            email="first@example.com",
            user_id="1234567",
            auth_provider=provider,
            email_verified=True,
        )
    assert got == role
    added = db.add.call_args_list[0].args[0]
    assert added.role == role.value
    assert added.is_initial_admin is initial_admin
    # link lookup, count, [first-account lock, recount: session only], address lock, holders
    assert db.execute.await_count == (6 if session else 4)
    sql = [str(c.args[0]) for c in db.execute.await_args_list]
    first_lock = [i for i, q in enumerate(sql) if "hashtextextended(:key" in q]
    address_lock = [i for i, q in enumerate(sql) if "'email_signup:'" in q]
    assert len(address_lock) == 1
    if session:
        # #2788: the first-account lock comes before the address lock.
        assert len(first_lock) == 1
        assert first_lock[0] < address_lock[0]
    else:
        assert first_lock == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["session", "off", "service"])
async def test_later_accounts_are_users_in_every_mode(monkeypatch, mode):
    monkeypatch.setattr(get_settings(), "system_admin_mode", mode)
    db = _db(5)
    with _patch_get_db(db):
        got = await RoleManager(use_postgres=True).ensure_user(
            email="later@example.com", user_id="7654321", auth_provider="google"
        )
    assert got == Role.USER
    assert db.add.call_args_list[0].args[0].is_initial_admin is False
