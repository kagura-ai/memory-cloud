"""RBAC tests for resources read endpoints (Issue #389).

Owner-only access on the four workspace-level read endpoints:

- ``GET /api/v1/resources``
- ``GET /api/v1/resources/{id}/schema``
- ``GET /api/v1/resources/{id}/impact``
- ``GET /api/v1/resources/{id}/indexer-status``

Same pattern as ``test_rbac_issue59.py`` — uses ``dependency_overrides`` to
short-circuit the ``WorkspaceOwner`` gate, so these tests run without a
real DB. The role gate is exercised in isolation; the cross-workspace 404
path (from ``resolve_resource_by_slug``) is covered by the real-DB
integration test in ``tests/integration/test_resource_cross_workspace.py``.

The mocked user role in ``non_owner_client`` is arbitrary (set to
``member``): ``require_workspace_owner`` is overridden to directly raise
403, so the specific non-owner role does not matter at the route layer
(identical rationale as ``test_rbac_issue59.py`` lines 53-56).
"""

from unittest.mock import patch
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
    """Client authenticated as a non-owner — ``WorkspaceOwner`` rejects at 403."""
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
    """Client authenticated as a workspace owner — ``WorkspaceOwner`` passes."""
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
# Tests
# ============================================================================


RESOURCES_OWNER_ENDPOINTS = [
    ("GET", "/api/v1/resources"),
    ("GET", "/api/v1/resources/test_slug/schema"),
    ("GET", "/api/v1/resources/test_slug/impact"),
    ("GET", "/api/v1/resources/test_slug/indexer-status"),
]


class TestResourcesOwnerOnly:
    """Issue #389: resources read endpoints are owner-only — non-owners get 403."""

    @pytest.mark.parametrize("method,path", RESOURCES_OWNER_ENDPOINTS)
    def test_non_owner_gets_403(self, non_owner_client, method, path):
        """Any non-owner role should get 403 on every resources read endpoint."""
        response = non_owner_client.request(method, path)
        assert response.status_code == 403, (
            f"{method} {path} returned {response.status_code}, expected 403"
        )


class TestResourcesOwnerHappyPath:
    """Issue #389: owner must reach the handler body (route-wiring smoke).

    The 403-only suite above would still pass if ``WorkspaceOwner`` were
    miswired to reject every caller. This smoke proves the dependency
    accepts an owner tuple and the handler executes. Covers ``GET
    /api/v1/resources`` specifically because the existing
    ``tests/api/test_resources.py`` suite xfails on missing async_client /
    authenticated_user fixtures — without this smoke the owner path has no
    non-xfail coverage for that endpoint. The three slug-path endpoints
    (schema / impact / indexer-status) receive owner-path coverage from
    ``tests/api/test_resource_indexer_api.py`` and the real-DB integration
    tests in ``tests/integration/test_resource_cross_workspace.py``; those
    reach handler bodies and prove the same route-wiring contract.
    """

    def test_owner_can_list_resources(self, owner_client):
        """``GET /api/v1/resources`` returns 200 for a workspace owner.

        ``get_accessible_contexts`` is mocked to return an empty list so
        the handler short-circuits to the empty-response path without
        needing a real DB. The assertion is on ``status_code == 200`` and
        the empty-body shape — route wiring is the contract under test.
        """

        async def mock_get_accessible_contexts(self, user_id, workspace_id):
            return []

        with patch(
            "services.permission_service.PermissionService.get_accessible_contexts",
            new=mock_get_accessible_contexts,
        ):
            response = owner_client.get("/api/v1/resources")

        assert response.status_code == 200, (
            f"GET /api/v1/resources returned {response.status_code}, expected 200"
        )
        assert response.json() == {"resources": [], "total": 0}
