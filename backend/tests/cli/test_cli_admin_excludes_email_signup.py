"""The CLI's "password admin" is never an account created by email sign-up (#1734).

An email sign-up account has ``auth_method='password'`` and no ``login_id``;
it can hold ``role='admin'`` (first account in ``session`` mode, or promoted
in the admin UI). ``create_admin``, ``delete_admin`` and ``seed_e2e_admin``
look for the CLI admin and must require ``login_id IS NOT NULL``, or a second
password admin would block ``create_admin`` and make ``delete_admin``'s
``scalar_one_or_none`` raise.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_BACKEND_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

from cli import create_admin, delete_admin, seed_e2e_admin  # noqa: E402


@pytest.mark.asyncio
async def test_delete_admin_lookup_requires_a_login_id() -> None:
    db = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=result)

    await delete_admin._password_admin(db)

    stmt = db.execute.await_args.args[0]
    sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "users.login_id IS NOT NULL" in sql
    assert "users.auth_method = 'password'" in sql


@pytest.mark.parametrize("module", [create_admin, seed_e2e_admin])
def test_admin_count_requires_a_login_id(module) -> None:
    source = inspect.getsource(module)
    assert source.count('User.role == "admin"') == source.count("User.login_id.is_not(None)")
    assert "User.login_id.is_not(None)" in source
