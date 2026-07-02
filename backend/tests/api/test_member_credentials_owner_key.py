"""Route-level tests for owner-key member-credential provisioning (#1165).

Covers the programmatic (API-key owner) mint / list / revoke paths and their
guardrails. Session self-mint/self-delete semantics are unchanged and covered
by the existing member-credentials tests; here we prove the NEW programmatic
behavior is wired and gated.

PermissionService (inside the shared auth helper) and MemberCredentialsService /
APIKeyManager are mocked so no DB is required.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from api.main import app
from auth.dependencies import get_user_from_api_key_or_session
from db.base import get_db

_WS = uuid.uuid4()


def _api_key_owner(*, workspace_id=None, user_id="owner-key") -> dict:
    return {
        "user_id": user_id,
        "email": f"{user_id}@api",
        "role": "user",
        "current_workspace_id": workspace_id or _WS,
        "api_key_workspace_id": workspace_id,
    }


def _oauth() -> dict:
    return {"user_id": "o", "email": "o@oauth", "role": "user", "oauth_scope": "memory:write"}


@pytest.fixture
def client():
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


def _override(user: dict) -> None:
    async def _get():
        return user

    app.dependency_overrides[get_user_from_api_key_or_session] = _get

    # Stub the DB session — services/manager/permission are all mocked, so the
    # route never needs a real session. db.commit must be awaitable.
    fake_db = MagicMock()
    fake_db.commit = AsyncMock()
    fake_db.execute = AsyncMock()

    async def _get_db():
        yield fake_db

    app.dependency_overrides[get_db] = _get_db


@pytest.fixture
def owner_gate(monkeypatch):
    """Owner check inside the helper passes (mocked); no DB."""
    from auth import programmatic_workspace_auth as mod

    inst = AsyncMock()
    inst.check_workspace_owner.return_value = MagicMock(role="owner")
    monkeypatch.setattr(mod, "PermissionService", lambda db: inst)
    return inst


def _mock_member_service(monkeypatch, *, target_role="member"):
    from api.routes import member_credentials as mc

    svc = MagicMock()
    svc.get_workspace_role = AsyncMock(return_value=target_role)
    monkeypatch.setattr(mc, "MemberCredentialsService", lambda db: svc)
    return svc


def _mock_manager(monkeypatch):
    from api.routes import member_credentials as mc

    new_key = MagicMock()
    new_key.id = 42
    new_key.name = "ci-key"
    new_key.key_prefix = "kagura_abc"
    new_key.created_at = __import__("datetime").datetime(2026, 1, 1)
    mgr = MagicMock()
    mgr.create_key = AsyncMock(return_value=("kagura_PLAINTEXT", new_key))
    monkeypatch.setattr(mc, "APIKeyManager", lambda db: mgr)
    return mgr, new_key


MINT_URL = f"/api/v1/workspaces/{_WS}/members/target-user/credentials/api-keys"


class TestOwnerProvisionedMint:
    def test_oauth_rejected(self, client):
        _override(_oauth())
        r = client.post(MINT_URL, json={"name": "k", "expires_days": 30})
        assert r.status_code == 403

    def test_success_for_member_target(self, client, owner_gate, monkeypatch):
        _override(_api_key_owner())
        _mock_member_service(monkeypatch, target_role="member")
        mgr, _ = _mock_manager(monkeypatch)
        # commit / db.add are on the request-scoped session; patch get_db's session
        # via the manager/audit no-op — the route calls db.add + db.commit which the
        # real (test) session handles. Use a stub session through dependency_overrides.
        r = client.post(MINT_URL, json={"name": "ci-key", "expires_days": 30})
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["plaintext_key"] == "kagura_PLAINTEXT"
        assert body["is_visible"] is False
        mgr.create_key.assert_awaited_once()
        # expires_days plumbed; force-hidden via auto_hide_minutes=0
        kwargs = mgr.create_key.await_args.kwargs
        assert kwargs["expires_days"] == 30
        assert kwargs["auto_hide_minutes"] == 0

    def test_self_mint_forbidden(self, client, owner_gate, monkeypatch):
        # target == caller → anti self-replication 403
        _override(_api_key_owner(user_id="target-user"))
        _mock_member_service(monkeypatch, target_role="member")
        r = client.post(MINT_URL, json={"name": "k", "expires_days": 30})
        assert r.status_code == 403

    def test_owner_target_forbidden(self, client, owner_gate, monkeypatch):
        _override(_api_key_owner())
        _mock_member_service(monkeypatch, target_role="owner")
        r = client.post(MINT_URL, json={"name": "k", "expires_days": 30})
        assert r.status_code == 403

    def test_missing_expires_days_400(self, client, owner_gate, monkeypatch):
        _override(_api_key_owner())
        _mock_member_service(monkeypatch, target_role="member")
        r = client.post(MINT_URL, json={"name": "k"})
        assert r.status_code == 400

    def test_bound_context_id_400(self, client, owner_gate, monkeypatch):
        _override(_api_key_owner())
        _mock_member_service(monkeypatch, target_role="member")
        r = client.post(
            MINT_URL,
            json={"name": "k", "expires_days": 30, "bound_context_id": str(uuid.uuid4())},
        )
        assert r.status_code == 400

    def test_scoped_key_foreign_workspace_404(self, client, owner_gate, monkeypatch):
        _override(_api_key_owner(workspace_id=uuid.uuid4()))
        _mock_member_service(monkeypatch, target_role="member")
        r = client.post(MINT_URL, json={"name": "k", "expires_days": 30})
        assert r.status_code == 404
