"""Smoke tests for authentication enforcement.

Verifies that protected endpoints return 401/403 when accessed
without authentication. Ensures no accidental public access.
"""

import pytest
from fastapi.testclient import TestClient

from api.main import app


@pytest.fixture
def client():
    """Create test client WITHOUT auth (no overrides)."""
    with TestClient(app) as c:
        yield c


# Endpoints that MUST require authentication
AUTH_REQUIRED_ENDPOINTS = [
    # User profile
    ("GET", "/api/v1/auth/me"),
    ("GET", "/api/v1/users/me"),
    ("GET", "/api/v1/users/profile"),
    # Workspaces
    ("GET", "/api/v1/workspaces"),
    ("POST", "/api/v1/workspaces"),
    # Contexts
    ("GET", "/api/v1/contexts"),
    ("POST", "/api/v1/contexts"),
    # Memory operations
    ("POST", "/api/v1/memory/remember"),
    ("POST", "/api/v1/memory/recall"),
    ("POST", "/api/v1/memory/forget"),
    ("POST", "/api/v1/memory/reference"),
    ("POST", "/api/v1/memory/explore"),
    ("GET", "/api/v1/memory/stats"),
    ("GET", "/api/v1/memory/list"),
    # API keys
    ("GET", "/api/v1/config/api-keys"),
    ("POST", "/api/v1/config/api-keys"),
    # OAuth clients
    ("GET", "/api/v1/oauth/clients"),
    ("POST", "/api/v1/oauth/clients"),
    # Admin
    ("GET", "/api/v1/admin/users"),
    ("GET", "/api/v1/admin/plans/workspaces"),
    ("GET", "/api/v1/admin/system-admins"),
    # MCP tools
    ("GET", "/api/v1/mcp/tools"),
    ("GET", "/api/v1/mcp/status"),
    # Usage: user-scoped /usage/* removed in #810 (use /workspace/usage/*).
]


class TestAuthRequired:
    """Verify that protected endpoints reject unauthenticated requests."""

    @pytest.mark.parametrize("method,path", AUTH_REQUIRED_ENDPOINTS)
    def test_returns_401_or_403(self, client, method, path):
        """Protected endpoint must return 401 or 403 without auth."""
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

        assert response.status_code in (401, 403, 405, 422), (
            f"{method} {path} returned {response.status_code}, expected 401/403 (auth required)"
        )
