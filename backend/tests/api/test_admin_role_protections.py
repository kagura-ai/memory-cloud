"""PUT /api/v1/admin/users/{user_id}/role keeps the initial admin and the last admin (#2828).

The role PUT and ``DELETE /api/v1/admin/system-admins/{user_id}`` share one
check, ``SystemAdminService.lock_for_role_change``: it locks the admin rows
and the target in ``user_id`` order, counts the admins after the lock, and
refuses with 403 ``ADMIN-001`` before anything is written. Both rules are
about accounts with ``role = 'admin'`` and hold in every ``SYSTEM_ADMIN_MODE``.

The session here is a fake that answers the two statements of the check; the
two-session behaviour is in
``tests/integration/test_system_admin_demotion_concurrency_db.py``.
"""

from __future__ import annotations

import json
import secrets
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from api.main import app
from auth.dependencies import get_current_user_optional
from config.settings import get_settings
from db.base import get_db
from models.auth import AuditLog
from services.system_admin_service import SystemAdminService
from utils.exceptions import AdminProtectionError

INITIAL_ADMIN_MESSAGE = (
    "Cannot demote the initial system administrator. This is a protected account."
)
LAST_ADMIN_MESSAGE = (
    "Cannot demote the last remaining system administrator. At least one admin must exist."
)


def account(user_id: str, role: str, *, initial: bool = False) -> SimpleNamespace:
    """A stand-in for a ``users`` row."""
    return SimpleNamespace(
        user_id=user_id,
        role=role,
        is_initial_admin=initial,
        email=f"{user_id}@example.com",
    )


class _Rows:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self._rows = rows

    def scalars(self) -> _Rows:
        return self

    def all(self) -> list[SimpleNamespace]:
        return list(self._rows)


class _Count:
    def __init__(self, value: int) -> None:
        self._value = value

    def scalar_one(self) -> int:
        return self._value

    def scalar(self) -> int:
        return self._value


def role_change_session(*accounts: SimpleNamespace) -> MagicMock:
    """A session that answers the lock and the count of a role change.

    ``accounts`` are the rows the lock statement returns (the admins and the
    target), handed back in ``user_id`` order. Every statement is kept in
    ``session.statements`` in execution order.
    """
    session = MagicMock()
    session.accounts = {row.user_id: row for row in accounts}
    session.statements = []

    async def _execute(statement, *args, **kwargs):
        session.statements.append(statement)
        if getattr(statement, "_for_update_arg", None) is not None:
            return _Rows(sorted(accounts, key=lambda row: row.user_id))
        return _Count(sum(1 for row in accounts if row.role == "admin"))

    session.execute = AsyncMock(side_effect=_execute)
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    return session


def audit_rows(session: MagicMock) -> list[AuditLog]:
    return [
        call.args[0] for call in session.add.call_args_list if isinstance(call.args[0], AuditLog)
    ]


def _sql(statement) -> str:
    return str(
        statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )


@pytest.fixture
def serve():
    """Mount a fake session and hand back a client."""

    def _serve(session: MagicMock) -> TestClient:
        async def _db():
            yield session

        app.dependency_overrides[get_db] = _db
        return TestClient(app, raise_server_exceptions=False)

    yield _serve
    app.dependency_overrides.clear()


@pytest.fixture
def session_admin(monkeypatch):
    monkeypatch.setattr(get_settings(), "system_admin_mode", "session")

    async def _admin():
        return {"user_id": "1001", "email": "admin@example.com", "role": "admin"}

    app.dependency_overrides[get_current_user_optional] = _admin
    return {}


@pytest.fixture
def operator(monkeypatch):
    token = secrets.token_hex(32)
    settings = get_settings()
    monkeypatch.setattr(settings, "system_admin_mode", "service")
    monkeypatch.setattr(settings, "system_admin_service_token", token)
    monkeypatch.setattr(settings, "system_admin_service_token_previous", "")
    monkeypatch.setattr(settings, "system_admin_service_allowed_cidrs", "")
    return {"Authorization": f"Bearer {token}", "X-Operator-Email": "ops@example.com"}


def _assert_refused(response, session: MagicMock, message: str) -> None:
    assert response.status_code == 403, response.text
    body = response.json()
    assert body["error"] == "ADMIN-001"
    assert body["message"] == message
    assert audit_rows(session) == []
    session.add.assert_not_called()
    session.commit.assert_not_awaited()
    session.rollback.assert_awaited_once()


# --- the initial admin -------------------------------------------------------


def test_put_moving_the_initial_admin_away_from_admin_is_403_admin_001(serve, session_admin):
    initial = account("2002", "admin", initial=True)
    session = role_change_session(account("1001", "admin"), initial)
    response = serve(session).put("/api/v1/admin/users/2002/role", json={"role": "user"})
    _assert_refused(response, session, INITIAL_ADMIN_MESSAGE)
    assert initial.role == "admin"


