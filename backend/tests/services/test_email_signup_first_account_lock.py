"""Email sign-up completion takes the first-account lock before the address lock (#2788).

``EmailSignupService._create`` decides the gate's first-user admission and
inserts the account in one transaction. On an empty users table it takes the
first-account lock (``claim_first_account(serialize=True)``) before
``lock_address`` in every ``SYSTEM_ADMIN_MODE``: that serialises email
completions with each other and with every session-mode creator, and keeps
one lock order (first-account, then address, then rows) on every creating
path, as the OAuth new-user path already has. Pinned without a database here;
with two real connections in ``tests/integration/test_first_account_race_db.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from auth.roles import FIRST_ACCOUNT_LOCK_KEY
from config.settings import get_settings
from services.email_signup_service import EmailSignupService, SignupConsents
from utils.exceptions import PasswordLinkInvalidError


def _empty_result() -> MagicMock:
    result = MagicMock()
    result.scalar = MagicMock(return_value=0)
    result.first = MagicMock(return_value=None)
    return result


async def _create_until_the_link_check(db: AsyncMock) -> None:
    svc = EmailSignupService(db, email_service=MagicMock())
    with pytest.raises(PasswordLinkInvalidError):
        await svc._create(
            gate=MagicMock(),
            gate_enabled=False,
            gate_mode="closed",
            token_hash="0" * 64,
            email="first@example.com",
            password_hash="hash",
            name=None,
            consents=SignupConsents(None),
            current_terms=None,
            ip_address=None,
            user_agent=None,
        )


def _db(count: int = 0) -> AsyncMock:
    result = _empty_result()
    result.scalar.return_value = count
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    db.rollback = AsyncMock()
    db.commit = AsyncMock()
    return db


def _kind(call) -> str:
    sql = str(call.args[0])
    if "pg_advisory_xact_lock" in sql and "'email_signup:'" in sql:
        return "address_lock"
    if "pg_advisory_xact_lock" in sql:
        assert call.args[1] == {"key": FIRST_ACCOUNT_LOCK_KEY}
        return "first_account_lock"
    if "count(" in sql.lower():
        return "count"
    return "other"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["session", "off", "service"])
async def test_first_account_lock_comes_before_the_address_lock(monkeypatch, mode) -> None:
    monkeypatch.setattr(get_settings(), "system_admin_mode", mode)
    db = _db(0)

    await _create_until_the_link_check(db)

    kinds = [_kind(c) for c in db.execute.await_args_list]
    assert kinds[:4] == ["count", "first_account_lock", "count", "address_lock"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["session", "off", "service"])
async def test_existing_accounts_add_one_count_and_no_lock(monkeypatch, mode) -> None:
    monkeypatch.setattr(get_settings(), "system_admin_mode", mode)
    db = _db(2)

    await _create_until_the_link_check(db)

    kinds = [_kind(c) for c in db.execute.await_args_list]
    assert kinds[:2] == ["count", "address_lock"]
    assert "first_account_lock" not in kinds
