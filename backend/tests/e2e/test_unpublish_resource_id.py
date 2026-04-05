"""E2E test for unpublish → make private flow (#156).

Verifies that unpublishing a context clears resource_id,
preventing unique constraint violations on subsequent operations.

Requires running Docker services:
    docker compose up -d

Run:
    pytest tests/e2e/test_unpublish_resource_id.py -m e2e -v --no-cov
"""

import os
import time
import uuid

import httpx
import pytest

API_URL = os.environ.get("E2E_API_URL", "http://localhost:8080")
ADMIN_LOGIN_ID = os.environ.get("E2E_ADMIN_LOGIN_ID", "admin")
ADMIN_PASSWORD = os.environ.get("E2E_ADMIN_PASSWORD", "adminPass123!!!")

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="module")
def api_client():
    """Authenticated API client for the test module."""
    client = httpx.Client(base_url=API_URL, timeout=10.0)

    # Login
    resp = client.post(
        "/api/v1/auth/login",
        json={"login_id": ADMIN_LOGIN_ID, "password": ADMIN_PASSWORD},
    )
    assert resp.status_code == 200, f"Login failed: {resp.text}"

    yield client
    client.close()


@pytest.fixture
def test_context(api_client: httpx.Client):
    """Create a temporary context for testing, cleaned up after."""
    unique_name = f"e2e-unpublish-{uuid.uuid4().hex[:8]}"
    resp = api_client.post(
        "/api/v1/contexts",
        json={"name": unique_name, "description": "E2E test for #156"},
    )
    assert resp.status_code in (200, 201), f"Create context failed: {resp.text}"
    ctx = resp.json()
    ctx_id = ctx["id"]

    yield ctx

    # Cleanup: unlock then delete
    api_client.put(f"/api/v1/contexts/{ctx_id}", json={"is_locked": False})
    api_client.delete(f"/api/v1/contexts/{ctx_id}")


class TestUnpublishResourceId:
    """Test the unpublish → make private flow (#156)."""

    def test_full_publish_unpublish_cycle(self, api_client: httpx.Client, test_context: dict):
        """Full cycle: shared → public → unpublish → private → re-publish."""
        ctx_id = test_context["id"]
        resource_id = f"test_{uuid.uuid4().hex[:6]}"

        # Step 1: Make shared
        resp = api_client.put(
            f"/api/v1/contexts/{ctx_id}",
            json={"is_private": False},
        )
        assert resp.status_code == 200
        assert resp.json()["is_private"] is False

        # Step 2: Set resource_id and make public
        resp = api_client.put(
            f"/api/v1/contexts/{ctx_id}",
            json={"is_public": True, "resource_id": resource_id},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_public"] is True
        assert data["resource_id"] == resource_id

        # Step 3: Unpublish
        resp = api_client.put(
            f"/api/v1/contexts/{ctx_id}",
            json={"is_public": False},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_public"] is False
        assert data["resource_id"] is None, (
            f"resource_id should be cleared on unpublish, got: {data['resource_id']}"
        )

        # Step 4: Make private (this was the failing step in #156)
        resp = api_client.put(
            f"/api/v1/contexts/{ctx_id}",
            json={"is_private": True},
        )
        assert resp.status_code == 200, f"Make private failed (was #156 bug): {resp.text}"
        assert resp.json()["is_private"] is True

        # Step 5: Re-publish with same resource_id
        resp = api_client.put(
            f"/api/v1/contexts/{ctx_id}",
            json={"is_private": False},
        )
        assert resp.status_code == 200

        resp = api_client.put(
            f"/api/v1/contexts/{ctx_id}",
            json={"is_public": True, "resource_id": resource_id},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_public"] is True
        assert data["resource_id"] == resource_id

    def test_unpublish_clears_resource_id_in_db(self, api_client: httpx.Client, test_context: dict):
        """Verify resource_id is NULL in API response after unpublish."""
        ctx_id = test_context["id"]
        resource_id = f"dbcheck_{uuid.uuid4().hex[:6]}"

        # Setup: shared → public with resource_id
        api_client.put(f"/api/v1/contexts/{ctx_id}", json={"is_private": False})
        api_client.put(
            f"/api/v1/contexts/{ctx_id}",
            json={"is_public": True, "resource_id": resource_id},
        )

        # Unpublish
        api_client.put(f"/api/v1/contexts/{ctx_id}", json={"is_public": False})

        # GET to verify persisted state
        resp = api_client.get(f"/api/v1/contexts/{ctx_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["resource_id"] is None
        assert data["is_public"] is False
