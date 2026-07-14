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
    new_key.expires_at = __import__("datetime").datetime(2026, 1, 31)  # #1165: owner-set expiry
    new_key.bound_context_id = None  # owner-provisioned keys never bind a context
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
        assert audit_rows[0].resource == "api_key:42"
        meta = audit_rows[0].user_metadata
        assert meta["target"] == "target-user"
        assert meta["via"] == "api_key"
        # #1171 cleanup: routed through the shared helper — the MINTED key prefix
        # lands under the distinct ``minted_key_prefix`` key (the helper reserves
        # ``key_prefix`` for the ACTING owner key).
        assert meta["minted_key_prefix"] == "kagura_abc"
        assert meta["expires_days"] == 30

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

    def test_admin_target_403_names_role_without_enum_repr(self, client, owner_gate, monkeypatch):
        # Regression for #1180: the REAL service returns WorkspaceRole enum
        # members, but the string mocks above masked the ``{target_role!r}``
        # repr leak — pass the actual enum and pin the client-facing message.
        from auth.workspace_roles import WorkspaceRole

        _override(_api_key_owner())
        _mock_member_service(monkeypatch, target_role=WorkspaceRole.ADMIN)
        r = client.post(MINT_URL, json={"name": "k", "expires_days": 30})
        assert r.status_code == 403
        message = r.json()["message"]
        assert "role='admin'" in message
        assert "<WorkspaceRole" not in message

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
        # v0.42 review #9: zero-knowledge — soft revoke must drop the at-rest
        # plaintext (sweeper skips revoked rows) and cancel the visibility window.
        assert api_key.plaintext_encrypted is None
        assert api_key.visibility_expires_at is None
        assert api_key.hidden_at is not None
        # A programmatic revoke must leave a forensic AuditLog row (#1164/#1165).
        from models.auth import AuditLog

        audit_rows = [
            c.args[0] for c in fake_db.add.call_args_list if isinstance(c.args[0], AuditLog)
        ]
        assert len(audit_rows) == 1
        assert audit_rows[0].action == "member_api_key_revoked"
        assert audit_rows[0].resource == "api_key:42"
        meta = audit_rows[0].user_metadata
        assert meta["target"] == "target-user"
        assert meta["via"] == "api_key"
        # #1171 cleanup: routed through the shared helper — the REVOKED key prefix
        # lands under the distinct ``revoked_key_prefix`` key.
        assert meta["revoked_key_prefix"] == "kagura_abc"
        assert meta["self_revoke"] is False

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

    def test_already_revoked_row_is_404_not_reprocessed(self, client, owner_gate, monkeypatch):
        # v0.42 review #6: a soft-revoked (forensic) row must be untouchable — a
        # repeated programmatic revoke must not overwrite revoked_at or append a
        # duplicate audit row; uniform 404.
        _override(_api_key_owner())
        _mock_member_service(monkeypatch, target_role="member")

        import datetime as _dt

        original_revoked_at = _dt.datetime(2026, 1, 1)
        api_key = MagicMock()
        api_key.id = 42
        api_key.workspace_id = _WS
        api_key.bound_context_id = None
        api_key.revoked_at = original_revoked_at  # already revoked
        api_key.key_prefix = "kagura_abc"
        fake_db = self._fake_db_with_key(api_key)

        async def _get_db():
            yield fake_db

        app.dependency_overrides[get_db] = _get_db

        r = client.delete(REVOKE_URL)
        assert r.status_code == 404
        assert api_key.revoked_at == original_revoked_at  # not overwritten
        fake_db.delete.assert_not_called()
        from models.auth import AuditLog

        audit_rows = [
            c.args[0] for c in fake_db.add.call_args_list if isinstance(c.args[0], AuditLog)
        ]
        assert audit_rows == []  # no duplicate forensic row


class TestGetMemberCredentialsNonMember:
    def test_non_member_target_is_404_not_500(self, client, owner_gate, monkeypatch):
        # v0.42 review #36: get_workspace_role returns None for a removed/mistyped
        # target; the non-optional target_user_role field must not raise a
        # ValidationError swallowed into a 500 — return a clean 404.
        _override(_api_key_owner())
        from api.routes import member_credentials as mc

        svc = MagicMock()
        svc.get_or_create_credentials = AsyncMock(return_value={"api_keys": []})
        svc.get_workspace_role = AsyncMock(return_value=None)  # not a member
        monkeypatch.setattr(mc, "MemberCredentialsService", lambda db: svc)

        r = client.get(LIST_URL)
        assert r.status_code == 404, r.text


class TestAgentBoundMint:
    """RFC-0002 P0-2 (#1275): owner-provisioned mint accepts agent_id."""

    def test_agent_id_plumbed_and_audited(self, client, owner_gate, monkeypatch):
        fake_db = _override(_api_key_owner())
        _mock_member_service(monkeypatch, target_role="member")
        mgr, _ = _mock_manager(monkeypatch)
        agent_id = uuid.uuid4()

        r = client.post(
            MINT_URL,
            json={"name": "agent-key", "expires_days": 30, "agent_id": str(agent_id)},
        )

        assert r.status_code == 201, r.text
        assert mgr.create_key.await_args.kwargs["agent_id"] == agent_id
        from models.auth import AuditLog

        audit_rows = [
            c.args[0] for c in fake_db.add.call_args_list if isinstance(c.args[0], AuditLog)
        ]
        assert audit_rows[0].user_metadata["agent_id"] == str(agent_id)

    def test_mint_gate_valueerror_maps_to_400(self, client, owner_gate, monkeypatch):
        """create_key's mint gates (unknown agent / cross-workspace / non-active)
        raise ValueError, which the route maps to a clean 400."""
        _override(_api_key_owner())
        _mock_member_service(monkeypatch, target_role="member")
        mgr, _ = _mock_manager(monkeypatch)
        mgr.create_key = AsyncMock(side_effect=ValueError("agent and key workspace mismatch"))

        r = client.post(
            MINT_URL,
            json={"name": "k", "expires_days": 30, "agent_id": str(uuid.uuid4())},
        )

        assert r.status_code == 400

    def test_session_self_mint_rejects_agent_id(self, client, monkeypatch):
        """agent_id is owner-provisioned-only — session self-mint 400s
        (same posture as expires_days)."""
        _override(
            {
                "user_id": "target-user",
                "sub": "target-user",  # session principal marker
                "email": "t@example.com",
                "role": "user",
                "current_workspace_id": _WS,
            }
        )
        r = client.post(MINT_URL, json={"name": "k", "agent_id": str(uuid.uuid4())})
        assert r.status_code == 400
        assert "owner-provisioned" in r.json()["message"]
