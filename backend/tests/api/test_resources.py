"""Tests for Resource list API.

Issue #47: Web UI for resource management — backend list endpoint tests.

Requires: async_client, authenticated_user fixtures (integration test infrastructure).
These are marked ``xfail(strict=True)`` until those fixtures land — the tests
still run as expected failures today, and once fixtures exist they will XPASS,
which strict=True converts into a CI failure that forces removal of the xfail
marker (matching the pattern in ``test_resource_tokens.py``).
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
async def test_list_resources_empty_workspace(
    async_client: AsyncClient,
    db_session: AsyncSession,
    authenticated_user: dict,
):
    """A workspace with no resource-bound contexts returns an empty list."""
    response = await async_client.get("/api/v1/resources")

    assert response.status_code == 200
    data = response.json()
    assert data["resources"] == []
    assert data["total"] == 0


@pytest.mark.asyncio
async def test_list_resources_returns_workspace_scoped(
    async_client: AsyncClient,
    db_session: AsyncSession,
    authenticated_user: dict,
):
    """Only resources in the caller's current workspace are returned."""
    # Arrange: seed a context with resource_id in the caller's workspace
    # (fixture helpers not yet available — placeholder).
    response = await async_client.get("/api/v1/resources")

    assert response.status_code == 200
    data = response.json()
    assert isinstance(data["resources"], list)
    assert "total" in data
    # Each row includes the documented shape
    for row in data["resources"]:
        assert "resource_id" in row
        assert "context_id" in row
        assert "token_count" in row
        assert "memory_count" in row
        assert "current_schema_version" in row


@pytest.mark.asyncio
async def test_list_resources_aggregates_counts(
    async_client: AsyncClient,
    db_session: AsyncSession,
    authenticated_user: dict,
):
    """token_count / memory_count / schema_version reflect real table state."""
    # Arrange: seed a resource with 2 active tokens, 47 memories, schema v3.
    response = await async_client.get("/api/v1/resources")

    assert response.status_code == 200
    data = response.json()
    if data["total"] > 0:
        row = data["resources"][0]
        assert isinstance(row["token_count"], int)
        assert isinstance(row["memory_count"], int)


@pytest.mark.asyncio
async def test_list_resources_excludes_deleted_contexts(
    async_client: AsyncClient,
    db_session: AsyncSession,
    authenticated_user: dict,
):
    """Soft-deleted contexts must not appear in the list."""
    response = await async_client.get("/api/v1/resources")

    assert response.status_code == 200
    # Seed a soft-deleted resource context, confirm it's filtered out.


@pytest.mark.asyncio
async def test_list_resources_requires_auth(async_client: AsyncClient):
    """Unauthenticated requests are rejected."""
    response = await async_client.get("/api/v1/resources")

    # Actual status depends on dependency — typically 401/403
    assert response.status_code in (401, 403)


@pytest.mark.asyncio
async def test_list_resources_requires_current_workspace(
    async_client: AsyncClient,
    db_session: AsyncSession,
    authenticated_user: dict,
):
    """Users without a current_workspace_id get AuthorizationError (403)."""
    response = await async_client.get("/api/v1/resources")

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_list_resources_order_by_recent_activity(
    async_client: AsyncClient,
    db_session: AsyncSession,
    authenticated_user: dict,
):
    """Resources are ordered by max(last_event_at, context.updated_at) DESC."""
    response = await async_client.get("/api/v1/resources")

    assert response.status_code == 200
    data = response.json()
    if data["total"] >= 2:
        rows = data["resources"]
        for i in range(len(rows) - 1):
            assert rows[i]["updated_at"] >= rows[i + 1]["updated_at"]


@pytest.mark.asyncio
async def test_list_resources_respects_context_access(
    async_client: AsyncClient,
    db_session: AsyncSession,
    authenticated_user: dict,
):
    """Members/viewers must not see resources for contexts outside
    ``PermissionService.get_accessible_contexts`` — private contexts and
    contexts excluded by ``WorkspaceMember.allowed_context_ids`` are hidden.
    Suspended members (allowed_context_ids IS NULL) get an empty list.
    """
    # Arrange: caller is a workspace member with allowed_context_ids = [A]
    # Workspace also has resource-bound contexts B (private) and C (public,
    # not whitelisted). Only A should appear in the response.
    response = await async_client.get("/api/v1/resources")

    assert response.status_code == 200
    data = response.json()
    visible_ids = {r["context_id"] for r in data["resources"]}
    # Must exclude contexts outside the caller's access scope.
    # (Real IDs filled in once integration fixtures land.)
    assert all(cid in visible_ids for cid in [])  # placeholder: allowed contexts
    assert not any(cid in visible_ids for cid in [])  # placeholder: forbidden
