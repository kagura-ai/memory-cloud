"""Tests for PermissionService RBAC."""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from services.permission_service import (
    CONTEXT_ROLE_WEIGHTS,
    ORG_ROLE_WEIGHTS,
    PermissionService,
)


class TestRoleWeights:
    """Test role hierarchy definitions."""

    def test_org_role_hierarchy(self):
        """Owner > admin > member > viewer."""
        assert ORG_ROLE_WEIGHTS["owner"] > ORG_ROLE_WEIGHTS["admin"]
        assert ORG_ROLE_WEIGHTS["admin"] > ORG_ROLE_WEIGHTS["member"]
        assert ORG_ROLE_WEIGHTS["member"] > ORG_ROLE_WEIGHTS["viewer"]

    def test_context_role_hierarchy(self):
        """Owner > editor > viewer."""
        assert CONTEXT_ROLE_WEIGHTS["owner"] > CONTEXT_ROLE_WEIGHTS["editor"]
        assert CONTEXT_ROLE_WEIGHTS["editor"] > CONTEXT_ROLE_WEIGHTS["viewer"]


class TestWorkspaceAccess:
    """Test workspace-level permission checks."""

    @pytest.fixture
    def mock_db(self):
        """Create mock database session."""
        db = MagicMock()
        # Mock workspace exists (not deleted)
        mock_ws = MagicMock(id=uuid4(), deleted_at=None)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_ws
        db.execute = AsyncMock(return_value=mock_result)
        return db

    @pytest.fixture
    def service(self, mock_db):
        """Create PermissionService."""
        return PermissionService(mock_db)

    @pytest.mark.asyncio
    async def test_check_workspace_access_owner(self, service):
        """Owner should have access to any role requirement."""
        ws_id = uuid4()
        mock_member = MagicMock(role="owner")
        service.workspace_service.get_member = AsyncMock(return_value=mock_member)

        result = await service.check_workspace_access("user1", ws_id, required_role="owner")
        assert result == mock_member

    @pytest.mark.asyncio
    async def test_check_workspace_access_insufficient_role(self, service):
        """Viewer should not have admin access."""
        ws_id = uuid4()
        mock_member = MagicMock(role="viewer")
        service.workspace_service.get_member = AsyncMock(return_value=mock_member)

        with pytest.raises(HTTPException) as exc_info:
            await service.check_workspace_access("user1", ws_id, required_role="admin")
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_check_workspace_access_not_member(self, service):
        """Non-member should be denied."""
        ws_id = uuid4()
        service.workspace_service.get_member = AsyncMock(return_value=None)

        with pytest.raises(HTTPException) as exc_info:
            await service.check_workspace_access("user1", ws_id)
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_check_workspace_access_deleted_workspace(self, service, mock_db):
        """Deleted workspace should return 403."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None  # workspace deleted
        mock_db.execute = AsyncMock(return_value=mock_result)

        with pytest.raises(HTTPException) as exc_info:
            await service.check_workspace_access("user1", uuid4())
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_is_workspace_member_true(self, service):
        """User with membership returns True."""
        service.workspace_service.get_member = AsyncMock(return_value=MagicMock(role="member"))
        assert await service.is_workspace_member("user1", uuid4()) is True

    @pytest.mark.asyncio
    async def test_is_workspace_member_false(self, service):
        """User without membership returns False."""
        service.workspace_service.get_member = AsyncMock(return_value=None)
        assert await service.is_workspace_member("user1", uuid4()) is False

    @pytest.mark.asyncio
    async def test_check_workspace_owner(self, service):
        """Convenience method for owner check."""
        ws_id = uuid4()
        mock_member = MagicMock(role="owner")
        service.workspace_service.get_member = AsyncMock(return_value=mock_member)
        result = await service.check_workspace_owner("user1", ws_id)
        assert result == mock_member

    @pytest.mark.asyncio
    async def test_check_workspace_admin(self, service):
        """Convenience method for admin check."""
        ws_id = uuid4()
        mock_member = MagicMock(role="admin")
        service.workspace_service.get_member = AsyncMock(return_value=mock_member)
        result = await service.check_workspace_admin("user1", ws_id)
        assert result == mock_member


class TestContextAccess:
    """Test context-level permission checks."""

    @pytest.fixture
    def mock_db(self):
        """Create mock database session."""
        return MagicMock()

    @pytest.fixture
    def service(self, mock_db):
        return PermissionService(mock_db)

    @pytest.mark.asyncio
    async def test_context_access_private_creator(self, service, mock_db):
        """Creator of private context should have owner access."""
        ctx_id = uuid4()
        mock_ctx = MagicMock(
            id=ctx_id, is_private=True, created_by="user1", workspace_id=uuid4(), deleted_at=None
        )
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_ctx
        mock_db.execute = AsyncMock(return_value=mock_result)

        context, role = await service.check_context_access("user1", ctx_id)
        assert role == "owner"

    @pytest.mark.asyncio
    async def test_context_access_private_non_creator(self, service, mock_db):
        """Non-creator of private context should be denied."""
        ctx_id = uuid4()
        mock_ctx = MagicMock(
            id=ctx_id,
            is_private=True,
            created_by="other_user",
            workspace_id=uuid4(),
            deleted_at=None,
        )
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_ctx
        mock_db.execute = AsyncMock(return_value=mock_result)

        with pytest.raises(HTTPException) as exc_info:
            await service.check_context_access("user1", ctx_id)
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_context_not_found(self, service, mock_db):
        """Deleted or missing context should return 404."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute = AsyncMock(return_value=mock_result)

        with pytest.raises(HTTPException) as exc_info:
            await service.check_context_access("user1", uuid4())
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_can_manage_context_true(self, service, mock_db):
        """Owner can manage context."""
        ctx_id = uuid4()
        mock_ctx = MagicMock(id=ctx_id, is_private=True, created_by="user1", deleted_at=None)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_ctx
        mock_db.execute = AsyncMock(return_value=mock_result)

        assert await service.can_manage_context("user1", ctx_id) is True

    @pytest.mark.asyncio
    async def test_can_manage_context_false(self, service, mock_db):
        """Non-owner cannot manage context."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute = AsyncMock(return_value=mock_result)

        assert await service.can_manage_context("user1", uuid4()) is False


class TestMemoryAccessControl:
    """Test memory access control for team collaboration."""

    @pytest.fixture
    def service(self):
        return PermissionService(MagicMock())

    @pytest.mark.asyncio
    async def test_owner_can_access_own_memory(self, service):
        """Memory owner always has access."""
        result = await service.can_access_memory("user1", "user1", uuid4(), uuid4())
        assert result is True

    @pytest.mark.asyncio
    async def test_non_owner_private_context_denied(self, service):
        """Non-owner denied access to private context memory."""
        from unittest.mock import patch

        mock_ctx_svc = MagicMock()
        mock_ctx_svc.is_context_shared = AsyncMock(return_value=False)

        with patch("services.context_service.ContextService", return_value=mock_ctx_svc):
            result = await service.can_access_memory("user2", "user1", uuid4(), uuid4())
        assert result is False

    @pytest.mark.asyncio
    async def test_workspace_member_shared_context_access(self, service):
        """Workspace member can access shared context memory."""
        from unittest.mock import patch

        mock_ctx_svc = MagicMock()
        mock_ctx_svc.is_context_shared = AsyncMock(return_value=True)
        service.workspace_service.get_member = AsyncMock(return_value=MagicMock(role="member"))

        with patch("services.context_service.ContextService", return_value=mock_ctx_svc):
            result = await service.can_access_memory("user2", "user1", uuid4(), uuid4())
        assert result is True


class TestCountContextOwners:
    """Test count_context_owners helper (Issue #362)."""

    def _service_with_count(self, count_value: int) -> PermissionService:
        db = MagicMock()
        mock_result = MagicMock()
        mock_result.scalar_one.return_value = count_value
        db.execute = AsyncMock(return_value=mock_result)
        return PermissionService(db)

    @pytest.mark.asyncio
    async def test_returns_zero_when_no_owner_row(self):
        service = self._service_with_count(0)
        assert await service.count_context_owners(uuid4()) == 0

    @pytest.mark.asyncio
    async def test_returns_count_when_owners_present(self):
        service = self._service_with_count(3)
        assert await service.count_context_owners(uuid4()) == 3

    @pytest.mark.asyncio
    async def test_handles_none_result_as_zero(self):
        """scalar_one returning None (no rows) should coerce to 0."""
        db = MagicMock()
        mock_result = MagicMock()
        mock_result.scalar_one.return_value = None
        db.execute = AsyncMock(return_value=mock_result)
        service = PermissionService(db)
        assert await service.count_context_owners(uuid4()) == 0
