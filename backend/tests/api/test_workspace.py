"""Tests for Workspace API Routes (Issue #115).

Tests:
- Workspace stats endpoint
- N+1 query optimization verification
- Error handling
- Empty context handling
- Member role updates (Issue #254)
"""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from api.routes.workspace import (
    ContextStats,
    WorkspaceStatsResponse,
    get_workspace_stats,
    get_workspace_usage_current,
)
from api.routes.workspaces import UpdateMemberRoleRequest, update_member_role


class TestWorkspaceStats:
    """Test workspace stats endpoint."""

    @pytest.fixture
    def mock_db(self):
        """Create mock database session."""
        return AsyncMock()

    @pytest.fixture
    def user_id(self):
        """Test user ID."""
        return "test_user_123"

    @pytest.fixture
    def mock_user(self, user_id):
        """Mock authenticated user."""
        return {"user_id": user_id, "email": "test@example.com", "role": "user"}

    @pytest.fixture
    def mock_contexts(self, user_id):
        """Create mock contexts."""
        context1 = MagicMock()
        context1.id = uuid4()
        context1.name = "default"
        context1.user_id = user_id

        context2 = MagicMock()
        context2.id = uuid4()
        context2.name = "work"
        context2.user_id = user_id

        return [context1, context2]

    @pytest.mark.asyncio
    async def test_no_user_id_returns_401(self, mock_db):
        """Test that missing user_id returns 401."""
        with pytest.raises(HTTPException) as exc_info:
            await get_workspace_stats(user={}, db=mock_db)

        assert exc_info.value.status_code == 401
        assert "User ID not found" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_empty_contexts_returns_empty_stats(self, mock_db, mock_user):
        """Test that user with no contexts returns empty stats.

        Note: Detailed stats aggregation is tested in test_single_collection_isolation.py.
        This test focuses on the empty case only.
        Issue #65: Updated for JOIN query (user + workspace in single query).
        """
        # Mock user with current_workspace_id
        mock_user_obj = MagicMock()
        mock_user_obj.current_workspace_id = uuid4()

        # Mock workspace
        mock_workspace = MagicMock()
        mock_workspace.plan_name = "free"

        # Mock JOIN result: one_or_none returns a row (directly unpackable)
        mock_join_row = (mock_user_obj, mock_workspace)

        # Mock empty contexts result
        mock_contexts_result = MagicMock()
        mock_contexts_result.scalars.return_value.all.return_value = []

        # Setup execute: JOIN(user+workspace), then empty contexts
        mock_db.execute.side_effect = [
            MagicMock(one_or_none=MagicMock(return_value=mock_join_row)),
            mock_contexts_result,
        ]

        response = await get_workspace_stats(user=mock_user, db=mock_db)

        assert response.total_memories == 0
        assert response.context_count == 0
        assert response.contexts == []
        assert response.plan_name == "free"

    @pytest.mark.skip(reason="Covered by test_single_collection_isolation.py integration tests")
    @pytest.mark.asyncio
    async def test_stats_aggregation(self, mock_db, mock_user, mock_contexts, user_id):
        """Test that stats are correctly aggregated across contexts.

        DEPRECATED: This test is too tightly coupled to implementation details.
        Stats aggregation is now properly tested in test_single_collection_isolation.py
        with real database queries.
        """
        pass

    @pytest.mark.skip(reason="Covered by test_single_collection_isolation.py integration tests")
    @pytest.mark.asyncio
    async def test_context_with_no_memories(self, mock_db, mock_user, mock_contexts, user_id):
        """Test that contexts with no memories show 0 count.

        DEPRECATED: This test is too tightly coupled to implementation details.
        Zero memory contexts are now properly tested in test_single_collection_isolation.py.
        """
        pass

    @pytest.mark.asyncio
    async def test_no_workspace_returns_empty_stats(self, mock_db, mock_user):
        """Test that user with no workspace returns empty stats (Issue #65: JOIN returns None)."""
        mock_db.execute.return_value = MagicMock(one_or_none=MagicMock(return_value=None))

        response = await get_workspace_stats(user=mock_user, db=mock_db)

        assert response.total_memories == 0
        assert response.context_count == 0
        assert response.contexts == []
        assert response.plan_name == "free"

    @pytest.mark.asyncio
    async def test_database_error_returns_500(self, mock_db, mock_user):
        """Test that database errors return 500 with safe message."""
        mock_db.execute.side_effect = Exception("Database connection failed")

        with pytest.raises(HTTPException) as exc_info:
            await get_workspace_stats(user=mock_user, db=mock_db)

        assert exc_info.value.status_code == 500
        # Should not leak internal error details
        assert "Database connection failed" not in exc_info.value.detail
        assert "Please try again later" in exc_info.value.detail

    @pytest.mark.skip(reason="Query optimization moved to service layer")
    @pytest.mark.asyncio
    async def test_query_count_optimization(self, mock_db, mock_user, mock_contexts, user_id):
        """Test query count optimization.

        DEPRECATED: Query optimization is now handled in WorkspaceService.get_collection_memory_stats().
        This test is no longer relevant with the service layer refactoring.
        """
        pass


