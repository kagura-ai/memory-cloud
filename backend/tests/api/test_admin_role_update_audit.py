"""PUT /api/v1/admin/users/{user_id}/role writes an audit row with its actor (#2767, R7).

In ``service`` mode the actor strings are ``operator:<address>``; a session
admin keeps its own id and address.
"""

from __future__ import annotations

import secrets
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from api.main import app
from auth.dependencies import get_current_user_optional
from config.settings import get_settings
from db.base import get_db
from models.auth import AuditLog


@pytest.fixture
def db():
    target = SimpleNamespace(user_id="2002", role="user")
    result = MagicMock()
    result.scalar_one_or_none.return_value = target
    session = MagicMock()
    session.execute = AsyncMock(return_value=result)
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.target = target
    return session


@pytest.fixture
def client(db):
    async def _db():
        yield db

    app.dependency_overrides[get_db] = _db
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


def _audit_rows(db) -> list[AuditLog]:
    return [call.args[0] for call in db.add.call_args_list if isinstance(call.args[0], AuditLog)]


def test_session_admin_role_change_is_audited(client, db, monkeypatch):
    monkeypatch.setattr(get_settings(), "system_admin_mode", "session")

    async def _admin():
        return {"user_id": "1001", "email": "admin@example.com", "role": "admin"}

    app.dependency_overrides[get_current_user_optional] = _admin
    response = client.put("/api/v1/admin/users/2002/role", json={"role": "read_only"})
    assert response.status_code == 200, response.text
    assert db.target.role == "read_only"
    [row] = _audit_rows(db)
    assert row.action == "system_role_update"
    assert row.user_email == "admin@example.com"
    assert row.user_id == "1001"
    assert row.resource == "user:2002"
    assert row.user_metadata == {
        "actor_user_id": "1001",
        "target_user_id": "2002",
        "old_role": "user",
        "new_role": "read_only",
    }
    db.commit.assert_awaited_once()


def test_operator_role_change_is_audited_as_the_operator(client, db, monkeypatch):
    token = secrets.token_hex(32)
    settings = get_settings()
    monkeypatch.setattr(settings, "system_admin_mode", "service")
    monkeypatch.setattr(settings, "system_admin_service_token", token)
    monkeypatch.setattr(settings, "system_admin_service_token_previous", "")
    monkeypatch.setattr(settings, "system_admin_service_allowed_cidrs", "")
    response = client.put(
        "/api/v1/admin/users/2002/role",
        json={"role": "admin"},
        headers={"Authorization": f"Bearer {token}", "X-Operator-Email": "Ops@Example.com"},
    )
    assert response.status_code == 200, response.text
    [row] = _audit_rows(db)
    assert row.user_email == "operator:ops@example.com"
    assert row.user_id == "operator:ops@example.com"
    assert row.user_metadata["actor_user_id"] == "operator:ops@example.com"
    assert row.user_metadata["new_role"] == "admin"
