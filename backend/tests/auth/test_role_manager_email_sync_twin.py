"""The provider address sync takes the address lock before its holder check (#1734).

Without a database: a sync of a verified new address first takes
``lock_address`` (so a concurrent account creation for the address is
serialized with it), then looks for another account holding the address in
any letter case. When one does, the address is not synced and no
``oauth_user_email_synced`` row is staged; the name still syncs. Against
Postgres: ``tests/integration/test_role_manager_email_sync_twin_db.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from auth.roles import RoleManager
from tests.auth.test_role_manager_postgres import (
    _LOCK,
    _execute_returns,
    _oauth_link_row,
    _patch_get_db,
    _user_row,
)


def _db(holder: object | None) -> AsyncMock:
    db = AsyncMock()
    existing = _user_row(email="old@example.test", name="Old")
    db.execute = AsyncMock(side_effect=_execute_returns(_oauth_link_row(), existing, _LOCK, holder))
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.add = MagicMock()
    db.existing = existing
    return db


async def _sign_in(db) -> None:
    with _patch_get_db(db):
        await RoleManager(use_postgres=True).ensure_user(
            email="Victim@Example.test",
            user_id="u1",
            name="New",
            auth_provider="google",
            email_verified=True,
        )


@pytest.mark.asyncio
async def test_address_held_elsewhere_keeps_the_old_one() -> None:
    db = _db(holder="email:other")

    await _sign_in(db)

    lock_sql = str(db.execute.await_args_list[2].args[0])
    assert "pg_advisory_xact_lock" in lock_sql
    assert db.execute.await_args_list[2].args[1] == {"address": "victim@example.test"}
    assert db.existing.email == "old@example.test"
    assert db.existing.name == "New"
    assert not [a for a in (c.args[0] for c in db.add.call_args_list) if hasattr(a, "action")]
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_free_address_syncs_after_the_lock() -> None:
    db = _db(holder=None)

    await _sign_in(db)

    assert "pg_advisory_xact_lock" in str(db.execute.await_args_list[2].args[0])
    assert db.existing.email == "Victim@Example.test"
    actions = [getattr(c.args[0], "action", None) for c in db.add.call_args_list]
    assert "oauth_user_email_synced" in actions
