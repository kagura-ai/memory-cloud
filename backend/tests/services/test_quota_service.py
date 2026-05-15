"""Tests for QuotaService.

Issue #229: Team member limit (10 members max for Pro plan)
Issue #661: Plan-based owned-workspace cap
"""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from config.plan_tiers import PlanName
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

        Issue #570 made ``EffectiveQuotaService.get_effective_quotas`` pure-read,
        so the historical "skip the addon-recalc branch by setting one bonus
        non-zero" trick is no longer load-bearing — the branch is gone.
        ``addon_member_bonus`` stays 0 so the plan's native ``max_members`` limit
        is used unchanged. The 4-execute call count for ``check_member_quota``
        below is still correct because there is no recalc-driven extra SELECT
        either before or after #570.
        """
        workspace = MagicMock()
        workspace.id = workspace_id
        workspace.plan_name = plan_name
        workspace.memory_limit = kwargs.get("memory_limit", 1000)
        workspace.addon_memory_bonus = 1
        workspace.addon_mcp_quota_bonus = 0
        workspace.addon_rest_quota_bonus = 0
        workspace.addon_public_quota_bonus = 0
        workspace.addon_member_bonus = 0
        workspace.addon_context_bonus = 0
        for k, v in kwargs.items():
            setattr(workspace, k, v)
        # Simulate @property effective quotas (plan tier lookup not available in mock)
        from config.plan_tiers import get_plan_tier

        tier = get_plan_tier(plan_name)
        workspace.effective_memory_limit = workspace.memory_limit + workspace.addon_memory_bonus
        workspace.effective_mcp_calls_per_day = (
            tier.mcp_calls_per_day + workspace.addon_mcp_quota_bonus
        )
        workspace.effective_max_members = (
            tier.max_members_per_workspace + workspace.addon_member_bonus
        )
        workspace.effective_max_contexts = (
            tier.max_contexts_per_workspace + workspace.addon_context_bonus
        )
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


class TestQuotaServiceWorkspaceCreationCap:
    """Test QuotaService.check_workspace_creation_allowed() — Issue #661.

    Mocks two execute() calls per check:
      1. SELECT count(Workspace.id) WHERE owner_user_id = X AND deleted_at IS NULL
      2. plan_resolver's SELECT Workspace.plan_name WHERE owner_user_id = X AND deleted_at IS NULL

    Settings are patched so each test controls ``enforce_workspace_cap``
    explicitly. Tier resolution is exercised through the real
    ``get_user_effective_plan`` and real ``PLAN_TIERS`` so the test
    locks the actual tier→cap mapping (Free=1 / Basic=3 / Pro=10).
    """

    @pytest.fixture
    def mock_db(self):
        db = MagicMock()
        db.execute = AsyncMock()
        return db

    @pytest.fixture
    def service(self, mock_db):
        return QuotaService(mock_db)

    def _patch_settings(self, enforce: bool):
        """Return a context manager that patches get_settings to return
        a settings object whose ``enforce_workspace_cap`` is ``enforce``.
        """
        mock_settings = MagicMock()
        mock_settings.enforce_workspace_cap = enforce
        return patch("config.settings.get_settings", return_value=mock_settings)

    def _arm_db(self, mock_db, owned_count: int, plan_names: list[str]):
        """Configure mock_db.execute side_effect for the two SELECTs.

        Order matches check_workspace_creation_allowed:
          1. count query → ``.scalar()``
          2. plan_resolver query → ``.scalars().all()``
        """
        count_result = MagicMock()
        count_result.scalar = MagicMock(return_value=owned_count)

        plan_scalars = MagicMock()
        plan_scalars.all = MagicMock(return_value=plan_names)
        plan_result = MagicMock()
        plan_result.scalars = MagicMock(return_value=plan_scalars)

        mock_db.execute.side_effect = [count_result, plan_result]

    # ------------------------------------------------------------------
    # Free tier (cap = 1)
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_free_user_zero_owned_can_create(self, service, mock_db):
        """Free user with 0 owned workspaces can create (count=0 < cap=1)."""
        self._arm_db(mock_db, owned_count=0, plan_names=[])
        with self._patch_settings(enforce=True):
            can_create, error = await service.check_workspace_creation_allowed("user-1")
        assert can_create is True
        assert error is None

    @pytest.mark.asyncio
    async def test_free_user_one_owned_denied_when_enforced(self, service, mock_db):
        """Free user with 1 owned workspace denied when flag=True."""
        self._arm_db(mock_db, owned_count=1, plan_names=[PlanName.FREE])
        with self._patch_settings(enforce=True):
            can_create, error = await service.check_workspace_creation_allowed("user-1")
        assert can_create is False
        assert error is not None
        assert "Workspace limit reached" in error
        assert "1 workspace" in error  # tier cap surfaced in message

    @pytest.mark.asyncio
    async def test_free_user_one_owned_allowed_when_flag_off(self, service, mock_db):
        """Free user with 1 owned workspace passes when flag=False (rollout gate)."""
        self._arm_db(mock_db, owned_count=1, plan_names=[PlanName.FREE])
        with self._patch_settings(enforce=False):
            can_create, error = await service.check_workspace_creation_allowed("user-1")
        # Flag off: still returns OK so the cap is observable via log only.
        assert can_create is True
        assert error is None

    # ------------------------------------------------------------------
    # Basic tier (cap = 3)
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_basic_user_two_owned_can_create(self, service, mock_db):
        """Basic user with 2 owned workspaces can create (count=2 < cap=3)."""
        self._arm_db(mock_db, owned_count=2, plan_names=[PlanName.BASIC, PlanName.BASIC])
        with self._patch_settings(enforce=True):
            can_create, error = await service.check_workspace_creation_allowed("user-1")
        assert can_create is True
        assert error is None

    @pytest.mark.asyncio
    async def test_basic_user_three_owned_denied(self, service, mock_db):
        """Basic user with 3 owned workspaces denied at cap."""
        self._arm_db(
            mock_db,
            owned_count=3,
            plan_names=[PlanName.BASIC, PlanName.BASIC, PlanName.BASIC],
        )
        with self._patch_settings(enforce=True):
            can_create, error = await service.check_workspace_creation_allowed("user-1")
        assert can_create is False
        assert error is not None
        assert "3 workspace" in error

    # ------------------------------------------------------------------
    # Pro tier (cap = 10)
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_pro_user_nine_owned_can_create(self, service, mock_db):
        """Pro user with 9 owned workspaces can create (count=9 < cap=10)."""
        self._arm_db(mock_db, owned_count=9, plan_names=[PlanName.PRO] * 9)
        with self._patch_settings(enforce=True):
            can_create, error = await service.check_workspace_creation_allowed("user-1")
        assert can_create is True
        assert error is None

    @pytest.mark.asyncio
    async def test_pro_user_ten_owned_denied(self, service, mock_db):
        """Pro user with 10 owned workspaces denied at cap."""
        self._arm_db(mock_db, owned_count=10, plan_names=[PlanName.PRO] * 10)
        with self._patch_settings(enforce=True):
            can_create, error = await service.check_workspace_creation_allowed("user-1")
        assert can_create is False
        assert "10 workspace" in error

    # ------------------------------------------------------------------
    # Mixed-tier ownership: highest tier wins
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_mixed_free_and_basic_uses_basic_cap(self, service, mock_db):
        """User owning 1 Free + 1 Basic gets Basic cap (3). With 2 owned → allowed."""
        self._arm_db(mock_db, owned_count=2, plan_names=[PlanName.FREE, PlanName.BASIC])
        with self._patch_settings(enforce=True):
            can_create, error = await service.check_workspace_creation_allowed("user-1")
        assert can_create is True
        assert error is None

    @pytest.mark.asyncio
    async def test_mixed_basic_and_pro_uses_pro_cap(self, service, mock_db):
        """User owning Basic + Pro gets Pro cap (10). With 5 owned → allowed."""
        self._arm_db(
            mock_db,
            owned_count=5,
            plan_names=[PlanName.BASIC, PlanName.PRO, PlanName.PRO, PlanName.PRO, PlanName.PRO],
        )
        with self._patch_settings(enforce=True):
            can_create, error = await service.check_workspace_creation_allowed("user-1")
        assert can_create is True
        assert error is None

    # ------------------------------------------------------------------
    # raise_on_denied behaviour
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_raises_on_denied_when_enforced(self, service, mock_db):
        """raise_on_denied=True raises QuotaExceededError when flag=True and over cap."""
        self._arm_db(mock_db, owned_count=1, plan_names=[PlanName.FREE])
        with self._patch_settings(enforce=True):
            with pytest.raises(QuotaExceededError) as exc_info:
                await service.check_workspace_creation_allowed("user-1", raise_on_denied=True)
        assert "Workspace limit reached" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_does_not_raise_when_flag_off(self, service, mock_db):
        """raise_on_denied=True does NOT raise when flag=False — log-only mode."""
        self._arm_db(mock_db, owned_count=1, plan_names=[PlanName.FREE])
        with self._patch_settings(enforce=False):
            # Should silently allow, not raise.
            can_create, error = await service.check_workspace_creation_allowed(
                "user-1", raise_on_denied=True
            )
        assert can_create is True
        assert error is None
