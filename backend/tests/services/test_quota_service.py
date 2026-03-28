"""Tests for QuotaService.

Issue #229: Team member limit (10 members max for Pro plan)
"""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from services.quota_service import QuotaService
from utils.exceptions import QuotaExceededError


class TestQuotaServiceMemberQuota:
    """Test QuotaService.check_member_quota() for member limit enforcement."""

    @pytest.fixture
    def mock_db(self):
        """Create mock database session."""
        db = MagicMock()
        db.execute = AsyncMock()
        return db

    @pytest.fixture
    def service(self, mock_db):
        """Create QuotaService instance."""
        return QuotaService(mock_db)

    @pytest.fixture
    def workspace_id(self):
        """Generate test workspace ID."""
        return uuid4()

    @pytest.mark.asyncio
    async def test_check_member_quota_within_limit(self, service, mock_db, workspace_id):
        """Test quota check when within limit (Pro plan, 5/10 members)."""
        # Mock workspace (Pro plan)
        workspace = MagicMock()
        workspace.id = workspace_id
        workspace.plan_name = "pro"
        workspace.memory_limit = 100000

        # Mock query results
        workspace_result = MagicMock()
        workspace_result.scalar_one_or_none = MagicMock(return_value=workspace)

        member_count_result = MagicMock()
        member_count_result.scalar = MagicMock(return_value=5)

        pending_count_result = MagicMock()
        pending_count_result.scalar = MagicMock(return_value=0)

        # Set up execute mock to return different results for different queries
        mock_db.execute.side_effect = [
            workspace_result,
            member_count_result,
            pending_count_result,
        ]

        # Execute
        can_invite, error = await service.check_member_quota(workspace_id)

        # Assert
        assert can_invite is True
        assert error is None

    @pytest.mark.asyncio
    async def test_check_member_quota_at_limit(self, service, mock_db, workspace_id):
        """Test quota check when at limit (Pro plan, 10/10 members)."""
        # Mock workspace (Pro plan)
        workspace = MagicMock()
        workspace.id = workspace_id
        workspace.plan_name = "pro"

        workspace_result = MagicMock()
        workspace_result.scalar_one_or_none = MagicMock(return_value=workspace)

        member_count_result = MagicMock()
        member_count_result.scalar = MagicMock(return_value=10)

        pending_count_result = MagicMock()
        pending_count_result.scalar = MagicMock(return_value=0)

        mock_db.execute.side_effect = [
            workspace_result,
            member_count_result,
            pending_count_result,
        ]

        # Execute
        can_invite, error = await service.check_member_quota(workspace_id)

        # Assert
        assert can_invite is False
        assert error is not None
        assert "Member limit reached" in error
        assert "10 member(s)" in error

    @pytest.mark.asyncio
    async def test_check_member_quota_with_pending_invitations(
        self, service, mock_db, workspace_id
    ):
        """Test quota includes pending invitations (8 members + 2 pending = 10/10)."""
        workspace = MagicMock()
        workspace.id = workspace_id
        workspace.plan_name = "pro"

        workspace_result = MagicMock()
        workspace_result.scalar_one_or_none = MagicMock(return_value=workspace)

        member_count_result = MagicMock()
        member_count_result.scalar = MagicMock(return_value=8)

        pending_count_result = MagicMock()
        pending_count_result.scalar = MagicMock(return_value=2)

        mock_db.execute.side_effect = [
            workspace_result,
            member_count_result,
            pending_count_result,
        ]

        # Execute
        can_invite, error = await service.check_member_quota(workspace_id)

        # Assert
        assert can_invite is False
        assert "Current members: 8, Pending invitations: 2" in error

    @pytest.mark.asyncio
    async def test_check_member_quota_basic_plan(self, service, mock_db, workspace_id):
        """Test quota for Basic plan (1 member limit, owner only)."""
        workspace = MagicMock()
        workspace.id = workspace_id
        workspace.plan_name = "basic"

        workspace_result = MagicMock()
        workspace_result.scalar_one_or_none = MagicMock(return_value=workspace)

        member_count_result = MagicMock()
        member_count_result.scalar = MagicMock(return_value=1)

        pending_count_result = MagicMock()
        pending_count_result.scalar = MagicMock(return_value=0)

        mock_db.execute.side_effect = [
            workspace_result,
            member_count_result,
            pending_count_result,
        ]

        # Execute
        can_invite, error = await service.check_member_quota(workspace_id)

        # Assert
        assert can_invite is False
        assert "1 member(s)" in error

    @pytest.mark.asyncio
    async def test_check_member_quota_raise_on_exceeded(self, service, mock_db, workspace_id):
        """Test that QuotaExceededError is raised when raise_on_exceeded=True."""
        workspace = MagicMock()
        workspace.id = workspace_id
        workspace.plan_name = "pro"

        workspace_result = MagicMock()
        workspace_result.scalar_one_or_none = MagicMock(return_value=workspace)

        member_count_result = MagicMock()
        member_count_result.scalar = MagicMock(return_value=10)

        pending_count_result = MagicMock()
        pending_count_result.scalar = MagicMock(return_value=0)

        mock_db.execute.side_effect = [
            workspace_result,
            member_count_result,
            pending_count_result,
        ]

        # Execute & Assert
        with pytest.raises(QuotaExceededError) as exc_info:
            await service.check_member_quota(workspace_id, raise_on_exceeded=True)

        assert "Member limit reached" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_check_member_quota_workspace_not_found(self, service, mock_db, workspace_id):
        """Test error when workspace doesn't exist."""
        workspace_result = MagicMock()
        workspace_result.scalar_one_or_none = MagicMock(return_value=None)

        mock_db.execute.side_effect = [workspace_result]

        # Execute
        can_invite, error = await service.check_member_quota(workspace_id)

        # Assert
        assert can_invite is False
        assert "not found" in error

    @pytest.mark.asyncio
    async def test_check_member_quota_workspace_not_found_raises(
        self, service, mock_db, workspace_id
    ):
        """Test that QuotaExceededError is raised when workspace not found and raise_on_exceeded=True."""
        workspace_result = MagicMock()
        workspace_result.scalar_one_or_none = MagicMock(return_value=None)

        mock_db.execute.side_effect = [workspace_result]

        # Execute & Assert
        with pytest.raises(QuotaExceededError):
            await service.check_member_quota(workspace_id, raise_on_exceeded=True)

    @pytest.mark.asyncio
    async def test_check_member_quota_free_plan(self, service, mock_db, workspace_id):
        """Test quota for Free plan (1 member limit)."""
        workspace = MagicMock()
        workspace.id = workspace_id
        workspace.plan_name = "free"

        workspace_result = MagicMock()
        workspace_result.scalar_one_or_none = MagicMock(return_value=workspace)

        member_count_result = MagicMock()
        member_count_result.scalar = MagicMock(return_value=1)

        pending_count_result = MagicMock()
        pending_count_result.scalar = MagicMock(return_value=0)

        mock_db.execute.side_effect = [
            workspace_result,
            member_count_result,
            pending_count_result,
        ]

        # Execute
        can_invite, error = await service.check_member_quota(workspace_id)

        # Assert
        assert can_invite is False
        assert "Free plan allows 1 member(s)" in error

    @pytest.mark.asyncio
    async def test_check_member_quota_pro_available_seats(self, service, mock_db, workspace_id):
        """Test quota check returns success when seats available (Pro plan, 3/10)."""
        workspace = MagicMock()
        workspace.id = workspace_id
        workspace.plan_name = "pro"

        workspace_result = MagicMock()
        workspace_result.scalar_one_or_none = MagicMock(return_value=workspace)

        member_count_result = MagicMock()
        member_count_result.scalar = MagicMock(return_value=2)

        pending_count_result = MagicMock()
        pending_count_result.scalar = MagicMock(return_value=1)

        mock_db.execute.side_effect = [
            workspace_result,
            member_count_result,
            pending_count_result,
        ]

        # Execute
        can_invite, error = await service.check_member_quota(workspace_id)

        # Assert
        assert can_invite is True
        assert error is None
