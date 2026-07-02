"""Route-level tests for owner-key member-credential provisioning (#1165).

Covers the programmatic (API-key owner) mint, list, and revoke paths and
their guardrails. Session self-mint/self-delete semantics are unchanged and covered
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


def _override(user: dict) -> MagicMock:
    async def _get():
        return user

    app.dependency_overrides[get_user_from_api_key_or_session] = _get

    # Stub the DB session — services/manager/permission are all mocked, so the
    # route never needs a real session. db.commit must be awaitable. Returned so
    # callers can assert on db.add (e.g. the programmatic AuditLog row).
    fake_db = MagicMock()
    fake_db.commit = AsyncMock()
    fake_db.execute = AsyncMock()

    async def _get_db():
        yield fake_db

    app.dependency_overrides[get_db] = _get_db
    return fake_db


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
        fake_db = _override(_api_key_owner())
        _mock_member_service(monkeypatch, target_role="member")
        mgr, new_key = _mock_manager(monkeypatch)
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
        # Force-hide must null BOTH the visibility window AND the encrypted-at-rest
        # copy: the auto-hide sweeper skips already-hidden rows, so a force-hidden
        # minted key that kept plaintext_encrypted would stay Fernet-decryptable
        # forever (Migration-035 zero-knowledge). Regression guard for #1171.
        assert new_key.visibility_expires_at is None
        assert new_key.plaintext_encrypted is None
        # A programmatic mint must leave a forensic AuditLog row (#1164/#1165).
        from models.auth import AuditLog

        audit_rows = [
            c.args[0] for c in fake_db.add.call_args_list if isinstance(c.args[0], AuditLog)
        ]
        assert len(audit_rows) == 1
        assert audit_rows[0].action == "member_api_key_provisioned"
        assert audit_rows[0].user_metadata["target"] == "target-user"

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


LIST_URL = f"/api/v1/workspaces/{_WS}/members/target-user/credentials"
REVOKE_URL = f"/api/v1/workspaces/{_WS}/members/target-user/credentials/api-keys/42"


def _member_api_key_dict(plaintext="secret-plain"):
    return {
        "id": 42,
        "name": "k",
        "key_prefix": "kagura_abc",
        "plaintext_key": plaintext,
        "is_visible": True,
        "visibility_expires_at": None,
        "created_at": "2026-01-01T00:00:00Z",
        "last_used_at": None,
        "revoked_at": None,
        "bound_context_id": None,
    }


class TestOwnerProvisionedList:
    def test_programmatic_list_masks_plaintext(self, client, owner_gate, monkeypatch):
        _override(_api_key_owner())
        from api.routes import member_credentials as mc

        svc = MagicMock()
        svc.get_or_create_credentials = AsyncMock(
            return_value={"api_keys": [_member_api_key_dict("secret-plain")]}
        )
        svc.get_workspace_role = AsyncMock(return_value="member")
        monkeypatch.setattr(mc, "MemberCredentialsService", lambda db: svc)

        r = client.get(LIST_URL)
        assert r.status_code == 200, r.text
        # Programmatic principal never receives plaintext, even if the service
        # returned it.
        assert r.json()["api_keys"][0]["plaintext_key"] is None

    def test_oauth_list_rejected(self, client):
        _override(_oauth())
        r = client.get(LIST_URL)
        assert r.status_code == 403


class TestOwnerProvisionedRevoke:
    def _fake_db_with_key(self, api_key):
        fake_db = MagicMock()
        fake_db.commit = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = api_key
        fake_db.execute = AsyncMock(return_value=result)
        return fake_db

    def test_owner_soft_revoke(self, client, owner_gate, monkeypatch):
        _override(_api_key_owner())
        _mock_member_service(monkeypatch, target_role="member")

        api_key = MagicMock()
        api_key.id = 42
        api_key.workspace_id = _WS
        api_key.bound_context_id = None
        api_key.revoked_at = None
        api_key.key_prefix = "kagura_abc"
        fake_db = self._fake_db_with_key(api_key)

        async def _get_db():
            yield fake_db

        app.dependency_overrides[get_db] = _get_db

        r = client.delete(REVOKE_URL)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "revoked"
        # Soft revoke: revoked_at set, row NOT deleted.
        assert api_key.revoked_at is not None
        fake_db.delete.assert_not_called()
        # A programmatic revoke must leave a forensic AuditLog row (#1164/#1165).
        from models.auth import AuditLog

        audit_rows = [
            c.args[0] for c in fake_db.add.call_args_list if isinstance(c.args[0], AuditLog)
        ]
        assert len(audit_rows) == 1
        assert audit_rows[0].action == "member_api_key_revoked"

    def test_cross_member_global_key_revoke_404(self, client, owner_gate, monkeypatch):
        # #1171 max-review: a global key (workspace_id NULL, bound_context_id NULL)
        # is not anchored to any workspace. An owner of THIS workspace cannot
        # revoke a member's account-global key across the workspace boundary —
        # uniform 404, no state change, no misattributed audit row.
        _override(_api_key_owner())  # caller "owner-key" != target "target-user"
        _mock_member_service(monkeypatch, target_role="member")

        api_key = MagicMock()
        api_key.id = 42
        api_key.workspace_id = None
        api_key.bound_context_id = None
        api_key.revoked_at = None
        api_key.key_prefix = "kagura_global"
        fake_db = self._fake_db_with_key(api_key)

        async def _get_db():
            yield fake_db

        app.dependency_overrides[get_db] = _get_db

        r = client.delete(REVOKE_URL)
        assert r.status_code == 404
        assert api_key.revoked_at is None
        fake_db.delete.assert_not_called()

    def test_oauth_revoke_rejected(self, client):
        _override(_oauth())
        r = client.delete(REVOKE_URL)
        assert r.status_code == 403

    def test_programmatic_self_revoke_is_soft(self, client, owner_gate, monkeypatch):
        # #1165 / Copilot #1171: an API-key owner revoking their OWN key is a
        # soft revoke + audit too — never a silent hard delete.
        _override(_api_key_owner(user_id="owner-key"))
        _mock_member_service(monkeypatch, target_role="owner")  # self role irrelevant here

        api_key = MagicMock()
        api_key.id = 7
        api_key.workspace_id = _WS
        api_key.bound_context_id = None
        api_key.revoked_at = None
        api_key.key_prefix = "kagura_self"
        fake_db = self._fake_db_with_key(api_key)

        async def _get_db():
            yield fake_db

        app.dependency_overrides[get_db] = _get_db

        r = client.delete(f"/api/v1/workspaces/{_WS}/members/owner-key/credentials/api-keys/7")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "revoked"
        assert api_key.revoked_at is not None
        fake_db.delete.assert_not_called()
