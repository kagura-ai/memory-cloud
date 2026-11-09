"""Route-level integration tests for programmatic member/invitation access (#1164).

Proves the auth contract is WIRED into the actual endpoints (the discrimination
logic itself is unit-tested in tests/auth/test_programmatic_workspace_auth.py):

- OAuth Bearer principal → 403 on every gated endpoint (rejected before any DB
  lookup, so no permission mocking needed).
- API-key owner → success; the permission lookup is mocked so no DB is needed.
- API-key non-owner → 403.
- Workspace-scoped key bound to a foreign workspace → uniform 404.
- Programmatic invitation list omits token / invitation_url.
- Programmatic add_member with role=owner → 422.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from api.main import app
from auth.dependencies import get_user_from_api_key_or_session
from utils.exceptions import AuthorizationError

_WS = uuid.uuid4()


def _api_key_user(*, workspace_id=None, user_id="owner-key") -> dict:
    return {
        "user_id": user_id,
        "email": f"{user_id}@api",
        "role": "user",
        "current_workspace_id": workspace_id or _WS,
        "api_key_workspace_id": workspace_id,
    }


def _oauth_user() -> dict:
    return {
        "user_id": "oauth-user",
        "email": "o@oauth",
        "role": "user",
        "current_workspace_id": _WS,
        "oauth_scope": "memory:read memory:write",
    }


@pytest.fixture
def client():
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


def _override_user(user: dict) -> None:
    async def _get_user():
        return user

    app.dependency_overrides[get_user_from_api_key_or_session] = _get_user


@pytest.fixture
def perm(monkeypatch):
    """Mock PermissionService inside the helper module (owner check passes)."""
    from auth import programmatic_workspace_auth as mod

    inst = AsyncMock()
    inst.check_workspace_owner.return_value = MagicMock(role="owner")
    monkeypatch.setattr(mod, "PermissionService", lambda db: inst)
    return inst


class TestOAuthRejected:
    """OAuth bearer tokens are 403 on the member/invitation surface (#1164)."""

    def test_list_members_oauth_403(self, client):
        _override_user(_oauth_user())
        r = client.get(f"/api/v1/workspaces/{_WS}/members")
        assert r.status_code == 403
        _assert_oauth_refusal(r)

    def test_create_invitation_oauth_403(self, client):
        _override_user(_oauth_user())
        r = client.post(
            f"/api/v1/workspaces/{_WS}/invitations",
            json={"email": "x@example.com", "role": "member"},
        )
        assert r.status_code == 403
        _assert_oauth_refusal(r)


def _assert_oauth_refusal(resp) -> None:
    # #1693: both SDKs drop any server message containing "bearer" (it could
    # echo a credential), so the refusal says "access tokens" to reach users.
    assert resp.json()["message"] == (
        "OAuth access tokens cannot manage workspace members or credentials. "
        "Use a workspace-owner API key."
    )
    assert "bearer" not in resp.text.lower()


class TestApiKeyConfinement:
    def test_scoped_key_foreign_workspace_404(self, client, perm):
        # Key bound to another workspace hitting this path → uniform 404,
        # never reaches the owner lookup.
        _override_user(_api_key_user(workspace_id=uuid.uuid4()))
        r = client.get(f"/api/v1/workspaces/{_WS}/members")
        assert r.status_code == 404
        perm.check_workspace_owner.assert_not_called()

    def test_api_key_non_owner_403(self, client, perm):
        perm.check_workspace_owner.side_effect = AuthorizationError("not owner")
        _override_user(_api_key_user(workspace_id=None))
        r = client.get(f"/api/v1/workspaces/{_WS}/members")
        assert r.status_code == 403


class TestProgrammaticInvitationList:
    def test_list_omits_token_for_api_key(self, client, perm, monkeypatch):
        _override_user(_api_key_user(workspace_id=None))

        inv = MagicMock()
        inv.id = 1
        inv.public_id = "winv_" + "1" * 22
        inv.workspace_id = _WS
        inv.token = "secret-token"
        inv.email = "x@example.com"
        inv.role = "member"
        inv.invited_by = "owner-key"
        inv.expires_at = None
        inv.accepted_at = None
        inv.accepted_by = None
        inv.created_at = __import__("datetime").datetime(2026, 1, 1)
        inv.is_expired.return_value = False
        inv.is_accepted.return_value = False
        inv.allowed_context_ids = None

        from api.routes import invitations as inv_mod

        svc = MagicMock()
        svc.list_invitations = AsyncMock(return_value=[inv])
        monkeypatch.setattr(inv_mod, "InvitationService", lambda db: svc)

        r = client.get(f"/api/v1/workspaces/{_WS}/invitations")
        assert r.status_code == 200
        body = r.json()[0]
        # Bearer join-credentials are omitted for programmatic principals.
        assert body["token"] is None
        assert body["invitation_url"] is None


class TestProgrammaticOwnerRoleRejected:
    def test_add_member_role_owner_422(self, client, perm):
        _override_user(_api_key_user(workspace_id=None))
        r = client.post(
            f"/api/v1/workspaces/{_WS}/members",
            json={"user_id": "target", "role": "owner"},
        )
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# #2326: the context access list at add time, and the owner key on the PUT
# ---------------------------------------------------------------------------

_CTX = uuid.uuid4()
MEMBERS_URL = f"/api/v1/workspaces/{_WS}/members"
ACCESS_URL = f"/api/v1/workspaces/{_WS}/members/target/context-access"


def _session_user(user_id: str = "admin-1") -> dict:
    return {"user_id": user_id, "sub": user_id, "email": f"{user_id}@test", "role": "user"}


@pytest.fixture
def fake_db():
    """A DB session that knows exactly one live context of _WS (``_CTX``).

    ``events`` records the audit row being added and the service write, in
    order, so a test can prove the audit row is in the session before the
    service commits.
    """
    from db.base import get_db

    result = MagicMock()
    result.all.return_value = [(_CTX,)]
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    db.events = []
    db.add.side_effect = lambda row: db.events.append(("add", type(row).__name__))

    async def _get_db():
        yield db

    app.dependency_overrides[get_db] = _get_db
    return db


@pytest.fixture
def ws_service(monkeypatch, fake_db):
    """WorkspaceService stub: add_member / update_member_context_access echo their input."""
    from api.routes import workspaces as mod

    svc = MagicMock()

    async def _add(*, workspace_id, user_id, role, invited_by, allowed_context_ids=None):
        fake_db.events.append(("service", "add_member"))
        return MagicMock(
            user_id=user_id, role=role, joined_at=None, allowed_context_ids=allowed_context_ids
        )

    async def _update(*, workspace_id, user_id, allowed_context_ids, actor_user_id):
        fake_db.events.append(("service", "update_member_context_access"))
        return MagicMock(user_id=user_id, allowed_context_ids=allowed_context_ids)

    svc.add_member = AsyncMock(side_effect=_add)
    svc.update_member_context_access = AsyncMock(side_effect=_update)
    monkeypatch.setattr(mod, "WorkspaceService", lambda db: svc)
    return svc


def _audit_rows(db) -> list:
    from models.auth import AuditLog

    return [c.args[0] for c in db.add.call_args_list if isinstance(c.args[0], AuditLog)]


class TestAddMemberContextAccessProgrammatic:
    def test_owner_key_with_a_list_is_201_and_audited_with_the_count(
        self, client, perm, fake_db, ws_service
    ):
        _override_user(_api_key_user(workspace_id=None))
        r = client.post(
            MEMBERS_URL,
            json={"user_id": "target", "role": "member", "allowed_context_ids": [str(_CTX)]},
        )
        assert r.status_code == 201, r.text
        assert r.json()["allowed_context_ids"] == [str(_CTX)]
        assert ws_service.add_member.await_args.kwargs["allowed_context_ids"] == [_CTX]
        rows = _audit_rows(fake_db)
        assert len(rows) == 1
        assert rows[0].action == "workspace_member_added"
        assert rows[0].user_metadata["target"] == "target"
        assert rows[0].user_metadata["role"] == "member"
        assert rows[0].user_metadata["context_count"] == 1
        assert fake_db.events.index(("add", "AuditLog")) < fake_db.events.index(
            ("service", "add_member")
        )

    @pytest.mark.parametrize(
        ("payload", "count"),
        [
            ({}, "not_set"),
            ({"allowed_context_ids": None}, "not_set"),
            ({"allowed_context_ids": []}, 0),
        ],
        ids=["absent", "null", "empty"],
    )
    def test_owner_key_audit_counts_not_set_and_empty(
        self, client, perm, fake_db, ws_service, payload, count
    ):
        _override_user(_api_key_user(workspace_id=None))
        r = client.post(MEMBERS_URL, json={"user_id": "target", "role": "member", **payload})
        assert r.status_code == 201, r.text
        assert _audit_rows(fake_db)[0].user_metadata["context_count"] == count

    def test_oauth_with_a_list_is_403(self, client, fake_db, ws_service):
        _override_user(_oauth_user())
        r = client.post(
            MEMBERS_URL,
            json={"user_id": "target", "role": "member", "allowed_context_ids": [str(_CTX)]},
        )
        assert r.status_code == 403
        _assert_oauth_refusal(r)
        ws_service.add_member.assert_not_called()


class TestMemberContextAccessProgrammatic:
    """PUT …/context-access goes through authorize_workspace_management (#2326)."""

    def test_owner_key_is_200_and_audited(self, client, perm, fake_db, ws_service):
        _override_user(_api_key_user(workspace_id=None))
        r = client.put(ACCESS_URL, json={"allowed_context_ids": [str(_CTX)]})
        assert r.status_code == 200, r.text
        assert r.json() == {
            "status": "ok",
            "user_id": "target",
            "allowed_context_ids": [str(_CTX)],
        }
        perm.check_workspace_owner.assert_awaited_once_with("owner-key", _WS)
        rows = _audit_rows(fake_db)
        assert len(rows) == 1
        assert rows[0].action == "workspace_member_context_access_set"
        assert rows[0].resource == f"workspace:{_WS}"
        assert rows[0].user_metadata["target"] == "target"
        assert rows[0].user_metadata["via"] == "api_key"
        assert rows[0].user_metadata["context_count"] == 1
        # Added before the service call, which commits it with the change.
        assert fake_db.events.index(("add", "AuditLog")) < fake_db.events.index(
            ("service", "update_member_context_access")
        )

    def test_owner_key_audit_says_not_set_for_null(self, client, perm, fake_db, ws_service):
        _override_user(_api_key_user(workspace_id=None))
        r = client.put(ACCESS_URL, json={"allowed_context_ids": None})
        assert r.status_code == 200, r.text
        assert _audit_rows(fake_db)[0].user_metadata["context_count"] == "not_set"

    def test_admin_key_is_403(self, client, perm, fake_db, ws_service):
        # An API key is owner-gated whatever role its user holds.
        perm.check_workspace_owner.side_effect = AuthorizationError("Insufficient permissions")
        _override_user(_api_key_user(workspace_id=None, user_id="admin-key"))
        r = client.put(ACCESS_URL, json={"allowed_context_ids": []})
        assert r.status_code == 403
        ws_service.update_member_context_access.assert_not_called()
        assert _audit_rows(fake_db) == []

    def test_key_bound_to_another_workspace_is_404(self, client, perm, fake_db, ws_service):
        _override_user(_api_key_user(workspace_id=uuid.uuid4()))
        r = client.put(ACCESS_URL, json={"allowed_context_ids": []})
        assert r.status_code == 404
        perm.check_workspace_owner.assert_not_called()
        ws_service.update_member_context_access.assert_not_called()

    def test_oauth_is_403(self, client, perm, fake_db, ws_service):
        _override_user(_oauth_user())
        r = client.put(ACCESS_URL, json={"allowed_context_ids": []})
        assert r.status_code == 403
        _assert_oauth_refusal(r)
        ws_service.update_member_context_access.assert_not_called()

    def test_kill_switch_off_is_403(self, client, perm, fake_db, ws_service, monkeypatch):
        from config.settings import get_settings

        monkeypatch.setattr(get_settings(), "enable_owner_key_member_management", False)
        _override_user(_api_key_user(workspace_id=None))
        r = client.put(ACCESS_URL, json={"allowed_context_ids": []})
        assert r.status_code == 403
        assert "disabled on this deployment" in r.json()["message"]
        perm.check_workspace_owner.assert_not_called()
        ws_service.update_member_context_access.assert_not_called()

    def test_session_admin_is_200_and_not_audited(self, client, perm, fake_db, ws_service):
        from auth.workspace_roles import WorkspaceRole

        perm.check_workspace_access.return_value = MagicMock(role="admin")
        _override_user(_session_user())
        r = client.put(ACCESS_URL, json={"allowed_context_ids": [str(_CTX)]})
        assert r.status_code == 200, r.text
        perm.check_workspace_access.assert_awaited_once_with(
            "admin-1", _WS, required_role=WorkspaceRole.ADMIN
        )
        assert _audit_rows(fake_db) == []

    def test_session_member_is_403(self, client, perm, fake_db, ws_service):
        perm.check_workspace_access.side_effect = AuthorizationError("Insufficient permissions")
        _override_user(_session_user("member-1"))
        r = client.put(ACCESS_URL, json={"allowed_context_ids": []})
        assert r.status_code == 403
        ws_service.update_member_context_access.assert_not_called()