def test_put_answers_the_initial_admin_as_the_demote_route_does(serve, session_admin):
    def _rows():
        return account("1001", "admin"), account("2002", "admin", initial=True)

    put = serve(role_change_session(*_rows())).put(
        "/api/v1/admin/users/2002/role", json={"role": "user"}
    )
    demote = serve(role_change_session(*_rows())).delete("/api/v1/admin/system-admins/2002")
    assert put.status_code == demote.status_code == 403
    assert put.json()["error"] == demote.json()["error"] == "ADMIN-001"
    assert put.json()["message"] == demote.json()["message"] == INITIAL_ADMIN_MESSAGE


def test_put_keeping_the_initial_admin_an_admin_is_not_refused(serve, session_admin):
    session = role_change_session(account("2002", "admin", initial=True))
    response = serve(session).put("/api/v1/admin/users/2002/role", json={"role": "admin"})
    assert response.status_code == 200, response.text


def _take_admin_role(client: TestClient, route: str, user_id: str, headers: dict):
    """Ask one of the two routes to take the admin role from ``user_id``."""
    if route == "DELETE":
        return client.delete(f"/api/v1/admin/system-admins/{user_id}", headers=headers)
    return client.put(
        f"/api/v1/admin/users/{user_id}/role",
        json={"role": route.removeprefix("PUT:")},
        headers=headers,
    )


ROUTES = ["PUT:user", "DELETE"]


@pytest.mark.parametrize("route", ROUTES)
def test_the_initial_admin_is_kept_for_an_operator_in_service_mode_on_both_routes(
    serve, operator, route
):
    initial = account("2002", "admin", initial=True)
    session = role_change_session(account("1001", "admin"), initial)
    response = _take_admin_role(serve(session), route, "2002", operator)
    assert response.status_code == 403, response.text
    assert response.json()["error"] == "ADMIN-001"
    assert response.json()["message"] == INITIAL_ADMIN_MESSAGE
    assert initial.role == "admin"
    session.add.assert_not_called()
    session.commit.assert_not_awaited()


def test_the_initial_admin_who_is_also_the_last_admin_is_answered_as_the_initial_admin(
    serve, operator
):
    session = role_change_session(account("2002", "admin", initial=True))
    for route in ROUTES:
        response = _take_admin_role(serve(session), route, "2002", operator)
        assert response.status_code == 403
        assert response.json()["message"] == INITIAL_ADMIN_MESSAGE


# --- the last admin ----------------------------------------------------------


@pytest.mark.parametrize("route", ROUTES)
def test_the_last_admin_is_kept_for_an_operator_in_service_mode_on_both_routes(
    serve, operator, route
):
    last = account("2002", "admin")
    session = role_change_session(last, account("3003", "user"))
    response = _take_admin_role(serve(session), route, "2002", operator)
    assert response.status_code == 403, response.text
    assert response.json()["error"] == "ADMIN-001"
    assert response.json()["message"] == LAST_ADMIN_MESSAGE
    assert last.role == "admin"
    session.add.assert_not_called()
    session.commit.assert_not_awaited()


def test_put_on_the_last_admin_is_403_admin_001_in_session_mode(serve, session_admin):
    last = account("2002", "admin")
    session = role_change_session(last, account("3003", "user"))
    response = serve(session).put("/api/v1/admin/users/2002/role", json={"role": "user"})
    _assert_refused(response, session, LAST_ADMIN_MESSAGE)
    assert last.role == "admin"


def test_put_on_the_last_admin_is_403_admin_001_for_an_operator_in_service_mode(serve, operator):
    last = account("2002", "admin")
    session = role_change_session(last)
    response = serve(session).put(
        "/api/v1/admin/users/2002/role", json={"role": "user"}, headers=operator
    )
    _assert_refused(response, session, LAST_ADMIN_MESSAGE)
    assert last.role == "admin"


def test_put_answers_the_last_admin_as_the_demote_route_does(serve, operator):
    put = serve(role_change_session(account("2002", "admin"))).put(
        "/api/v1/admin/users/2002/role", json={"role": "user"}, headers=operator
    )
    demote = serve(role_change_session(account("2002", "admin"))).delete(
        "/api/v1/admin/system-admins/2002", headers=operator
    )
    assert put.status_code == demote.status_code == 403
    assert put.json()["error"] == demote.json()["error"] == "ADMIN-001"
    assert put.json()["message"] == demote.json()["message"] == LAST_ADMIN_MESSAGE


