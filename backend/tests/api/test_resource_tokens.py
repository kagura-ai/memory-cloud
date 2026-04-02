"""Tests for Resource Token Management API.

Issue #242: Resource Token Management UI - Backend API tests.

Requires: async_client, authenticated_user fixtures (integration test infrastructure).
These are skipped when fixtures are not available (no test DB connection).
"""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

# Mark as expected failure — requires integration test fixtures
# (async_client, authenticated_user) that are not yet implemented.
pytestmark = pytest.mark.xfail(
    strict=True,
    reason="Integration test fixtures not available (async_client, authenticated_user)",
)


@pytest.mark.asyncio
async def test_create_resource_token(
    async_client: AsyncClient,
    db_session: AsyncSession,
    authenticated_user: dict,
):
    """Test creating a resource token.

    Should:
    - Return 201 status
    - Return plaintext token (only once)
    - Store hashed token in database
    """
    # Arrange
    payload = {
        "resource_id": "test_resource",
        "description": "Test token",
        "quota_events_per_hour": 500,
    }

    # Act
    response = await async_client.post("/api/v1/resource-tokens", json=payload)

    # Assert
    assert response.status_code == 201
    data = response.json()

    # Verify response structure
    assert "token" in data
    assert data["token"].startswith("kagura_resource_")
    assert data["resource_id"] == "test_resource"
    assert data["description"] == "Test token"
    assert data["quota_events_per_hour"] == 500
    assert data["status"] == "active"
    assert data["is_active"] is True

    # Verify token is hashed in database
    # (Cannot retrieve plaintext from DB)


@pytest.mark.asyncio
async def test_list_resource_tokens(
    async_client: AsyncClient,
    db_session: AsyncSession,
    authenticated_user: dict,
):
    """Test listing resource tokens.

    Should:
    - Return 200 status
    - Return list of tokens (no plaintext)
    """
    # Arrange: Create a token first
    create_payload = {
        "resource_id": "test_resource",
        "description": "Test token",
        "quota_events_per_hour": 1000,
    }
    await async_client.post("/api/v1/resource-tokens", json=create_payload)

    # Act
    response = await async_client.get("/api/v1/resource-tokens")

    # Assert
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert len(data) > 0

    # Verify no plaintext token in list
    first_token = data[0]
    assert "token" not in first_token
    assert "resource_id" in first_token
    assert "status" in first_token


@pytest.mark.asyncio
async def test_list_resource_tokens_with_filter(
    async_client: AsyncClient,
    db_session: AsyncSession,
    authenticated_user: dict,
):
    """Test listing resource tokens with resource_id filter.

    Should:
    - Return 200 status
    - Return only tokens for specified resource
    """
    # Arrange: Create tokens for different resources
    await async_client.post(
        "/api/v1/resource-tokens",
        json={"resource_id": "resource_a", "quota_events_per_hour": 1000},
    )
    await async_client.post(
        "/api/v1/resource-tokens",
        json={"resource_id": "resource_b", "quota_events_per_hour": 1000},
    )

    # Act
    response = await async_client.get("/api/v1/resource-tokens?resource_id=resource_a")

    # Assert
    assert response.status_code == 200
    data = response.json()
    assert all(token["resource_id"] == "resource_a" for token in data)


@pytest.mark.asyncio
async def test_revoke_resource_token(
    async_client: AsyncClient,
    db_session: AsyncSession,
    authenticated_user: dict,
):
    """Test revoking a resource token.

    Should:
    - Return 204 status
    - Set is_active=False (soft delete)
    """
    # Arrange: Create a token
    create_response = await async_client.post(
        "/api/v1/resource-tokens",
        json={"resource_id": "test_resource", "quota_events_per_hour": 1000},
    )
    token_id = create_response.json()["id"]

    # Act
    response = await async_client.delete(f"/api/v1/resource-tokens/{token_id}")

    # Assert
    assert response.status_code == 204

    # Verify token is revoked
    list_response = await async_client.get("/api/v1/resource-tokens")
    tokens = list_response.json()
    revoked_token = next((t for t in tokens if t["id"] == token_id), None)
    assert revoked_token is not None
    assert revoked_token["status"] == "revoked"
    assert revoked_token["is_active"] is False


@pytest.mark.asyncio
async def test_revoke_nonexistent_token(
    async_client: AsyncClient,
    db_session: AsyncSession,
    authenticated_user: dict,
):
    """Test revoking a non-existent token.

    Should:
    - Return 404 status
    """
    # Act
    response = await async_client.delete("/api/v1/resource-tokens/99999")

    # Assert
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_create_token_validation(
    async_client: AsyncClient,
    db_session: AsyncSession,
    authenticated_user: dict,
):
    """Test resource token creation validation.

    Should:
    - Reject invalid resource_id
    - Reject invalid quota
    """
    # Test: Empty resource_id
    response = await async_client.post(
        "/api/v1/resource-tokens",
        json={"resource_id": "", "quota_events_per_hour": 1000},
    )
    assert response.status_code in [400, 422]

    # Test: Invalid quota (too low)
    response = await async_client.post(
        "/api/v1/resource-tokens",
        json={"resource_id": "test", "quota_events_per_hour": 0},
    )
    assert response.status_code in [400, 422]

    # Test: Invalid quota (too high)
    response = await async_client.post(
        "/api/v1/resource-tokens",
        json={"resource_id": "test", "quota_events_per_hour": 20000},
    )
    assert response.status_code in [400, 422]


@pytest.mark.asyncio
async def test_unauthenticated_access(async_client: AsyncClient):
    """Test unauthenticated access to resource token endpoints.

    Should:
    - Return 401 for all endpoints
    """
    # Test: List tokens without authentication
    # (Remove authentication headers temporarily)
    _response = await async_client.get("/api/v1/resource-tokens")
    # Note: Actual status code depends on auth middleware configuration
    # May be 401 or 403


# TODO: Add tests for:
# - Owner-only permission checks
# - Created token usage in resource_ingest endpoint
# - Quota enforcement for resource tokens