class TestContextStatsModel:
    """Test ContextStats Pydantic model."""

    def test_context_stats_creation(self):
        """Test ContextStats model creation."""
        stats = ContextStats(
            context_id="test-id",
            context_name="default",
            created_by="user123",
            created_by_name="Test User",
            memory_count=10,
            is_private=False,
        )

        assert stats.context_id == "test-id"
        assert stats.context_name == "default"
        assert stats.memory_count == 10
        assert stats.is_private is False


class TestWorkspaceStatsResponse:
    """Test WorkspaceStatsResponse Pydantic model."""

    def test_response_creation(self):
        """Test WorkspaceStatsResponse model creation."""
        response = WorkspaceStatsResponse(
            total_memories=100,
            context_count=3,
            contexts=[
                ContextStats(
                    context_id="p1",
                    context_name="default",
                    created_by="user123",
                    created_by_name="Test User",
                    memory_count=50,
                    is_private=False,
                ),
                ContextStats(
                    context_id="p2",
                    context_name="work",
                    created_by="user123",
                    created_by_name="Test User",
                    memory_count=50,
                    is_private=False,
                ),
            ],
            plan_name="free",
        )

        assert response.total_memories == 100
        assert response.context_count == 3
        assert len(response.contexts) == 2
        assert response.plan_name == "free"