def test_put_demotes_one_of_two_admins_and_refuses_the_one_left(serve, operator):
    # Exactly two admins: the demotion is allowed (the boundary of the count).
    first, second = account("2002", "admin"), account("3003", "admin")
    session = role_change_session(first, second)
    response = serve(session).put(
        "/api/v1/admin/users/2002/role", json={"role": "user"}, headers=operator
    )
    assert response.status_code == 200, response.text
    assert first.role == "user"
    # The fake now counts one admin: the second demotion is refused.
    response = serve(session).put(
        "/api/v1/admin/users/3003/role", json={"role": "user"}, headers=operator
    )
    assert response.status_code == 403
    assert response.json()["message"] == LAST_ADMIN_MESSAGE
    assert second.role == "admin"
    assert len(audit_rows(session)) == 1


# --- what stays as it was ----------------------------------------------------


@pytest.mark.parametrize(
    ("old_role", "new_role"),
    [("user", "user"), ("user", "admin")],
)
def test_a_plain_write_and_a_promotion_keep_their_audit_row(
    serve, session_admin, old_role, new_role
):
    target = account("2002", old_role)
    session = role_change_session(account("1001", "admin"), target)
    response = serve(session).put("/api/v1/admin/users/2002/role", json={"role": new_role})
    assert response.status_code == 200, response.text
    assert response.json() == {
        "message": f"User role updated to {new_role}",
        "user_id": "2002",
        "new_role": new_role,
    }
    assert target.role == new_role
    [row] = audit_rows(session)
    assert row.action == "system_role_update"
    assert row.user_id == "1001"
    assert row.user_email == "admin@example.com"
    assert row.resource == "user:2002"
    assert row.old_value_hash == old_role
    assert row.new_value_hash == new_role
    assert row.user_metadata == {
        "actor_user_id": "1001",
        "target_user_id": "2002",
        "old_role": old_role,
        "new_role": new_role,
    }
    session.commit.assert_awaited_once()


def test_demoting_a_non_initial_admin_while_another_admin_exists_keeps_its_audit_row(
    serve, session_admin
):
    new_role = "user"
    target = account("2002", "admin")
    session = role_change_session(account("1001", "admin", initial=True), target)
    response = serve(session).put("/api/v1/admin/users/2002/role", json={"role": new_role})
    assert response.status_code == 200, response.text
    assert target.role == new_role
    [row] = audit_rows(session)
    assert row.action == "system_role_update"
    assert row.user_metadata == {
        "actor_user_id": "1001",
        "target_user_id": "2002",
        "old_role": "admin",
        "new_role": new_role,
    }
    session.commit.assert_awaited_once()


def test_self_demotion_is_refused_before_any_statement(serve, session_admin):
    session = role_change_session(account("1001", "admin"), account("2002", "admin"))
    response = serve(session).put("/api/v1/admin/users/1001/role", json={"role": "user"})
    assert response.status_code == 403
    assert response.json()["message"] == "Cannot change your own admin role"
    assert session.statements == []


@pytest.mark.parametrize("role", ["read_only", "owner", "Admin", "", None, 1])
def test_an_invalid_role_is_422_before_any_statement(serve, session_admin, role):
    # #2854: ``admin`` and ``user`` are the only system roles the
    # ``valid_role`` CHECK of ``users`` allows.
    target = account("2002", "user")
    session = role_change_session(target)
    response = serve(session).put("/api/v1/admin/users/2002/role", json={"role": role})
    assert response.status_code == 422, response.text
    assert session.statements == []
    assert target.role == "user"
    session.add.assert_not_called()
    session.commit.assert_not_awaited()


def test_put_for_an_unknown_user_is_404_and_writes_nothing(serve, session_admin):
    session = role_change_session(account("1001", "admin"))
    response = serve(session).put("/api/v1/admin/users/2002/role", json={"role": "user"})
    assert response.status_code == 404, response.text
    assert response.json()["message"] == "User not found"
    session.add.assert_not_called()
    session.commit.assert_not_awaited()


# --- the shared check --------------------------------------------------------


def test_both_routes_go_through_the_shared_check(serve, session_admin, monkeypatch):
    calls: list[tuple[str, str]] = []

    async def _refuse(self, user_id: str, new_role: str):
        calls.append((user_id, new_role))
        raise AdminProtectionError("refused by the shared check", reason="last_admin")

    monkeypatch.setattr(SystemAdminService, "lock_for_role_change", _refuse)
    session = role_change_session(account("2002", "admin"), account("3003", "admin"))
    client = serve(session)

    put = client.put("/api/v1/admin/users/2002/role", json={"role": "user"})
    demote = client.delete("/api/v1/admin/system-admins/2002")

    assert calls == [("2002", "user"), ("2002", "user")]
    assert put.status_code == demote.status_code == 403
    assert put.json()["message"] == demote.json()["message"] == "refused by the shared check"
    # Neither route read or wrote a row on its own.
    assert session.statements == []
    session.add.assert_not_called()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("new_role", ["admin", "user"])
