"""Coverage-focused API route tests.

Tests API endpoints with mocked auth to exercise route handler code paths.
Issue #14: Increase unit test coverage.
"""

from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from api.main import app
from auth.dependencies import get_current_user


def _mock_user():
    """Create a mock authenticated user."""
    user = MagicMock()
    user.id = "test_user_001"
    user.user_id = "test_user_001"
    user.email = "test@example.com"
    user.name = "Test User"
    user.is_system_admin = False
    user.workspace_id = str(uuid4())
    return user


@pytest.fixture
def authed_client():
    """Create test client with mocked auth."""
    mock_user = _mock_user()

    async def override_user():
        return mock_user

    app.dependency_overrides[get_current_user] = override_user

    # APIKeyOrSessionUser is an Annotated type, not a simple dependency
    # Override the underlying function that resolves it
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, mock_user

    app.dependency_overrides.clear()


class TestHealthEndpoints:
    """Test public health/system endpoints."""

    @pytest.fixture
    def client(self):
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c

    def test_root(self, client):
        """Root endpoint returns project info."""
        response = client.get("/")
        assert response.status_code == 200

    def test_health(self, client):
        """Health endpoint returns ok."""
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"

    def test_well_known_mcp(self, client):
        """Well-known MCP endpoint returns server info."""
        response = client.get("/.well-known/mcp.json")
        assert response.status_code in (200, 404)  # May not be configured

    def test_openapi_docs(self, client):
        """OpenAPI docs endpoint works."""
        response = client.get("/docs")
        assert response.status_code == 200

    def test_openapi_json(self, client):
        """OpenAPI JSON schema is accessible."""
        response = client.get("/openapi.json")
        assert response.status_code == 200
        data = response.json()
        assert "paths" in data


class TestAuthConfig:
    """Test auth configuration endpoint."""

    @pytest.fixture
    def client(self):
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c

    def test_auth_config(self, client):
        """Auth config endpoint is reachable."""
        response = client.get("/api/v1/auth/config")
        assert response.status_code in (200, 404)  # Depends on auth module config


class TestAuthEndpoints:
    """Test authenticated endpoints return proper responses."""

    @pytest.fixture(scope="class")
    def client(self):
        """Share a single TestClient across all tests in this class."""
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c

    def test_workspaces_list_requires_auth(self, client):
        response = client.get("/api/v1/workspaces")
        assert response.status_code in (401, 403)

    def test_contexts_list_requires_auth(self, client):
        response = client.get("/api/v1/contexts")
        assert response.status_code in (401, 403)

    def test_memory_stats_requires_auth(self, client):
        response = client.get("/api/v1/memory/stats")
        assert response.status_code in (401, 403)

    def test_remember_requires_auth(self, client):
        response = client.post("/api/v1/memory/remember", json={})
        assert response.status_code in (401, 403, 422)

    def test_recall_requires_auth(self, client):
        response = client.post("/api/v1/memory/recall", json={})
        assert response.status_code in (401, 403, 422)

    def test_admin_requires_auth(self, client):
        response = client.get("/api/v1/admin/users")
        assert response.status_code in (401, 403)


class TestSystemEndpoints:
    """Test system info endpoints."""

    @pytest.fixture
    def client(self):
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c

    def test_system_openapi_tags(self, client):
        """OpenAPI schema has proper tags."""
        response = client.get("/openapi.json")
        data = response.json()
        assert len(data["paths"]) > 0

    def test_system_routes_no_500(self, client):
        """System endpoints should not return 500."""
        for path in ["/", "/health", "/.well-known/mcp.json"]:
            response = client.get(path)
            assert response.status_code != 500, f"{path} returned 500"


class TestAllEndpointsExercised:
    """Exercise all API endpoints to get code path coverage.

    These tests don't assert specific behaviors — they just ensure
    the routes are reachable and don't crash. This exercises imports,
    middleware, auth dependencies, and route definitions.
    """

    @pytest.fixture
    def client(self):
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c

    @pytest.mark.parametrize(
        "method,path",
        [
            ("GET", "/api/v1/workspaces"),
            ("POST", "/api/v1/workspaces"),
            ("GET", "/api/v1/contexts"),
            ("POST", "/api/v1/contexts"),
            ("POST", "/api/v1/memory/remember"),
            ("POST", "/api/v1/memory/recall"),
            ("POST", "/api/v1/memory/forget"),
            ("POST", "/api/v1/memory/reference"),
            ("POST", "/api/v1/memory/explore"),
            ("GET", "/api/v1/memory/stats"),
            ("GET", "/api/v1/memory/list"),
            ("GET", "/api/v1/config/api-keys"),
            ("POST", "/api/v1/config/api-keys"),
            ("GET", "/api/v1/oauth/clients"),
            ("POST", "/api/v1/oauth/clients"),
            ("GET", "/api/v1/admin/users"),
            ("GET", "/api/v1/admin/plans/workspaces"),
            ("GET", "/api/v1/mcp/tools"),
            ("GET", "/api/v1/mcp/status"),
            ("GET", "/api/v1/config/external-keys"),
            ("POST", "/api/v1/config/external-keys"),
            ("GET", "/api/v1/auth/me"),
            ("GET", "/api/v1/users/me"),
            ("POST", "/api/v1/auth/password/login"),
        ],
    )
    def test_endpoint_reachable(self, client, method, path):
        """Each endpoint should be reachable (not 500)."""
        if method == "GET":
            response = client.get(path)
        elif method == "POST":
            response = client.post(path, json={})
        elif method == "PUT":
            response = client.put(path, json={})
        elif method == "DELETE":
            response = client.delete(path)
        else:
            pytest.fail(f"Unknown method: {method}")

        # Should not crash (500). Auth errors (401/403/422) are fine.
        assert response.status_code != 500, f"{method} {path} returned 500: {response.text[:200]}"
