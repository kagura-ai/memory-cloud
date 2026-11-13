"""Email sign-up follows SYSTEM_ADMIN_MODE for the first account (#1734 + #2767).

The email path creates its ``users`` row in ``stage_email_account``, which asks
``new_account_role`` like the OAuth path does: on an empty users table the
first account is the initial admin only in ``session``; in ``off`` and
``service`` it is an ordinary ``user`` with ``is_initial_admin`` false. Later
accounts are users in every mode. The OAuth side: ``test_new_account_role.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import auth.roles as roles_module
from auth.roles import Role, is_initial_admin_role, stage_email_account
from config.settings import get_settings


def _db(user_count: int) -> AsyncMock:
    count = MagicMock()
    count.scalar = MagicMock(return_value=user_count)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=count)
    db.add = MagicMock()
    return db


async def _stage(db) -> Role:
    return await stage_email_account(
        db,
        user_id="email:00000000-0000-4000-8000-000000000000",
        email=" First@Example.test ",
        name=None,
        password_hash="hash",
        locale=None,
        timezone=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "role", "initial_admin"),
    [("session", Role.ADMIN, True), ("off", Role.USER, False), ("service", Role.USER, False)],
)
async def test_first_email_account(monkeypatch, mode, role, initial_admin) -> None:
    monkeypatch.setattr(get_settings(), "system_admin_mode", mode)
    db = _db(0)

    got = await _stage(db)

    assert got == role
    added = db.add.call_args.args[0]
    assert (added.role, added.is_initial_admin) == (role.value, initial_admin)
    assert added.email == "first@example.test"
    # new_account_role: one count; in session on an empty table the
    # first-account lock and a recount follow (#2788).
    assert db.execute.await_count == (3 if mode == "session" else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["session", "off", "service"])
async def test_later_email_accounts_are_users_in_every_mode(monkeypatch, mode) -> None:
    monkeypatch.setattr(get_settings(), "system_admin_mode", mode)
    db = _db(3)

    assert await _stage(db) == Role.USER
    assert db.add.call_args.args[0].is_initial_admin is False


@pytest.mark.asyncio
async def test_email_path_asks_new_account_role(monkeypatch) -> None:
    asked = AsyncMock(return_value=Role.USER)
    monkeypatch.setattr(roles_module, "new_account_role", asked)
    db = _db(0)

    assert await _stage(db) == Role.USER

    asked.assert_awaited_once_with(db)
    assert db.add.call_args.args[0].is_initial_admin is False


@pytest.mark.parametrize(("role", "marker"), [(Role.ADMIN, True), (Role.USER, False)])
def test_initial_admin_marker_follows_the_chosen_role(role, marker) -> None:
    # A bootstrap marker, not a grant: the role new_account_role chose decides it.
    assert is_initial_admin_role(role) is marker
