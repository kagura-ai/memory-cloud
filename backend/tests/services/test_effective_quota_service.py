"""Tests for EffectiveQuotaService — addon bonus calculation.

Verifies that effective quotas = base plan limits + addon bonuses.
"""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from services.effective_quota_service import EffectiveQuotaService


class TestEffectiveQuotaService:
    """Test effective quota calculation (base + addons)."""

    @pytest.fixture
    def mock_db(self):
        return MagicMock()

    @pytest.fixture
    def service(self, mock_db):
        return EffectiveQuotaService(mock_db)

    def _mock_workspace(
        self,
        plan_name="pro",
        memory_limit=1000,
        daily_api_limit=1000,
        addon_memory_bonus=500,
        addon_mcp_quota_bonus=200,
        addon_rest_quota_bonus=100,
        addon_public_quota_bonus=50,
        addon_member_bonus=3,
        addon_context_bonus=5,
    ):
        """Create a mock workspace with addon bonuses and effective properties."""
        ws = MagicMock()
        ws.plan_name = plan_name
        ws.memory_limit = memory_limit
        ws.daily_api_limit = daily_api_limit
        ws.addon_memory_bonus = addon_memory_bonus
        ws.addon_mcp_quota_bonus = addon_mcp_quota_bonus
        ws.addon_rest_quota_bonus = addon_rest_quota_bonus
        ws.addon_public_quota_bonus = addon_public_quota_bonus
        ws.addon_member_bonus = addon_member_bonus
        ws.addon_context_bonus = addon_context_bonus
        from config.plan_tiers import get_plan_tier

        tier = get_plan_tier(plan_name)
        ws.effective_memory_limit = memory_limit + addon_memory_bonus
        ws.effective_mcp_calls_per_day = tier.mcp_calls_per_day + addon_mcp_quota_bonus
        ws.effective_max_contexts = tier.max_contexts_per_workspace + addon_context_bonus
        ws.effective_max_members = tier.max_members_per_workspace + addon_member_bonus
        return ws

    @pytest.mark.asyncio
    async def test_effective_memory_includes_addon(self, service, mock_db):
        """Effective memory limit = base + addon_memory_bonus."""
        ws = self._mock_workspace(memory_limit=1000, addon_memory_bonus=500)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute = AsyncMock(return_value=mock_result)

        quotas = await service.get_effective_quotas(uuid4())
        assert quotas["memory_limit"] == 1500  # 1000 + 500

    @pytest.mark.asyncio
    async def test_effective_mcp_includes_addon(self, service, mock_db):
        """Effective MCP calls = base + addon_mcp_quota_bonus."""
        ws = self._mock_workspace(addon_mcp_quota_bonus=200)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute = AsyncMock(return_value=mock_result)

        quotas = await service.get_effective_quotas(uuid4())
        assert quotas["mcp_calls_per_day"] > 200  # base + 200

    @pytest.mark.skip(reason="Zero-bonus path triggers AddonCalculatorService which needs real DB")
    @pytest.mark.asyncio
    async def test_no_addons_returns_base(self, service, mock_db):
        """With zero addon bonuses, recalculation is triggered (needs integration test)."""

    @pytest.mark.asyncio
    async def test_workspace_not_found(self, service, mock_db):
        """Missing workspace raises ValueError."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute = AsyncMock(return_value=mock_result)

        with pytest.raises(ValueError, match="not found"):
            await service.get_effective_quotas(uuid4())

    @pytest.mark.asyncio
    async def test_get_addon_summary(self, service, mock_db):
        """get_addon_summary returns all bonus fields."""
        ws = self._mock_workspace(
            addon_memory_bonus=500,
            addon_mcp_quota_bonus=200,
            addon_rest_quota_bonus=100,
            addon_public_quota_bonus=50,
            addon_member_bonus=3,
            addon_context_bonus=5,
        )
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute = AsyncMock(return_value=mock_result)

        summary = await service.get_addon_summary(uuid4())
        assert summary["addon_memory_bonus"] == 500
        assert summary["addon_mcp_quota_bonus"] == 200
        assert summary["addon_context_bonus"] == 5

    @pytest.mark.asyncio
    async def test_addon_summary_not_found(self, service, mock_db):
        """get_addon_summary with missing workspace raises ValueError."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute = AsyncMock(return_value=mock_result)

        with pytest.raises(ValueError):
            await service.get_addon_summary(uuid4())


class TestDashboardAddonReflection:
    """Verify EffectiveQuotaService returns correct values for dashboard display."""

    @pytest.mark.asyncio
    async def test_effective_limits_with_addons(self):
        """Service returns base + addon for all quota types."""
        mock_db = MagicMock()
        ws = MagicMock()
        ws.plan_name = "pro"
        ws.memory_limit = 1000
        ws.daily_api_limit = 1000
        ws.addon_memory_bonus = 500
        ws.addon_mcp_quota_bonus = 200
        ws.addon_rest_quota_bonus = 100
        ws.addon_public_quota_bonus = 50
        ws.addon_member_bonus = 3
        ws.addon_context_bonus = 5
        from config.plan_tiers import get_plan_tier

        tier = get_plan_tier("pro")
        ws.effective_memory_limit = 1000 + 500
        ws.effective_mcp_calls_per_day = tier.mcp_calls_per_day + 200
        ws.effective_mcp_calls_per_week = tier.mcp_calls_per_week + 200
        ws.effective_rest_calls_per_day = tier.rest_calls_per_day + 100
        ws.effective_rest_calls_per_week = tier.rest_calls_per_week + 100
        ws.effective_public_calls_per_day = tier.public_calls_per_day + 50
        ws.effective_public_calls_per_week = tier.public_calls_per_week + 50
        ws.effective_max_contexts = tier.max_contexts_per_workspace + 5
        ws.effective_max_members = tier.max_members_per_workspace + 3

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute = AsyncMock(return_value=mock_result)

        service = EffectiveQuotaService(mock_db)
        quotas = await service.get_effective_quotas(uuid4())

        assert quotas["memory_limit"] == 1500
        assert quotas["mcp_calls_per_day"] > 200  # base_mcp + 200
        assert quotas["rest_calls_per_day"] > 100  # base_rest + 100
        # Issue #198: weekly fields are now exposed too.
        assert quotas["mcp_calls_per_week"] == tier.mcp_calls_per_week + 200
        assert quotas["rest_calls_per_week"] == tier.rest_calls_per_week + 100
        assert quotas["public_calls_per_week"] == tier.public_calls_per_week + 50
