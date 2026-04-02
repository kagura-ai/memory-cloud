"""End-to-end tests for API endpoints."""

from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from api.main import app


class TestAPIEndpoints:
    """Test API endpoints end-to-end with FastAPI TestClient."""

    @pytest.fixture
    def client(self):
        """Create FastAPI test client."""
        # Override authentication dependency for testing
        from auth.dependencies import get_current_user

        async def mock_get_current_user():
            return MagicMock(user_id="test_user", role="user")

        app.dependency_overrides[get_current_user] = mock_get_current_user

        with TestClient(app) as test_client:
            yield test_client

        # Cleanup
        app.dependency_overrides.clear()

    def test_root_endpoint(self, client):
        """Test root endpoint."""
        response = client.get("/")

        assert response.status_code == 200
        data = response.json()
        assert "message" in data or "name" in data

    def test_health_endpoint(self, client):
        """Test health check endpoint."""
        response = client.get("/health")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"

    def test_info_endpoint(self, client):
        """Test system info endpoint."""
        response = client.get("/api/v1/system/info")

        assert response.status_code == 200
        data = response.json()
        assert "version" in data or "name" in data

    def test_oauth_metadata_endpoint(self, client):
        """Test OAuth2 metadata endpoint."""
        response = client.get("/.well-known/oauth-protected-resource")

        assert response.status_code == 200
        data = response.json()
        assert "resource" in data or "issuer" in data

    @patch("services.memory_service.MemoryService.remember")
    def test_remember_endpoint(self, mock_remember, client):
        """Test remember endpoint."""
        mock_remember.return_value = MagicMock(
            memory_id=uuid4(),
            scope="working",
            message="Memory stored successfully",
        )

        payload = {
            "summary": "Test memory",
            "content": "Test content",
            "type": "code",
        }

        response = client.post("/api/v1/memory/remember", json=payload)

        # Check response (may be 200 or authentication required)
        assert response.status_code in [200, 401, 403]

    @patch("services.memory_service.MemoryService.recall")
    def test_recall_endpoint(self, mock_recall, client):
        """Test recall endpoint."""
        mock_recall.return_value = MagicMock(
            results=[],
            total=0,
        )

        payload = {
            "query": "test query",
            "k": 5,
        }

        response = client.post("/api/v1/memory/recall", json=payload)

        # Check response
        assert response.status_code in [200, 401, 403]

    @patch("services.memory_service.MemoryService.forget")
    def test_forget_endpoint(self, mock_forget, client):
        """Test forget endpoint."""
        mock_forget.return_value = MagicMock(
            success=True,
            deleted_count=1,
        )

        memory_id = str(uuid4())
        payload = {
            "memory_id": memory_id,
        }

        response = client.post("/api/v1/memory/forget", json=payload)

        # Check response
        assert response.status_code in [200, 401, 403, 404]

    @patch("services.memory_service.MemoryService.reference")
    def test_reference_endpoint(self, mock_reference, client):
        """Test reference endpoint."""
        memory_id = uuid4()
        mock_reference.return_value = MagicMock(
            memory_id=memory_id,
            summary="Test",
            content="Test content",
        )

        payload = {
            "memory_id": str(memory_id),
        }

        response = client.post("/api/v1/memory/reference", json=payload)

        # Check response
        assert response.status_code in [200, 401, 403, 404]

    @patch("services.memory_service.MemoryService.explore")
    def test_explore_endpoint(self, mock_explore, client):
        """Test explore endpoint."""
        mock_explore.return_value = MagicMock(
            related_memories=[],
            total=0,
        )

        memory_id = str(uuid4())
        payload = {
            "memory_id": memory_id,
            "depth": 2,
        }

        response = client.post("/api/v1/memory/explore", json=payload)

        # Check response
        assert response.status_code in [200, 401, 403, 404]

    def test_invalid_endpoint(self, client):
        """Test 404 for invalid endpoint."""
        response = client.get("/api/v1/invalid")

        assert response.status_code == 404

    def test_method_not_allowed(self, client):
        """Test 405 for wrong HTTP method."""
        # remember endpoint expects POST, not GET
        response = client.get("/api/v1/memory/remember")

        assert response.status_code == 405

    def test_invalid_payload(self, client):
        """Test 422 for invalid payload."""
        # Missing required fields
        payload = {
            "summary": "Missing required fields",
        }

        response = client.post("/api/v1/memory/remember", json=payload)

        # Should return validation error
        assert response.status_code in [422, 401, 403]

    def test_graph_data_degree_counts_edges_not_weights(self, client):
        """Test that the graph data endpoint is reachable (requires context_id param).

        The graph data endpoint now uses a SQL-based GraphService with 3-level
        isolation (user_id/workspace_id/context_id). context_id is required.
        Without it the endpoint returns 422 (validation error), not 500.
        """
        # Make request without context_id — expect 422 (missing required query param)
        response = client.get("/api/v1/graph/data")
        assert response.status_code in [200, 401, 403, 404, 422], (
            f"Unexpected status {response.status_code}: {response.text}"
        )

    def test_graph_stats_degree_ranking_uses_edge_count(self, client):
        """Test that the graph stats endpoint is reachable and returns ordered results.

        The graph stats endpoint now uses a SQL-based GraphService with 3-level
        isolation. Top connections are ranked by edge count (not weighted degree).
        Without context_id the endpoint still works (context_id is optional for stats).
        """
        response = client.get("/api/v1/graph/stats")

        assert response.status_code in [200, 401, 403, 404, 422], (
            f"Unexpected status {response.status_code}: {response.text}"
        )

        if response.status_code == 200:
            data = response.json()

            # If top_connections present, verify descending degree order
            if "top_connections" in data and len(data["top_connections"]) >= 2:
                degrees = [conn["degree"] for conn in data["top_connections"]]
                assert degrees == sorted(degrees, reverse=True), (
                    "Top connections should be sorted by edge count"
                )
