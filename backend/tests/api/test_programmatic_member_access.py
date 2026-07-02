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

    def test_create_invitation_oauth_403(self, client):
        _override_user(_oauth_user())
        r = client.post(
            f"/api/v1/workspaces/{_WS}/invitations",
            json={"email": "x@example.com", "role": "member"},
        )
        assert r.status_code == 403


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
