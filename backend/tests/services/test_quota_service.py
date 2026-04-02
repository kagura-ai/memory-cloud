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

    def _make_workspace(self, workspace_id, plan_name, **kwargs):
        """Build a workspace mock with addon bonuses pre-set.

        addon_memory_bonus is set to 1 by default so that
        EffectiveQuotaService skips the addon-recalc branch
        (which fires only when ALL bonus columns are 0), keeping the
        total db.execute call count at exactly 4 per check_member_quota
        invocation.  addon_member_bonus stays 0 so the plan's native
        max_members limit is used unchanged.
        """
        workspace = MagicMock()
        workspace.id = workspace_id
        workspace.plan_name = plan_name
        workspace.addon_memory_bonus = 1  # non-zero: skips recalc branch
        workspace.addon_mcp_quota_bonus = 0
        workspace.addon_rest_quota_bonus = 0
        workspace.addon_public_quota_bonus = 0
        workspace.addon_member_bonus = 0
        workspace.addon_context_bonus = 0
        for k, v in kwargs.items():
            setattr(workspace, k, v)
        return workspace

    def _make_side_effects(self, workspace, member_count, pending_count):
        """Return the 4-element side_effect list for mock_db.execute.

        Call order inside check_member_quota:
          1. select(Workspace)                     – quota_service
          2. select(count(WorkspaceMember.id))     – member count
          3. select(count(WorkspaceInvitation.id)) – pending count
          4. select(Workspace)                     – EffectiveQuotaService
        """
        workspace_result = MagicMock()
        workspace_result.scalar_one_or_none = MagicMock(return_value=workspace)

        member_count_result = MagicMock()
        member_count_result.scalar = MagicMock(return_value=member_count)

        pending_count_result = MagicMock()
        pending_count_result.scalar = MagicMock(return_value=pending_count)

        effective_workspace_result = MagicMock()
        effective_workspace_result.scalar_one_or_none = MagicMock(return_value=workspace)

        return [
            workspace_result,
            member_count_result,
            pending_count_result,
            effective_workspace_result,
        ]

    @pytest.mark.asyncio
    async def test_check_member_quota_within_limit(self, service, mock_db, workspace_id):
        """Test quota check when within limit (Pro plan, 5/10 members)."""
        workspace = self._make_workspace(workspace_id, "pro", memory_limit=100000)
        mock_db.execute.side_effect = self._make_side_effects(workspace, 5, 0)

        can_invite, error = await service.check_member_quota(workspace_id)

        assert can_invite is True
        assert error is None

    @pytest.mark.asyncio
    async def test_check_member_quota_at_limit(self, service, mock_db, workspace_id):
        """Test quota check when at limit (Pro plan, 10/10 members)."""
        workspace = self._make_workspace(workspace_id, "pro")
        mock_db.execute.side_effect = self._make_side_effects(workspace, 10, 0)

        can_invite, error = await service.check_member_quota(workspace_id)

        assert can_invite is False
        assert error is not None
        assert "Member limit reached" in error
        assert "10 seats" in error

    @pytest.mark.asyncio
    async def test_check_member_quota_with_pending_invitations(
        self, service, mock_db, workspace_id
    ):
        """Test quota includes pending invitations (8 members + 2 pending = 10/10)."""
        workspace = self._make_workspace(workspace_id, "pro")
        mock_db.execute.side_effect = self._make_side_effects(workspace, 8, 2)

        can_invite, error = await service.check_member_quota(workspace_id)

        assert can_invite is False
        assert "Current members: 8, Pending invitations: 2" in error

    @pytest.mark.asyncio
    async def test_check_member_quota_basic_plan(self, service, mock_db, workspace_id):
        """Test quota for Basic plan (1 member limit, owner only)."""
        workspace = self._make_workspace(workspace_id, "basic")
        mock_db.execute.side_effect = self._make_side_effects(workspace, 1, 0)

        can_invite, error = await service.check_member_quota(workspace_id)

        assert can_invite is False
        assert "1 seats" in error

    @pytest.mark.asyncio
    async def test_check_member_quota_raise_on_exceeded(self, service, mock_db, workspace_id):
        """Test that QuotaExceededError is raised when raise_on_exceeded=True."""
        workspace = self._make_workspace(workspace_id, "pro")
        mock_db.execute.side_effect = self._make_side_effects(workspace, 10, 0)

        with pytest.raises(QuotaExceededError) as exc_info:
            await service.check_member_quota(workspace_id, raise_on_exceeded=True)

        assert "Member limit reached" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_check_member_quota_workspace_not_found(self, service, mock_db, workspace_id):
        """Test error when workspace doesn't exist."""
        workspace_result = MagicMock()
        workspace_result.scalar_one_or_none = MagicMock(return_value=None)
        mock_db.execute.side_effect = [workspace_result]

        can_invite, error = await service.check_member_quota(workspace_id)

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

        with pytest.raises(QuotaExceededError):
            await service.check_member_quota(workspace_id, raise_on_exceeded=True)

    @pytest.mark.asyncio
    async def test_check_member_quota_free_plan(self, service, mock_db, workspace_id):
        """Test quota for Free plan (1 member limit)."""
        workspace = self._make_workspace(workspace_id, "free")
        mock_db.execute.side_effect = self._make_side_effects(workspace, 1, 0)

        can_invite, error = await service.check_member_quota(workspace_id)

        assert can_invite is False
        assert "1 seats" in error

    @pytest.mark.asyncio
    async def test_check_member_quota_pro_available_seats(self, service, mock_db, workspace_id):
        """Test quota check returns success when seats available (Pro plan, 3/10)."""
        workspace = self._make_workspace(workspace_id, "pro")
        mock_db.execute.side_effect = self._make_side_effects(workspace, 2, 1)

        can_invite, error = await service.check_member_quota(workspace_id)

        assert can_invite is True
        assert error is None
