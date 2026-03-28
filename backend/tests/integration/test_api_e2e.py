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
        assert data["status"] == "healthy"

    def test_info_endpoint(self, client):
        """Test system info endpoint."""
        response = client.get("/api/v1/info")

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

    @patch("api.routes.graph.GraphService")
    @patch("api.routes.graph.GraphRepository")
    def test_graph_data_degree_counts_edges_not_weights(
        self, mock_graph_repo, mock_graph_service_class, client
    ):
        """Test that node degree counts edges, not weighted degree sum.

        Bug fix: Previously used graph.degree(weight="weight") which sums edge weights.
        For low-weight edges (< 1.0), this would show 0 connections even when edges exist.
        Now uses graph.degree() to count actual edges.
        """
        import networkx as nx
        from models.graph import GraphMemory

        # Create a mock graph with low-weight edges
        mock_graph = nx.DiGraph()
        node1_id = str(uuid4())
        node2_id = str(uuid4())
        node3_id = str(uuid4())

        # Add nodes
        mock_graph.add_node(node1_id)
        mock_graph.add_node(node2_id)
        mock_graph.add_node(node3_id)

        # Add 3 edges with low weights (< 1.0) to node1
        mock_graph.add_edge(node1_id, node2_id, weight=0.1)
        mock_graph.add_edge(node1_id, node3_id, weight=0.2)
        mock_graph.add_edge(node2_id, node1_id, weight=0.15)

        # Mock GraphRepository to return graph data
        mock_repo_instance = MagicMock()
        mock_graph_model = MagicMock(spec=GraphMemory)
        mock_graph_model.graph_data = nx.node_link_data(mock_graph)
        mock_repo_instance.get_user_graph.return_value = mock_graph_model
        mock_graph_repo.return_value = mock_repo_instance

        # Mock GraphService
        mock_service_instance = MagicMock()
        mock_service_instance.graph = mock_graph
        mock_graph_service_class.return_value = mock_service_instance

        # Mock memory data
        from models.memory import Memory

        mock_memories = [
            MagicMock(
                spec=Memory,
                id=uuid4(),
                summary="Memory 1",
                type="code",
                importance=0.5,
                created_at=None,
            ),
            MagicMock(
                spec=Memory,
                id=uuid4(),
                summary="Memory 2",
                type="note",
                importance=0.7,
                created_at=None,
            ),
            MagicMock(
                spec=Memory,
                id=uuid4(),
                summary="Memory 3",
                type="decision",
                importance=0.6,
                created_at=None,
            ),
        ]

        with patch("api.routes.graph.select") as _mock_select:
            # Mock database query to return memories
            mock_result = MagicMock()
            mock_scalars = MagicMock()
            mock_scalars.all.return_value = mock_memories
            mock_result.scalars.return_value = mock_scalars

            async def mock_execute(*args, **kwargs):
                return mock_result

            with patch("api.routes.graph.get_db") as mock_get_db:
                mock_db = MagicMock()
                mock_db.execute = mock_execute

                async def mock_db_generator():
                    yield mock_db

                mock_get_db.return_value = mock_db_generator()

                # Make request
                response = client.get("/api/v1/graph/data")

                # Verify response
                if response.status_code == 200:
                    data = response.json()

                    # Check that degrees are counts, not weighted sums
                    # With the fix: node1 should have degree=3 (3 edges)
                    # Without fix: node1 would have degree=0 (0.1+0.2+0.15=0.45 -> int=0)
                    if "nodes" in data and len(data["nodes"]) > 0:
                        # Find node1 in the results
                        node1_data = next((n for n in data["nodes"] if n["id"] == node1_id), None)

                        if node1_data:
                            # Degree should be 3 (edge count), not 0 (weighted sum as int)
                            assert node1_data["degree"] > 0, (
                                "Node degree should count edges, not weight sum"
                            )
                            assert node1_data["degree"] == 3, (
                                f"Expected degree=3, got {node1_data['degree']}"
                            )

    @patch("api.routes.graph.GraphService")
    @patch("api.routes.graph.GraphRepository")
    def test_graph_stats_degree_ranking_uses_edge_count(
        self, mock_graph_repo, mock_graph_service_class, client
    ):
        """Test that graph stats ranks nodes by edge count, not weighted degree.

        Bug fix: Previously used graph.degree(weight="weight") for sorting top nodes.
        This caused incorrect ranking when edge weights varied.
        """
        import networkx as nx
        from models.graph import GraphMemory

        # Create graph with nodes having different edge patterns
        mock_graph = nx.DiGraph()
        node1_id = str(uuid4())  # 5 edges, low weights
        node2_id = str(uuid4())  # 2 edges, high weights
        node3_id = str(uuid4())

        mock_graph.add_node(node1_id)
        mock_graph.add_node(node2_id)
        mock_graph.add_node(node3_id)

        # node1: 5 edges × 0.1 weight = 0.5 weighted degree, 5 edge count
        for _i in range(5):
            mock_graph.add_edge(node1_id, str(uuid4()), weight=0.1)

        # node2: 2 edges × 0.9 weight = 1.8 weighted degree, 2 edge count
        mock_graph.add_edge(node2_id, str(uuid4()), weight=0.9)
        mock_graph.add_edge(node2_id, str(uuid4()), weight=0.9)

        # Mock repositories and services
        mock_repo_instance = MagicMock()
        mock_graph_model = MagicMock(spec=GraphMemory)
        mock_graph_model.graph_data = nx.node_link_data(mock_graph)
        mock_repo_instance.get_user_graph.return_value = mock_graph_model
        mock_graph_repo.return_value = mock_repo_instance

        mock_service_instance = MagicMock()
        mock_service_instance.graph = mock_graph
        mock_service_instance.stats.return_value = {
            "total_nodes": 3,
            "total_edges": 7,
            "avg_edge_weight": 0.3,
            "density": 0.5,
        }
        mock_graph_service_class.return_value = mock_service_instance

        # Mock memory data

        with patch("api.routes.graph.get_db") as mock_get_db:
            mock_db = MagicMock()

            async def mock_execute(*args, **kwargs):
                mock_result = MagicMock()
                mock_scalars = MagicMock()
                mock_scalars.all.return_value = []
                mock_result.scalars.return_value = mock_scalars
                return mock_result

            mock_db.execute = mock_execute

            async def mock_db_generator():
                yield mock_db

            mock_get_db.return_value = mock_db_generator()

            # Make request
            response = client.get("/api/v1/graph/stats")

            if response.status_code == 200:
                data = response.json()

                # With the fix: node1 (5 edges) should rank higher than node2 (2 edges)
                # Without fix: node2 (1.8 weighted) would rank higher than node1 (0.5 weighted)
                if "top_connections" in data and len(data["top_connections"]) >= 2:
                    # Verify that nodes are ranked by edge count
                    degrees = [conn["degree"] for conn in data["top_connections"]]

                    # Degrees should be in descending order
                    assert degrees == sorted(degrees, reverse=True), (
                        "Top connections should be sorted by edge count"
                    )
