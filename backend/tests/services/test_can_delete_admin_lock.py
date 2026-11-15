"""``SystemAdminService.can_delete_admin`` decides under the role-change lock (#2855).

Its first statement is the one ``lock_for_role_change`` takes (the admin rows
and the target, ``user_id`` order, ``FOR NO KEY UPDATE``), for any target;
the admin count is a statement of its own after it. Its answers are those it
gave before. The two-session behaviour is in
``tests/integration/test_admin_deletion_concurrency_db.py``.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from models.auth import User
from services.system_admin_service import SystemAdminService
from tests.api.test_admin_role_protections import _sql, account, role_change_session

INITIAL = (False, "Cannot delete the initial system administrator")
LAST = (False, "Cannot delete the last remaining system administrator")


def _statements(session) -> list[str]:
    return [" ".join(_sql(statement).split()) for statement in session.statements]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rows", "answer"),
    [
        ([account("1001", "admin"), account("2002", "user")], (True, "")),
        ([account("1001", "admin")], (True, "")),  # 2002 is unknown
        ([account("1001", "admin"), account("2002", "admin", initial=True)], INITIAL),
        ([account("2002", "admin", initial=True)], INITIAL),
        ([account("2002", "admin"), account("3003", "user")], LAST),
        ([account("1001", "admin"), account("2002", "admin")], (True, "")),
    ],
)
async def test_can_delete_admin_answers_as_before(rows, answer):
    session = role_change_session(*rows)
    assert await SystemAdminService(session).can_delete_admin("2002") == answer


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["admin", "user"])
async def test_can_delete_admin_locks_first_for_any_target(role):
    session = role_change_session(account("1001", "admin"), account("2002", role))
    await SystemAdminService(session).can_delete_admin("2002")
    lock = _statements(session)[0]
    assert "WHERE users.role = 'admin' OR users.user_id = '2002'" in lock
    assert lock.endswith("ORDER BY users.user_id FOR NO KEY UPDATE")
    assert session.statements[0].get_execution_options().get("populate_existing") is True
    session.add.assert_not_called()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_can_delete_admin_counts_the_admins_after_the_lock():
    session = role_change_session(account("1001", "admin"), account("2002", "admin"))
    await SystemAdminService(session).can_delete_admin("2002")
    lock, count = _statements(session)
    assert "FOR NO KEY UPDATE" in lock
    assert count.startswith("SELECT count(*)")
    assert "WHERE users.role = 'admin'" in count
    assert "FOR " not in count


@pytest.mark.asyncio
async def test_can_delete_admin_locks_the_also_lock_rows_in_the_same_statement():
    session = role_change_session(account("1001", "admin"), account("2002", "user"))
    also = User.user_id.in_(select(User.user_id).where(User.email == "successor@example.com"))
    await SystemAdminService(session).can_delete_admin("2002", also_lock=also)
    [lock] = _statements(session)
    assert (
        "WHERE users.role = 'admin' OR users.user_id = '2002' OR users.user_id IN "
        "(SELECT users.user_id FROM users WHERE users.email = 'successor@example.com')"
    ) in lock
    assert lock.endswith("ORDER BY users.user_id FOR NO KEY UPDATE")


@pytest.mark.asyncio
async def test_the_role_change_and_the_deletion_take_the_same_lock_statement():
    one = role_change_session(account("1001", "admin"), account("2002", "admin"))
    two = role_change_session(account("1001", "admin"), account("2002", "admin"))
    await SystemAdminService(one).lock_for_role_change("2002", "user")
    await SystemAdminService(two).can_delete_admin("2002")
    assert _statements(one)[0] == _statements(two)[0]
