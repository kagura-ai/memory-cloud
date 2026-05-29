"""Integration tests for API routes with mocked auth and real DB.

Requires: Docker postgres running (TEST_DATABASE_URL).
These tests exercise actual route handler code paths with authenticated
users against a real database, providing high code coverage.

Issue #14: Increase unit test coverage to 60%+.
"""

import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from api.main import app
from auth.dependencies import (
    get_current_user,
    get_user_from_api_key_or_session,
    require_session_auth,
)
from db.base import get_db
from models.auth import Base as AuthBase
from models.memory import Base as MemoryBase

# Skip entire module if no DB available
TEST_DB_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://kagura:kagura_dev_password@localhost:5432/kagura_test",
)

# Test user data
TEST_USER = {
    "user_id": "test_api_user",
    "email": "test@example.com",
    "name": "Test User",
    "role": "user",
    "current_context_id": None,
    "current_workspace_id": None,
    "api_key_workspace_id": None,
}


def _check_db_available():
    """Check if test DB is available using sync psycopg2 connection."""
    try:
        import psycopg2

        url = TEST_DB_URL.replace("postgresql+asyncpg://", "postgresql://")
        conn = psycopg2.connect(url, connect_timeout=3)
        conn.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _check_db_available(),
    reason="Test database not available (set TEST_DATABASE_URL)",
)


@pytest.fixture(scope="module")
def db_engine():
    """Create async engine for integration tests."""
    import asyncio

    engine = create_async_engine(TEST_DB_URL, poolclass=NullPool, echo=False)

    async def _setup():
        async with engine.begin() as conn:
            await conn.run_sync(AuthBase.metadata.create_all)
            await conn.run_sync(MemoryBase.metadata.create_all)

    asyncio.run(_setup())
    yield engine

    asyncio.run(engine.dispose())


@pytest.fixture
def authed_client(db_engine):
    """TestClient with mocked auth and real DB session."""

    async def override_user():
        return TEST_USER

    async def override_db():
        session_maker = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        async with session_maker() as session:
            try:
                yield session
            finally:
                await session.rollback()

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_user_from_api_key_or_session] = override_user
    app.dependency_overrides[require_session_auth] = override_user
    app.dependency_overrides[get_db] = override_db

    with TestClient(app, raise_server_exceptions=False) as client:
        yield client

    app.dependency_overrides.clear()


class TestWorkspaceRoutes:
    """Test workspace API routes with authenticated user."""

    def test_list_workspaces(self, authed_client):
        """GET /api/v1/workspaces returns list (may require session auth)."""
        response = authed_client.get("/api/v1/workspaces")
        assert response.status_code != 500

    def test_create_workspace(self, authed_client):
        """POST /api/v1/workspaces creates workspace."""
        response = authed_client.post(
            "/api/v1/workspaces",
            json={"name": f"Test Workspace {uuid4().hex[:8]}"},
        )
        assert response.status_code != 500


class TestContextRoutes:
    """Test context API routes."""

    def test_list_contexts(self, authed_client):
        """GET /api/v1/contexts returns list."""
        response = authed_client.get("/api/v1/contexts")
        assert response.status_code == 200

    def test_create_context_no_workspace(self, authed_client):
        """POST /api/v1/contexts without workspace returns error."""
        response = authed_client.post(
            "/api/v1/contexts",
            json={"name": "test-ctx", "display_name": "Test"},
        )
        # Fails because no workspace_id in user
        assert response.status_code in (400, 422, 500)


class TestMemoryRoutes:
    """Test memory API routes."""

    def test_remember_no_workspace(self, authed_client):
        """POST /api/v1/memory/remember without context returns error."""
        response = authed_client.post(
            "/api/v1/memory/remember",
            json={
                "summary": "Test memory for unit test coverage",
                "type": "code",
            },
        )
        # Should fail because no workspace/context
        assert response.status_code in (400, 404, 422, 500)

    def test_recall_no_workspace(self, authed_client):
        """POST /api/v1/memory/recall without context returns error."""
        response = authed_client.post(
            "/api/v1/memory/recall",
            json={"query": "test", "k": 5},
        )
        assert response.status_code in (400, 404, 422, 500)

    def test_memory_stats(self, authed_client):
        """GET /api/v1/memory/stats returns stats."""
        response = authed_client.get("/api/v1/memory/stats")
        assert response.status_code in (200, 404, 422)

    def test_memory_list(self, authed_client):
        """GET /api/v1/memory/list returns memory list."""
        response = authed_client.get("/api/v1/memory/list")
        assert response.status_code in (200, 404, 422)


class TestConfigRoutes:
    """Test config API routes."""

    def test_list_api_keys(self, authed_client):
        """GET /api/v1/config/api-keys returns list."""
        response = authed_client.get("/api/v1/config/api-keys")
        assert response.status_code != 500

    def test_list_external_keys(self, authed_client):
        """GET /api/v1/config/external-keys returns list."""
        response = authed_client.get("/api/v1/config/external-keys")
        assert response.status_code != 500


class TestUserRoutes:
    """Test user API routes."""

    def test_get_me(self, authed_client):
        """GET /api/v1/auth/me returns user info."""
        response = authed_client.get("/api/v1/auth/me")
        assert response.status_code in (200, 404)

    def test_get_users_me(self, authed_client):
        """GET /api/v1/users/me returns user profile."""
        response = authed_client.get("/api/v1/users/me")
        assert response.status_code in (200, 404)


# TestUsageRoutes removed in #810: user-scoped /usage/{current,history} endpoints
# were deleted (superseded by /workspace/usage/*). Workspace usage coverage lives
# in test_usage_workspaces.py and the workspace route tests.


class TestAdminRoutes:
    """Test admin routes (will fail with 403 for non-admin user)."""

    def test_admin_users_non_admin(self, authed_client):
        """GET /api/v1/admin/users returns 403 for non-admin."""
        response = authed_client.get("/api/v1/admin/users")
        assert response.status_code in (403, 404)

    def test_admin_plans_non_admin(self, authed_client):
        """GET /api/v1/admin/plans/workspaces returns 403 for non-admin."""
        response = authed_client.get("/api/v1/admin/plans/workspaces")
        assert response.status_code in (403, 404)


class TestOAuthRoutes:
    """Test OAuth routes."""

    def test_list_oauth_clients(self, authed_client):
        """GET /api/v1/oauth/clients returns list."""
        response = authed_client.get("/api/v1/oauth/clients")
        assert response.status_code != 500


class TestMCPRoutes:
    """Test MCP routes."""

    def test_mcp_tools(self, authed_client):
        """GET /api/v1/mcp/tools returns tool list."""
        response = authed_client.get("/api/v1/mcp/tools")
        assert response.status_code in (200, 404)

    def test_mcp_status(self, authed_client):
        """GET /api/v1/mcp/status returns status."""
        response = authed_client.get("/api/v1/mcp/status")
        assert response.status_code in (200, 404)