class TestUpdateMemberRole:
    """Test member role update endpoint (Issue #254)."""

    @pytest.fixture
    def workspace_id(self):
        """Test workspace ID."""
        return uuid4()

    @pytest.fixture
    def admin_user(self):
        """Mock admin user."""
        return {"user_id": "admin_123", "email": "admin@example.com"}

    @pytest.fixture
    def owner_user(self):
        """Mock owner user."""
        return {"user_id": "owner_123", "email": "owner@example.com"}

    @pytest.fixture
    def target_user_id(self):
        """Target user ID for role change."""
        return "member_456"

    @pytest.fixture
    def mock_request(self):
        """Mock FastAPI request."""
        return MagicMock()

    @pytest.fixture
    def mock_db(self):
        """Mock database session."""
        return AsyncMock()

    @pytest.mark.asyncio
    async def test_update_own_role_forbidden(self, workspace_id, admin_user, mock_request, mock_db):
        """Test that users cannot change their own role (Issue #254)."""
        # Mock current user
        with patch("api.routes.workspaces.get_current_user", return_value=admin_user):
            # Mock permission check to return admin member
            mock_admin_member = MagicMock()
            mock_admin_member.role = "admin"

            with patch("api.routes.workspaces.PermissionService") as mock_perm_service:
                mock_perm_service.return_value.check_workspace_admin = AsyncMock(
                    return_value=mock_admin_member
                )

                # Try to change own role
                body = UpdateMemberRoleRequest(role="member")

                with pytest.raises(HTTPException) as exc_info:
                    await update_member_role(
                        workspace_id=workspace_id,
                        user_id=admin_user["user_id"],  # Same as current user
                        body=body,
                        request=mock_request,
                        db=mock_db,
                    )

                assert exc_info.value.status_code == 403
                assert "own role" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_admin_cannot_change_owner_role(
        self, workspace_id, admin_user, target_user_id, mock_request, mock_db
    ):
        """Test that admins cannot change owner's role (Issue #254)."""
        # Mock current user as admin
        with patch("api.routes.workspaces.get_current_user", return_value=admin_user):
            # Mock permission check to return admin member
            mock_admin_member = MagicMock()
            mock_admin_member.role = "admin"

            # Mock target member as owner
            mock_owner_member = MagicMock()
            mock_owner_member.role = "owner"

            with patch("api.routes.workspaces.PermissionService") as mock_perm_service:
                mock_perm_service.return_value.check_workspace_admin = AsyncMock(
                    return_value=mock_admin_member
                )

                with patch("api.routes.workspaces.WorkspaceService") as mock_workspace_service:
                    mock_workspace_service.return_value.get_member = AsyncMock(
                        return_value=mock_owner_member
                    )

                    body = UpdateMemberRoleRequest(role="admin")

                    with pytest.raises(HTTPException) as exc_info:
                        await update_member_role(
                            workspace_id=workspace_id,
                            user_id=target_user_id,
                            body=body,
                            request=mock_request,
                            db=mock_db,
                        )

                    assert exc_info.value.status_code == 403
                    assert "owner" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_owner_can_change_owner_role(
        self, workspace_id, owner_user, target_user_id, mock_request, mock_db
    ):
        """Test that owners can change owner's role (Issue #254)."""
        # Mock current user as owner
        with patch("api.routes.workspaces.get_current_user", return_value=owner_user):
            # Mock permission check to return owner member
            mock_owner_member = MagicMock()
            mock_owner_member.role = "owner"

            # Mock target member as owner
            mock_target_owner = MagicMock()
            mock_target_owner.role = "owner"
            mock_target_owner.user_id = target_user_id
            mock_target_owner.joined_at = None

            with patch("api.routes.workspaces.PermissionService") as mock_perm_service:
                mock_perm_service.return_value.check_workspace_admin = AsyncMock(
                    return_value=mock_owner_member
                )

                with patch("api.routes.workspaces.WorkspaceService") as mock_workspace_service:
                    mock_workspace_service.return_value.get_member = AsyncMock(
                        return_value=mock_target_owner
                    )
                    mock_workspace_service.return_value.update_member_role = AsyncMock(
                        return_value=mock_target_owner
                    )

                    body = UpdateMemberRoleRequest(role="admin")

                    # Should succeed
                    response = await update_member_role(
                        workspace_id=workspace_id,
                        user_id=target_user_id,
                        body=body,
                        request=mock_request,
                        db=mock_db,
                    )

                    assert response.user_id == target_user_id

    @pytest.mark.asyncio
    async def test_admin_can_change_member_role(
        self, workspace_id, admin_user, target_user_id, mock_request, mock_db
    ):
        """Test that admins can change member's role (Issue #254)."""
        # Mock current user as admin
        with patch("api.routes.workspaces.get_current_user", return_value=admin_user):
            # Mock permission check to return admin member
            mock_admin_member = MagicMock()
            mock_admin_member.role = "admin"

            # Mock target member as regular member
            mock_target_member = MagicMock()
            mock_target_member.role = "member"
            mock_target_member.user_id = target_user_id
            mock_target_member.joined_at = None

            with patch("api.routes.workspaces.PermissionService") as mock_perm_service:
                mock_perm_service.return_value.check_workspace_admin = AsyncMock(
                    return_value=mock_admin_member
                )

                with patch("api.routes.workspaces.WorkspaceService") as mock_workspace_service:
                    mock_workspace_service.return_value.get_member = AsyncMock(
                        return_value=mock_target_member
                    )
                    mock_workspace_service.return_value.update_member_role = AsyncMock(
                        return_value=mock_target_member
                    )

                    body = UpdateMemberRoleRequest(role="viewer")

                    # Should succeed
                    response = await update_member_role(
                        workspace_id=workspace_id,
                        user_id=target_user_id,
                        body=body,
                        request=mock_request,
                        db=mock_db,
                    )

                    assert response.user_id == target_user_id


