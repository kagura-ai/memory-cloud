"""Smoke tests for health and public endpoints.

Verifies that basic endpoints respond correctly after deployment.
No authentication required.
"""

from unittest.mock import AsyncMock, MagicMock, patch

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


class TestReadinessEndpoint:
    """Test /readiness probe for blue-green deploy (Issue #239).

    Uses mocks to make tests deterministic regardless of whether
    backends are actually running in the test environment.
    """

    def _mock_all_ok(self):
        """Patch all backends to succeed."""
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.execute = AsyncMock()

        mock_factory = MagicMock(return_value=MagicMock(return_value=mock_session))
        mock_qdrant = MagicMock()
        mock_qdrant.get_collections = AsyncMock()
        mock_redis = MagicMock()
        mock_redis.ping = AsyncMock(return_value=True)

        return (
            patch("db.base._get_session_factory", mock_factory),
            patch("db.qdrant.get_qdrant_client", return_value=mock_qdrant),
            patch("db.redis.get_redis_client", return_value=mock_redis),
        )

    def _mock_partial_failure(self):
        """Patch postgres to fail, others succeed."""
        mock_factory = MagicMock(side_effect=ConnectionError("pg down"))
        mock_qdrant = MagicMock()
        mock_qdrant.get_collections = AsyncMock()
        mock_redis = MagicMock()
        mock_redis.ping = AsyncMock(return_value=True)

        return (
            patch("db.base._get_session_factory", mock_factory),
            patch("db.qdrant.get_qdrant_client", return_value=mock_qdrant),
            patch("db.redis.get_redis_client", return_value=mock_redis),
        )

    def test_readiness_all_ok(self, client):
        """GET /readiness returns 200 when all backends are reachable."""
        p1, p2, p3 = self._mock_all_ok()
        with p1, p2, p3:
            response = client.get("/readiness")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ready"
        assert data["checks"] == {"postgres": "ok", "qdrant": "ok", "redis": "ok"}

    def test_readiness_partial_failure(self, client):
        """GET /readiness returns 503 when any backend is down."""
        p1, p2, p3 = self._mock_partial_failure()
        with p1, p2, p3:
            response = client.get("/readiness")
        assert response.status_code == 503
        data = response.json()
        assert data["status"] == "not_ready"
        assert data["checks"]["postgres"] == "error"
        assert data["checks"]["qdrant"] == "ok"
        assert data["checks"]["redis"] == "ok"

    def test_readiness_response_shape(self, client):
        """GET /readiness always returns the expected JSON structure."""
        response = client.get("/readiness")
        data = response.json()
        assert "status" in data
        assert data["status"] in ("ready", "not_ready")
        assert "checks" in data
        for backend in ("postgres", "qdrant", "redis"):
            assert backend in data["checks"]
            assert data["checks"][backend] in ("ok", "error")
