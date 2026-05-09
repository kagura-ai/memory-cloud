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
        addon_storage_bonus_mb=0,
        addon_analysis_bonus=0,
        addon_sleep_contexts_bonus=0,
    ):
        """Create a mock workspace with addon bonuses and effective properties.

        All 12 ``effective_*`` properties read by
        ``EffectiveQuotaService.get_effective_quotas`` are set to concrete int
        values. Without explicit assignment, MagicMock attribute access
        produces nested MagicMocks — which silently pass equality checks but
        return non-int values and let drift between the service's returned
        keys and the mock's populated keys go undetected (Copilot review on
        PR #588 flagged this gap).
        """
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
        ws.addon_storage_bonus_mb = addon_storage_bonus_mb
        ws.addon_analysis_bonus = addon_analysis_bonus
        ws.addon_sleep_contexts_bonus = addon_sleep_contexts_bonus
        from config.plan_tiers import get_plan_tier

        tier = get_plan_tier(plan_name)
        ws.effective_memory_limit = memory_limit + addon_memory_bonus
        ws.effective_mcp_calls_per_day = tier.mcp_calls_per_day + addon_mcp_quota_bonus
        ws.effective_mcp_calls_per_week = tier.mcp_calls_per_week + addon_mcp_quota_bonus
        ws.effective_rest_calls_per_day = tier.rest_calls_per_day + addon_rest_quota_bonus
        ws.effective_rest_calls_per_week = tier.rest_calls_per_week + addon_rest_quota_bonus
        ws.effective_public_calls_per_day = tier.public_calls_per_day + addon_public_quota_bonus
        ws.effective_public_calls_per_week = tier.public_calls_per_week + addon_public_quota_bonus
        ws.effective_max_contexts = tier.max_contexts_per_workspace + addon_context_bonus
        ws.effective_max_members = tier.max_members_per_workspace + addon_member_bonus
        ws.effective_analysis_runs_per_day = tier.analysis_runs_per_day + addon_analysis_bonus
        ws.effective_storage_limit_bytes = (
            tier.storage_limit_bytes + addon_storage_bonus_mb * 1024 * 1024
        )
        ws.effective_sleep_enabled_contexts_limit = (
            getattr(tier, "sleep_enabled_contexts_per_workspace", 0) + addon_sleep_contexts_bonus
        )
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

    @pytest.mark.asyncio
    async def test_no_addons_returns_base_no_recalc(self, service, mock_db):
        """Issue #570: with all bonus columns at 0, the service is pure-read.

        Pre-#570 the zero-bonus path triggered ``AddonCalculatorService.recalculate_workspace_bonuses``
        which COMMITted from this GET path. The refactor makes the service trust the cached
        column values; ``recalculate_workspace_bonuses`` is now caller responsibility on the
        write path. This test pins the new contract.
        """
        ws = self._mock_workspace(
            addon_memory_bonus=0,
            addon_mcp_quota_bonus=0,
            addon_rest_quota_bonus=0,
            addon_public_quota_bonus=0,
            addon_member_bonus=0,
            addon_context_bonus=0,
            addon_storage_bonus_mb=0,
            addon_analysis_bonus=0,
            addon_sleep_contexts_bonus=0,
        )
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute = AsyncMock(return_value=mock_result)
        # Catch a stray COMMIT — the service must not write on read.
        mock_db.commit = AsyncMock()
        mock_db.flush = AsyncMock()

        quotas = await service.get_effective_quotas(uuid4())

        # Pure read: exactly one SELECT, no COMMIT, no FLUSH.
        assert mock_db.execute.await_count == 1
        mock_db.commit.assert_not_awaited()
        mock_db.flush.assert_not_awaited()

        # Drift guard: the service returns 12 keys, all int-typed and matching
        # the tier base when bonuses are 0. Without this whole-dict check, a
        # MagicMock fixture would silently produce non-int (MagicMock-typed)
        # values for any future field the service starts reading, and the test
        # would still pass — Copilot flagged this gap on PR #588.
        expected_keys = {
            "memory_limit",
            "mcp_calls_per_day",
            "mcp_calls_per_week",
            "rest_calls_per_day",
            "rest_calls_per_week",
            "public_calls_per_day",
            "public_calls_per_week",
            "max_members",
            "max_contexts",
            "analysis_runs_per_day",
            "storage_bytes_limit",
            "sleep_enabled_contexts_limit",
        }
        assert set(quotas.keys()) == expected_keys
        for key, value in quotas.items():
            assert isinstance(value, int), f"{key}={value!r} is not int"
        # Spot-check three independent dimensions to ensure mock wiring
        # actually returns tier-base values (not stray MagicMocks that pass
        # the int instance check via __index__-able sentinels).
        assert quotas["memory_limit"] == ws.effective_memory_limit
        assert quotas["mcp_calls_per_day"] == ws.effective_mcp_calls_per_day
        assert quotas["storage_bytes_limit"] == ws.effective_storage_limit_bytes

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
        ws.addon_analysis_bonus = 2  # Issue #494
        ws.addon_storage_bonus_mb = 250  # Issue #485
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
        ws.effective_analysis_runs_per_day = tier.analysis_runs_per_day + 2
        ws.effective_storage_limit_bytes = tier.storage_limit_bytes + 250 * 1024 * 1024

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
        # Issue #494: broadlistening analysis runs/day surfaces here too.
        assert quotas["analysis_runs_per_day"] == tier.analysis_runs_per_day + 2
        # Issue #485: storage byte limit surfaces here too.
        assert quotas["storage_bytes_limit"] == tier.storage_limit_bytes + 250 * 1024 * 1024


class TestStorageQuotaSurface:
    """Issue #485: storage_bytes_limit travels through EffectiveQuotaService."""

    def test_free_tier_default_is_100mb(self):
        """FREE plan default storage cap is 100 MiB (per #485 Phase 1 spec)."""
        from config.plan_tiers import get_plan_tier

        tier = get_plan_tier("free")
        assert tier.storage_limit_bytes == 100 * 1024 * 1024

    def test_workspace_effective_storage_property(self):
        """``Workspace.effective_storage_limit_bytes`` =
        ``tier.storage_limit_bytes + addon_storage_bonus_mb * 1 MiB``.

        Invoke the property's ``fget`` descriptor against a MagicMock so we
        don't need a real Workspace row — that would force every NOT NULL
        column to be supplied just to read one derived value.
        """
        from config.plan_tiers import get_plan_tier
        from models.auth import Workspace

        tier = get_plan_tier("pro")
        ws = MagicMock()
        ws._plan_tier = tier
        ws.addon_storage_bonus_mb = 1024  # +1 GiB

        result = Workspace.effective_storage_limit_bytes.fget(ws)
        assert result == tier.storage_limit_bytes + 1024 * 1024 * 1024