class TestWorkspaceUsageCurrent:
    """Tests for get_workspace_usage_current endpoint (Issue #65 optimization)."""

    @pytest.fixture
    def mock_db(self):
        """Create mock database session."""
        return AsyncMock()

    @pytest.fixture
    def workspace_id(self):
        return uuid4()

    @pytest.fixture
    def mock_user(self, workspace_id):
        return {"user_id": "test_user_123", "current_workspace_id": workspace_id}

    @pytest.fixture
    def mock_workspace(self):
        ws = MagicMock()
        ws.plan_name = "pro"
        ws.memory_limit = 10000
        ws.daily_api_limit = 1000
        ws.weekly_api_limit = 5000
        return ws

    @pytest.mark.asyncio
    async def test_no_workspace_returns_400(self, mock_db):
        """No workspace selected returns 400."""
        with pytest.raises(HTTPException) as exc_info:
            await get_workspace_usage_current(user={"user_id": "u1"}, db=mock_db)
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_workspace_not_found_returns_404(self, mock_db, mock_user):
        """Unknown workspace_id returns 404."""
        mock_db.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=None))

        with pytest.raises(HTTPException) as exc_info:
            await get_workspace_usage_current(user=mock_user, db=mock_db)
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_usage_aggregation_single_query(self, mock_db, mock_user, mock_workspace):
        """Issue #65: Verify single conditional aggregation query returns correct values."""
        mock_db.execute.side_effect = self._build_execute_side_effects(mock_workspace)

        with patch("services.effective_quota_service.EffectiveQuotaService") as mock_quota:
            mock_quota.return_value.get_effective_quotas = AsyncMock(
                return_value={
                    "memory_limit": 10000,
                    "mcp_calls_per_day": 500,
                    "rest_calls_per_day": 500,
                }
            )

            response = await get_workspace_usage_current(user=mock_user, db=mock_db)

        assert response.usage.memory_count == 42
        assert response.usage.api_calls_today == 100
        assert response.usage.mcp_calls_today == 60
        assert response.usage.public_calls_today == 15
        assert response.usage.rest_calls_today == 25
        assert response.usage.api_calls_this_week == 500
        assert response.usage.mcp_calls_this_week == 300
        assert response.usage.public_calls_this_week == 80
        assert response.usage.rest_calls_this_week == 120
        assert response.plan.plan_name == "pro"

    @pytest.mark.asyncio
    async def test_no_member_ids_query(self, mock_db, mock_user, mock_workspace):
        """Issue #65: Verify member_ids query is no longer executed (redundant IN-list removed)."""
        call_count = 0

        async def track_execute(stmt):
            nonlocal call_count
            call_count += 1
            results = self._build_execute_side_effects(mock_workspace)
            return results[call_count - 1]

        mock_db.execute = track_execute

        with patch("services.effective_quota_service.EffectiveQuotaService") as mock_quota:
            mock_quota.return_value.get_effective_quotas = AsyncMock(
                return_value={
                    "memory_limit": 10000,
                    "mcp_calls_per_day": 500,
                    "rest_calls_per_day": 500,
                }
            )
            await get_workspace_usage_current(user=mock_user, db=mock_db)

        # 3 queries: workspace, memory count, usage aggregation (no member_ids query)
        assert call_count == 3

    @staticmethod
    def _build_execute_side_effects(mock_workspace):
        """Build mock execute results for the 3 queries in get_workspace_usage_current."""
        # Query 1: workspace fetch
        workspace_result = MagicMock(scalar_one_or_none=MagicMock(return_value=mock_workspace))

        # Query 2: memory count (Issue #65: no member_ids IN-list)
        memory_result = MagicMock(scalar=MagicMock(return_value=42))

        # Query 3: single conditional aggregation (Issue #65: replaces 8 queries)
        usage_row = MagicMock()
        usage_row.total_today = 100
        usage_row.mcp_today = 60
        usage_row.public_today = 15
        usage_row.rest_today = 25
        usage_row.total_week = 500
        usage_row.mcp_week = 300
        usage_row.public_week = 80
        usage_row.rest_week = 120
        usage_result = MagicMock(one=MagicMock(return_value=usage_row))

        return [workspace_result, memory_result, usage_result]