async def test_the_lock_is_the_first_statement_of_every_role_change(new_role):
    session = role_change_session(account("1001", "admin"), account("2002", "user"))
    target = await SystemAdminService(session).lock_for_role_change("2002", new_role)
    assert target is session.accounts["2002"]
    assert session.statements[0]._for_update_arg is not None
    session.add.assert_not_called()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_lock_statement_orders_by_user_id_and_takes_for_no_key_update():
    session = role_change_session(account("1001", "admin"), account("2002", "admin"))
    await SystemAdminService(session).lock_for_role_change("2002", "user")
    lock = session.statements[0]
    sql = " ".join(_sql(lock).split())
    # The admin rows AND the target, in one statement.
    assert "WHERE users.role = 'admin' OR users.user_id = '2002'" in sql
    assert sql.endswith("ORDER BY users.user_id FOR NO KEY UPDATE")
    # A row the session already holds is refreshed from the locked read.
    assert lock.get_execution_options().get("populate_existing") is True


@pytest.mark.asyncio
async def test_the_admin_count_is_its_own_statement_after_the_lock():
    session = role_change_session(account("1001", "admin"), account("2002", "admin"))
    await SystemAdminService(session).lock_for_role_change("2002", "user")
    assert len(session.statements) == 2
    lock, count = (" ".join(_sql(statement).split()) for statement in session.statements)
    assert "FOR NO KEY UPDATE" in lock
    assert count.startswith("SELECT count(*)")
    assert "WHERE users.role = 'admin'" in count
    assert "FOR " not in count


@pytest.mark.asyncio
async def test_the_shared_check_reads_no_system_admin_mode(monkeypatch):
    # Both rules are about role = 'admin' rows, whatever the mode grants.
    for mode in ("session", "service", "off"):
        monkeypatch.setattr(get_settings(), "system_admin_mode", mode)
        session = role_change_session(account("2002", "admin"))
        with pytest.raises(AdminProtectionError) as refused:
            await SystemAdminService(session).lock_for_role_change("2002", "user")
        assert refused.value.reason == "last_admin"
        assert refused.value.message == LAST_ADMIN_MESSAGE


@pytest.mark.asyncio
async def test_the_shared_check_returns_none_for_an_unknown_user():
    session = role_change_session(account("1001", "admin"))
    assert await SystemAdminService(session).lock_for_role_change("2002", "user") is None


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize(
    ("initial", "reason", "admins"),
    [(True, "initial_admin_protected", 2), (False, "last_admin_protected", 1)],
)
def test_the_refusal_log_line_names_the_account_by_id_and_holds_no_address(
    serve, operator, route, initial, reason, admins
):
    rows = [account("2002", "admin", initial=initial)]
    if initial:
        rows.append(account("1001", "admin"))
    with structlog.testing.capture_logs() as events:
        response = _take_admin_role(serve(role_change_session(*rows)), route, "2002", operator)
    assert response.status_code == 403
    [line] = [event for event in events if event["event"] == "demote_system_admin_blocked"]
    assert line == {
        "event": "demote_system_admin_blocked",
        "log_level": "warning",
        "user_id": "2002",
        "reason": reason,
        "admin_count": admins,
    }
    # The target's address (``2002@example.com``) is in no field of the line.
    assert "@" not in json.dumps(line, default=str)


# --- the demote route --------------------------------------------------------


def test_demote_writes_the_role_and_its_audit_row_in_one_commit(serve, session_admin):
    target = account("2002", "admin")
    session = role_change_session(account("1001", "admin", initial=True), target)
    order: list[str] = []
    session.add.side_effect = lambda row: order.append("add")

    async def _commit():
        order.append("commit")

    session.commit.side_effect = _commit
    response = serve(session).delete("/api/v1/admin/system-admins/2002")
    assert response.status_code == 200, response.text
    assert target.role == "user"
    assert order == ["add", "commit"]
    [row] = audit_rows(session)
    assert row.action == "system_admin_demote"
    assert row.user_id == "2002"
    assert row.user_email == "admin@example.com"
    assert row.resource == "user:2002@example.com"
    assert row.old_value_hash == "admin"
    assert row.new_value_hash == "user"
    assert row.user_metadata == {"demoted_by": "admin@example.com", "was_initial_admin": False}


def test_demote_keeps_its_answers_for_an_unknown_user_and_a_non_admin(serve, session_admin):
    session = role_change_session(account("1001", "admin"), account("2002", "user"))
    client = serve(session)
    missing = client.delete("/api/v1/admin/system-admins/4004")
    assert missing.status_code == 404
    assert missing.json()["error"] == "RES-001"
    not_admin = client.delete("/api/v1/admin/system-admins/2002")
    assert not_admin.status_code == 400
    assert not_admin.json()["error"] == "REQ-102"
    session.add.assert_not_called()
    session.commit.assert_not_awaited()
