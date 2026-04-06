"""Regression tests for #198: workspace quota display bugs.

Three independent bugs in the workspace usage endpoint:

* **Bug B**: ``daily_api_limit`` was the unlabelled sum of MCP + REST,
  so PRO showed 55,000 instead of the marketed 50,000 MCP/day. The
  per-tier fields ``mcp_daily_limit`` / ``rest_daily_limit`` /
  ``public_daily_limit`` now expose the real per-tier numbers.

* **Bug C**: ``weekly_api_limit`` was hardcoded to ``daily * 7``, so
  PRO showed 385,000 even though ``plan_tiers.py`` actually permits
  275,000 (250,000 MCP/week + 25,000 REST/week). The rate limiter
  enforced the lower number, so the dashboard told users they had
  headroom they didn't really have. The fix sums the real per-tier
  weekly fields instead.

* **Bug D**: ``usage.py:get_current_usage`` counted soft-deleted
  memories, so the dashboard's "memories" card was higher than the
  underlying DB row count. Fix adds a ``deleted_at IS NULL`` filter.

These tests use mocked Workspace objects so they run without a live
database.
"""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from config.plan_tiers import get_plan_tier
from services.effective_quota_service import EffectiveQuotaService


class _NoopAddonCalculator:
    """Stub that replaces AddonCalculatorService in the all-bonuses-zero
    branch so the test never touches a live database."""

    def __init__(self, *_args, **_kwargs):
        pass

    async def recalculate_workspace_bonuses(self, *_args, **_kwargs):
        return None


@pytest.fixture
def stub_addon_calculator(monkeypatch):
    """Replace the addon recalc service with a noop for the duration of one test."""
    monkeypatch.setattr(
        "services.addon_calculator_service.AddonCalculatorService",
        _NoopAddonCalculator,
    )


def _build_pro_workspace_mock():
    """Mock Workspace whose effective_* properties return the PRO plan tier values."""
    ws = MagicMock()
    ws.plan_name = "pro"
    ws.addon_memory_bonus = 0
    ws.addon_mcp_quota_bonus = 0
    ws.addon_rest_quota_bonus = 0
    ws.addon_public_quota_bonus = 0
    ws.addon_member_bonus = 0
    ws.addon_context_bonus = 0

    tier = get_plan_tier("pro")
    ws.effective_memory_limit = tier.memory_limit
    ws.effective_mcp_calls_per_day = tier.mcp_calls_per_day
    ws.effective_mcp_calls_per_week = tier.mcp_calls_per_week
    ws.effective_rest_calls_per_day = tier.rest_calls_per_day
    ws.effective_rest_calls_per_week = tier.rest_calls_per_week
    ws.effective_public_calls_per_day = tier.public_calls_per_day
    ws.effective_public_calls_per_week = tier.public_calls_per_week
    ws.effective_max_contexts = tier.max_contexts_per_workspace
    ws.effective_max_members = tier.max_members_per_workspace
    return ws, tier


def _mock_db_returning(workspace):
    mock_db = MagicMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = workspace
    mock_db.execute = AsyncMock(return_value=mock_result)
    return mock_db


@pytest.mark.asyncio
class TestEffectiveQuotaWeeklyFields:
    """EffectiveQuotaService must expose the weekly fields the dashboard
    and rate limiter both need (regression for #198 Bug C)."""

    async def test_returns_weekly_fields(self, stub_addon_calculator):
        ws, tier = _build_pro_workspace_mock()
        service = EffectiveQuotaService(_mock_db_returning(ws))
        quotas = await service.get_effective_quotas(uuid4())
        # Bug C: weekly fields must come straight from plan_tiers, not
        # from a daily * 7 heuristic.
        assert quotas["mcp_calls_per_week"] == tier.mcp_calls_per_week
        assert quotas["rest_calls_per_week"] == tier.rest_calls_per_week
        assert quotas["public_calls_per_week"] == tier.public_calls_per_week

    async def test_weekly_is_not_daily_times_seven(self, stub_addon_calculator):
        """For PRO the legacy `daily * 7` heuristic produced
        56000 * 7 = 392000, but plan_tiers.py actually permits
        250000 (mcp) + 25000 (rest) + 5000 (public) = 280000. The rate
        limiter enforced the lower number, so the dashboard told users
        they had headroom they didn't really have. This is the precise
        discrepancy that caused unexpected 429s."""
        ws, _tier = _build_pro_workspace_mock()
        service = EffectiveQuotaService(_mock_db_returning(ws))
        quotas = await service.get_effective_quotas(uuid4())
        weekly_total = (
            quotas["mcp_calls_per_week"]
            + quotas["rest_calls_per_week"]
            + quotas["public_calls_per_week"]
        )
        daily_total = (
            quotas["mcp_calls_per_day"]
            + quotas["rest_calls_per_day"]
            + quotas["public_calls_per_day"]
        )
        # The whole point of #198 Bug C: weekly is NOT daily * 7.
        assert weekly_total != daily_total * 7
        # And the actual weekly cap matches plan_tiers.py for PRO
        # (250k MCP + 25k REST + 5k Public).
        assert weekly_total == 280000


class TestWorkspaceModelEffectiveProperties:
    """The Workspace model now exposes effective_*_per_week and
    effective_rest/public_calls_per_day as properties so the rest of
    the codebase doesn't need to compose them by hand."""

    def test_pro_plan_effective_properties(self):
        from config.plan_tiers import get_plan_tier
        from models.auth import Workspace

        ws = Workspace(plan_name="pro")
        # __init__ doesn't fully construct the SQLAlchemy attribute
        # machinery, so set the bonus columns directly.
        ws.addon_memory_bonus = 0
        ws.addon_mcp_quota_bonus = 0
        ws.addon_rest_quota_bonus = 0
        ws.addon_public_quota_bonus = 0
        ws.addon_member_bonus = 0
        ws.addon_context_bonus = 0

        tier = get_plan_tier("pro")
        assert ws.effective_mcp_calls_per_day == tier.mcp_calls_per_day
        assert ws.effective_mcp_calls_per_week == tier.mcp_calls_per_week
        assert ws.effective_rest_calls_per_day == tier.rest_calls_per_day
        assert ws.effective_rest_calls_per_week == tier.rest_calls_per_week
        assert ws.effective_public_calls_per_day == tier.public_calls_per_day
        assert ws.effective_public_calls_per_week == tier.public_calls_per_week

    def test_addon_bonuses_apply_to_per_tier_fields(self):
        from models.auth import Workspace

        ws = Workspace(plan_name="pro")
        ws.addon_memory_bonus = 0
        ws.addon_mcp_quota_bonus = 100
        ws.addon_rest_quota_bonus = 50
        ws.addon_public_quota_bonus = 25
        ws.addon_member_bonus = 0
        ws.addon_context_bonus = 0

        from config.plan_tiers import get_plan_tier

        tier = get_plan_tier("pro")
        assert ws.effective_mcp_calls_per_day == tier.mcp_calls_per_day + 100
        assert ws.effective_mcp_calls_per_week == tier.mcp_calls_per_week + 100
        assert ws.effective_rest_calls_per_day == tier.rest_calls_per_day + 50
        assert ws.effective_rest_calls_per_week == tier.rest_calls_per_week + 50
        assert ws.effective_public_calls_per_day == tier.public_calls_per_day + 25
        assert ws.effective_public_calls_per_week == tier.public_calls_per_week + 25
