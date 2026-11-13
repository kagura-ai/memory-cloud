"""The first-account lock: one initial admin when first sign-ups race (#2788).

``claim_first_account`` counts the users table; only while it is empty and the
caller asks for it does it take the transaction-scoped first-account advisory
lock and count again in a new statement, so of two transactions creating the
first account the second waits and then sees the first one's row. Once any
account exists it is one statement and never waits. ``new_account_role``
serialises only in ``SYSTEM_ADMIN_MODE=session`` (the only mode that promotes).
Two real connections: ``tests/integration/test_first_account_race_db.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from auth.roles import FIRST_ACCOUNT_LOCK_KEY, Role, claim_first_account, new_account_role
from config.settings import get_settings


def _count(value: int | None) -> MagicMock:
    result = MagicMock()
    result.scalar = MagicMock(return_value=value)
    return result


def _db(*counts: int | None) -> AsyncMock:
    """A session whose count statements return ``counts`` in order (lock calls in between)."""
    results = iter(counts)

    async def _execute(statement, params=None):
        if "pg_advisory_xact_lock" in str(statement):
            return MagicMock()
        return _count(next(results))

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=_execute)
    return db


def _is_lock(call) -> bool:
    return "pg_advisory_xact_lock" in str(call.args[0])


def test_lock_key_is_a_constant_without_personal_data() -> None:
    assert FIRST_ACCOUNT_LOCK_KEY == "first_account"


class TestClaimFirstAccount:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("serialize", [True, False])
    async def test_existing_accounts_cost_one_statement_and_no_lock(self, serialize) -> None:
        db = _db(3)
        assert await claim_first_account(db, serialize=serialize) is False
        assert db.execute.await_count == 1
        assert not any(_is_lock(c) for c in db.execute.await_args_list)

    @pytest.mark.asyncio
    async def test_empty_table_locks_then_counts_again(self) -> None:
        db = _db(0, 0)
        assert await claim_first_account(db, serialize=True) is True
        calls = db.execute.await_args_list
        assert len(calls) == 3
        assert [_is_lock(c) for c in calls] == [False, True, False]
        lock = calls[1]
        assert "hashtextextended" in str(lock.args[0])
        assert lock.args[1] == {"key": FIRST_ACCOUNT_LOCK_KEY}

    @pytest.mark.asyncio
    async def test_lost_race_sees_the_winners_row(self) -> None:
        db = _db(0, 1)
        assert await claim_first_account(db, serialize=True) is False
        assert db.execute.await_count == 3

    @pytest.mark.asyncio
    async def test_without_serialize_no_lock_on_an_empty_table(self) -> None:
        db = _db(0)
        assert await claim_first_account(db, serialize=False) is True
        assert db.execute.await_count == 1
        assert not any(_is_lock(c) for c in db.execute.await_args_list)

    @pytest.mark.asyncio
    async def test_null_count_counts_as_empty(self) -> None:
        db = _db(None, None)
        assert await claim_first_account(db, serialize=True) is True


class TestNewAccountRole:
    @pytest.mark.asyncio
    async def test_session_first_account_is_admin_under_the_lock(self, monkeypatch) -> None:
        monkeypatch.setattr(get_settings(), "system_admin_mode", "session")
        db = _db(0, 0)
        assert await new_account_role(db) == Role.ADMIN
        assert [_is_lock(c) for c in db.execute.await_args_list] == [False, True, False]

    @pytest.mark.asyncio
    async def test_session_loser_of_the_race_is_a_user(self, monkeypatch) -> None:
        monkeypatch.setattr(get_settings(), "system_admin_mode", "session")
        db = _db(0, 1)
        assert await new_account_role(db) == Role.USER
        assert db.execute.await_count == 3

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["off", "service"])
    async def test_off_and_service_never_lock(self, monkeypatch, mode) -> None:
        monkeypatch.setattr(get_settings(), "system_admin_mode", mode)
        db = _db(0)
        assert await new_account_role(db) == Role.USER
        assert db.execute.await_count == 1
        assert not any(_is_lock(c) for c in db.execute.await_args_list)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["session", "off", "service"])
    async def test_later_accounts_one_statement_in_every_mode(self, monkeypatch, mode) -> None:
        monkeypatch.setattr(get_settings(), "system_admin_mode", mode)
        db = _db(4)
        assert await new_account_role(db) == Role.USER
        assert db.execute.await_count == 1
