"""URL validation tests for all API routes.

Ensures no route returns 500 (Internal Server Error) when accessed
without authentication. This catches import errors, missing dependencies,
and misconfigured routes.
"""

import pytest
from fastapi.testclient import TestClient

from api.main import app


@pytest.fixture
def client():
    """Create test client without auth (raw access)."""
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _get_all_routes():
    """Extract all routes from the FastAPI app."""
    routes = []
    for route in app.routes:
        if not hasattr(route, "methods") or not hasattr(route, "path"):
            continue
        # Skip MCP wildcard routes (require special handling)
        if "/mcp" in route.path:
            continue
        # Skip OpenAPI internal routes
        if route.path in ("/openapi.json", "/docs/oauth2-redirect"):
            continue
        # Skip OAuth routes that require external provider config
        if (
            "/auth/google" in route.path
            or "/auth/github" in route.path
            or "/oauth/token" in route.path
        ):
            continue
        for method in route.methods:
            if method in ("HEAD", "OPTIONS"):
                continue
            routes.append((method, route.path))
    return sorted(routes, key=lambda x: (x[1], x[0]))


# Paths with parameters need dummy values
PARAM_DEFAULTS = {
    "workspace_id": "00000000-0000-0000-0000-000000000001",
    "context_id": "00000000-0000-0000-0000-000000000002",
    "user_id": "test_user_dummy",
    "key_id": "1",
    "key_name": "OPENAI_API_KEY",
    "client_id": "1",
    "invitation_id": "1",
    "token_id": "1",
    "grant_id": "00000000-0000-0000-0000-000000000003",  # Issue #1470: referral ledger row
    "resource_id": "test-resource",
    "token": "dummy-token",
    "key": "test-key",
}


def _resolve_path(path: str) -> str:
    """Replace path parameters with dummy values."""
    for param, value in PARAM_DEFAULTS.items():
        path = path.replace(f"{{{param}}}", value)
    return path


ALL_ROUTES = _get_all_routes()


class TestAllRoutesNoServerError:
    """Verify that no route returns 500 Internal Server Error.

    This catches:
    - Import errors in route modules
    - Missing dependency injections
    - Misconfigured middleware
    - Unhandled exceptions before auth check
    """

    @pytest.mark.parametrize("method,path", ALL_ROUTES, ids=[f"{m} {p}" for m, p in ALL_ROUTES])
    def test_route_does_not_500(self, client, method, path):
        """Route should never return 500 (even without auth)."""
        resolved_path = _resolve_path(path)

        if method == "GET":
            response = client.get(resolved_path)
        elif method == "POST":
            response = client.post(resolved_path, json={})
        elif method == "PUT":
            response = client.put(resolved_path, json={})
        elif method == "DELETE":
            response = client.delete(resolved_path)
        elif method == "PATCH":
            response = client.patch(resolved_path, json={})
        else:
            pytest.skip(f"Unsupported method: {method}")

        # 500 = server error (always bad)
        # Expected: 401, 403, 404, 405, 422 (all acceptable without auth/data)
        assert response.status_code != 500, (
            f"{method} {resolved_path} returned 500: {response.text[:300]}"
        )
