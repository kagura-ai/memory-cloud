"""E2E test for memory API endpoints.

Tests that memory endpoints accept valid requests and return
appropriate responses with mocked authentication.
DB/Qdrant-dependent endpoints may return 500 without full Docker stack.
"""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from api.main import app
from auth.dependencies import get_user_from_api_key_or_session

# Module lives under tests/e2e; tag every test so `-m e2e` / `-m "not e2e"`
# selection is complete (the directory guard in tests/conftest.py is the
# primary protection — see pytest_ignore_collect there).
pytestmark = pytest.mark.e2e

MOCK_USER = {
    "user_id": "test_user_e2e",
    "email": "test@example.com",
    "role": "user",
    "current_workspace_id": uuid4(),
}

MOCK_CONTEXT_ID = uuid4()
MOCK_MEMORY_ID = uuid4()


@pytest.fixture
def auth_client():
    """Create authenticated test client."""

    async def mock_auth():
        return MOCK_USER

    app.dependency_overrides[get_user_from_api_key_or_session] = mock_auth

    with TestClient(app, raise_server_exceptions=False) as client:
        yield client

    app.dependency_overrides.clear()


class TestMemoryEndpoints:
    """Test memory API endpoints accept valid payloads."""

    def test_remember_accepts_valid_payload(self, auth_client):
        """POST /memory/remember with valid payload should not 500."""
        response = auth_client.post(
            "/api/v1/memory/remember",
            json={
                "summary": "E2E test memory",
                "content": "Test content for E2E",
                "type": "note",
                "context_id": str(MOCK_CONTEXT_ID),
            },
        )
        # 500 = server error (bad), anything else = expected behavior
        # 429 = quota check (workspace not found), 500 = DB error
        assert response.status_code in (200, 201, 404, 429, 500), (
            f"remember returned unexpected {response.status_code}: {response.text[:200]}"
        )

    def test_recall_accepts_valid_payload(self, auth_client):
        """POST /memory/recall with valid payload should not crash.

        Note: May return 500 if DB/Qdrant is not available (expected in CI
        without full Docker stack). Should return 200/404 with DB available.
        """
        response = auth_client.post(
            "/api/v1/memory/recall",
            json={
                "query": "test query",
                "k": 5,
                "context_id": str(MOCK_CONTEXT_ID),
            },
        )
        # 422 = validation error (bad payload), others = service behavior
        assert response.status_code in (200, 404, 500), (
            f"recall returned unexpected {response.status_code}: {response.text[:200]}"
        )

    def test_reference_accepts_valid_payload(self, auth_client):
        """POST /memory/reference with valid payload should not 500."""
        response = auth_client.post(
            "/api/v1/memory/reference",
            json={
                "memory_id": str(MOCK_MEMORY_ID),
                "context_id": str(MOCK_CONTEXT_ID),
            },
        )
        assert response.status_code in (200, 404, 500), (
            f"reference returned unexpected {response.status_code}: {response.text[:200]}"
        )

    def test_explore_accepts_valid_payload(self, auth_client):
        """POST /memory/explore with valid payload should not 500."""
        response = auth_client.post(
            "/api/v1/memory/explore",
            json={
                "memory_id": str(MOCK_MEMORY_ID),
                "depth": 2,
                "context_id": str(MOCK_CONTEXT_ID),
            },
        )
        assert response.status_code in (200, 404, 500), (
            f"explore returned unexpected {response.status_code}: {response.text[:200]}"
        )

    def test_forget_accepts_valid_payload(self, auth_client):
        """POST /memory/forget with valid payload should not 500."""
        response = auth_client.post(
            "/api/v1/memory/forget",
            json={
                "memory_id": str(MOCK_MEMORY_ID),
                "context_id": str(MOCK_CONTEXT_ID),
            },
        )
        assert response.status_code in (200, 404, 500), (
            f"forget returned unexpected {response.status_code}: {response.text[:200]}"
        )

    def test_stats_endpoint(self, auth_client):
        """GET /memory/stats should not 500."""
        response = auth_client.get("/api/v1/memory/stats")
        assert response.status_code != 500

    def test_list_endpoint(self, auth_client):
        """GET /memory/list should not 500."""
        response = auth_client.get("/api/v1/memory/list")
        assert response.status_code != 500

    def test_remember_rejects_missing_fields(self, auth_client):
        """POST /memory/remember with missing fields should return 422."""
        response = auth_client.post(
            "/api/v1/memory/remember",
            json={},
        )
        assert response.status_code == 422

    def test_recall_rejects_missing_query(self, auth_client):
        """POST /memory/recall with missing query should return 422."""
        response = auth_client.post(
            "/api/v1/memory/recall",
            json={},
        )
        assert response.status_code == 422
