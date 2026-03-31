"""RBAC tests for Issue #59: Viewer restriction and resource token owner-only.

Tests:
- Viewer role is rejected from all external keys endpoints (403)
- Member/admin/owner roles can access external keys endpoints
- Non-owner is rejected from resource token list (403)
- Owner can access resource token list

Uses dependency_overrides to mock auth — no DB or Docker required.
"""

from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from api.main import app
from auth.dependencies import (
    get_user_from_api_key_or_session,
    require_workspace_member,
    require_workspace_owner,
)

WORKSPACE_ID = uuid4()


def _mock_user(workspace_role: str = "member") -> dict:
    """Create a mock user dict with a given workspace role."""
    return {
        "user_id": f"test_{workspace_role}",
        "email": f"{workspace_role}@test.com",
        "role": "user",
        "current_workspace_id": WORKSPACE_ID,
        "workspace_role": workspace_role,
    }


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def viewer_client():
    """Client authenticated as a viewer — WorkspaceMember dependency rejects."""

    async def mock_reject_viewer():
        raise HTTPException(status_code=403, detail="Requires 'member' role or higher")

    app.dependency_overrides[require_workspace_member] = mock_reject_viewer
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture
def member_client():
    """Client authenticated as a member — WorkspaceMember passes."""
    user = _mock_user("member")

    async def mock_member():
        return user

    app.dependency_overrides[require_workspace_member] = mock_member
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture
def non_owner_client():
    """Client authenticated as a member — WorkspaceOwner rejects."""
    user = _mock_user("member")

    async def mock_auth():
        return user

    async def mock_reject_owner():
        raise HTTPException(status_code=403, detail="Requires 'owner' role or higher")

    app.dependency_overrides[get_user_from_api_key_or_session] = mock_auth
    app.dependency_overrides[require_workspace_owner] = mock_reject_owner
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture
def owner_client():
    """Client authenticated as workspace owner — WorkspaceOwner passes."""
    user = _mock_user("owner")

    async def mock_auth():
        return user

    async def mock_owner():
        return (user["user_id"], WORKSPACE_ID)

    app.dependency_overrides[get_user_from_api_key_or_session] = mock_auth
    app.dependency_overrides[require_workspace_owner] = mock_owner
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client
    app.dependency_overrides.clear()


# ============================================================================
# External Keys — Viewer Rejection (Issue #59)
# ============================================================================


EXTERNAL_KEYS_ENDPOINTS = [
    ("GET", "/api/v1/external-keys"),
    ("POST", "/api/v1/external-keys"),
    ("PUT", "/api/v1/external-keys/test_key"),
    ("PATCH", "/api/v1/external-keys/test_key/toggle"),
    ("DELETE", "/api/v1/external-keys/test_key"),
    ("POST", "/api/v1/external-keys/import"),
]


class TestExternalKeysViewerRejection:
    """Viewer role should be rejected from all external keys endpoints."""

    @pytest.mark.parametrize("method,path", EXTERNAL_KEYS_ENDPOINTS)
    def test_viewer_gets_403(self, viewer_client, method, path):
        """Viewer should get 403 on all external keys endpoints."""
        json_body = None
        if method == "POST" and path == "/api/v1/external-keys":
            json_body = {
                "key_name": "test",
                "provider": "openai",
                "value": "sk-test",
            }
        elif method == "PUT":
            json_body = {"value": "sk-new"}
        elif method == "PATCH":
            json_body = {"enabled": True}

        response = viewer_client.request(method, path, json=json_body)
        assert response.status_code == 403, (
            f"{method} {path} returned {response.status_code}, expected 403"
        )


class TestExternalKeysMemberAccess:
    """Member role should be able to access external keys endpoints."""

    def test_member_can_list_external_keys(self, member_client):
        """Member should not get 403 on list endpoint.

        May get 500 (no DB) but NOT 403.
        """
        response = member_client.get("/api/v1/external-keys")
        assert response.status_code != 403, f"Member got 403 on list — RBAC too restrictive"


# ============================================================================
# Resource Tokens — Owner-Only List (Issue #59)
# ============================================================================


class TestResourceTokenListOwnerOnly:
    """Resource token list should require workspace owner."""

    def test_non_owner_gets_403(self, non_owner_client):
        """Non-owner should get 403 on resource token list."""
        response = non_owner_client.get("/api/v1/resource-tokens")
        assert response.status_code == 403, f"Non-owner got {response.status_code}, expected 403"

    def test_owner_can_list(self, owner_client):
        """Owner should not get 403 on resource token list.

        May get 500 (no DB) but NOT 403.
        """
        response = owner_client.get("/api/v1/resource-tokens")
        assert response.status_code != 403, f"Owner got 403 on list — RBAC too restrictive"
