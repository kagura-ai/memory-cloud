"""Smoke tests for health and public endpoints.

Verifies that basic endpoints respond correctly after deployment.
No authentication required.
"""

import pytest
from fastapi.testclient import TestClient

from api.main import app


@pytest.fixture
def client():
    """Create test client without auth overrides."""
    with TestClient(app) as c:
        yield c


class TestHealthEndpoints:
    """Test public health/info endpoints."""

    def test_root(self, client):
        """GET / returns 200 with app info."""
        response = client.get("/")
        assert response.status_code == 200
        data = response.json()
        assert "name" in data
        assert data["status"] == "ok"

    def test_health(self, client):
        """GET /health returns 200."""
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"

    def test_docs(self, client):
        """GET /docs returns 200 (Swagger UI)."""
        response = client.get("/docs")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    def test_redoc(self, client):
        """GET /redoc returns 200."""
        response = client.get("/redoc")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    def test_openapi_json(self, client):
        """GET /openapi.json returns valid OpenAPI spec."""
        response = client.get("/openapi.json")
        assert response.status_code == 200
        data = response.json()
        assert "openapi" in data
        assert "paths" in data

    def test_system_health(self, client):
        """GET /api/v1/system/health returns 200."""
        response = client.get("/api/v1/system/health")
        # May require auth but should not 500
        assert response.status_code in (200, 401, 403)

    def test_system_info(self, client):
        """GET /api/v1/system/info returns 200."""
        response = client.get("/api/v1/system/info")
        assert response.status_code in (200, 401, 403)
