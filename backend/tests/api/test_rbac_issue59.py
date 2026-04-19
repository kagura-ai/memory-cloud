"""RBAC tests for external keys (#381 owner-only) and resource tokens (#59 owner-only).

External keys (Issue #59 → tightened in #381):
- Issue #59 originally restricted viewers; Issue #381 tightened the contract to owner-only
  because external API keys are workspace-level secrets (OpenAI/Cohere/Anthropic credentials).
- ALL non-owner roles (viewer, member, admin) are now rejected with 403.
- Only the workspace owner can list/create/update/toggle/delete external keys.
- The legacy POST /external-keys/import endpoint was deleted in #381 (dead code).

Resource tokens (Issue #59 — unchanged):
- Non-owner is rejected from resource token list (403).
- Owner can access resource token list.

Uses dependency_overrides to mock auth — no DB or Docker required.
"""

from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from api.main import app
from auth.dependencies import (
    get_user_from_api_key_or_session,
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
def non_owner_client():
    """Client authenticated as a non-owner — WorkspaceOwner rejects.

    Used for both external keys (#381) and resource tokens (#59) — both require owner.
    The mocked user role doesn't matter here because the override directly raises 403;
    the test only verifies that the route's owner gate fires, not which non-owner role
    triggered it. Parametrize the underlying role at the test layer if that distinction
    becomes important.
    """
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
# External Keys — Owner-Only (Issue #381, tightened from #59)
# ============================================================================


EXTERNAL_KEYS_ENDPOINTS = [
    ("GET", "/api/v1/external-keys"),
    ("POST", "/api/v1/external-keys"),
    ("PUT", "/api/v1/external-keys/test_key"),
    ("PATCH", "/api/v1/external-keys/test_key/toggle"),
    ("DELETE", "/api/v1/external-keys/test_key"),
]


class TestExternalKeysOwnerOnly:
    """Issue #381: external keys are owner-only — non-owners (viewer/member/admin) get 403."""

    @pytest.mark.parametrize("method,path", EXTERNAL_KEYS_ENDPOINTS)
    def test_non_owner_gets_403(self, non_owner_client, method, path):
        """Any non-owner role should get 403 on every external keys endpoint."""
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

        response = non_owner_client.request(method, path, json=json_body)
        assert response.status_code == 403, (
            f"{method} {path} returned {response.status_code}, expected 403"
        )

    def test_import_endpoint_removed(self, owner_client):
        """Issue #381: POST /external-keys/import is removed; even owner gets 404/405."""
        response = owner_client.post("/api/v1/external-keys/import")
        assert response.status_code in (404, 405), (
            f"POST /external-keys/import returned {response.status_code}; "
            "endpoint should be removed (expected 404 Not Found or 405 Method Not Allowed)"
        )


class TestExternalKeysOwnerAccess:
    """Workspace owner should reach the handler body on every external keys endpoint."""

    def test_owner_can_list_external_keys(self, owner_client):
        """Owner should not get 403 on list endpoint.

        May get 500 (no DB) but NOT 403.
        """
        response = owner_client.get("/api/v1/external-keys")
        assert response.status_code != 403, "Owner got 403 on list — RBAC too restrictive"


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
        assert response.status_code != 403, "Owner got 403 on list — RBAC too restrictive"
