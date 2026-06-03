"""Defense-in-depth tests for ``effective_*_limit`` properties (Issue #569).

Verifies the ``_zero_floor`` rule: when a plan tier's base value is 0,
no addon bonus can raise the effective limit above 0. This closes a
bypass where a misconfigured Stripe SKU or a manual ``WorkspaceAddon``
SQL insert on a zero-base tier would otherwise grant access to a
feature the tier explicitly excludes.

The rule was established for ``sleep_enabled_contexts_limit`` in #560 /
PR #568 and is now applied uniformly to all 12 ``effective_*_limit``
properties via ``models.auth._zero_floor``. These tests are
ORM-mock-only (no DB) so they run in ``make test-local``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from models.auth import Workspace, _zero_floor


class TestZeroFloorHelper:
    """Direct unit tests for the shared helper."""

    @pytest.mark.parametrize(
        ("base", "addon", "expected"),
        [
            (0, 0, 0),
            (0, 100, 0),  # bypass attempt — addon must be ignored
            (0, None, 0),
            (1, 0, 1),
            (1, None, 1),
            (10, 5, 15),
            (100, None, 100),  # None addon must be treated as 0
        ],
    )
    def test_zero_floor(self, base, addon, expected):
        assert _zero_floor(base, addon) == expected


# (property, tier_attr, addon_attr, addon_scale)
# ``addon_scale`` is the multiplier applied to the addon column before
# stacking; 1 for plain counts, (1024 * 1024) for storage (MB→bytes).
EFFECTIVE_PROPERTIES = [
    ("effective_memory_limit", "memory_limit", "addon_memory_bonus", 1),
    ("effective_mcp_calls_per_day", "mcp_calls_per_day", "addon_mcp_quota_bonus", 1),
    ("effective_mcp_calls_per_week", "mcp_calls_per_week", "addon_mcp_quota_bonus", 1),
    ("effective_rest_calls_per_day", "rest_calls_per_day", "addon_rest_quota_bonus", 1),
    ("effective_rest_calls_per_week", "rest_calls_per_week", "addon_rest_quota_bonus", 1),
    ("effective_public_calls_per_day", "public_calls_per_day", "addon_public_quota_bonus", 1),
    ("effective_public_calls_per_week", "public_calls_per_week", "addon_public_quota_bonus", 1),
    ("effective_max_contexts", "max_contexts_per_workspace", "addon_context_bonus", 1),
    ("effective_max_members", "max_members_per_workspace", "addon_member_bonus", 1),
    ("effective_analysis_runs_per_day", "analysis_runs_per_day", "addon_analysis_bonus", 1),
    (
        "effective_storage_limit_bytes",
        "storage_limit_bytes",
        "addon_storage_bonus_mb",
        1024 * 1024,
    ),
    (
        "effective_sleep_enabled_contexts_limit",
        "sleep_enabled_contexts_limit",
        "addon_sleep_contexts_bonus",
        1,
    ),
]


class TestZeroBaseBypassClosed:
    """For every ``effective_*_limit``, a synthetic tier with base=0 and
    a positive addon must still report effective=0. This is the core
    anti-bypass invariant of #569.

    Properties are invoked via the descriptor ``fget`` so we avoid
    constructing a real ``Workspace`` row (which would require every
    NOT NULL column to be populated just to read one derived value).
    """

    @pytest.mark.parametrize(
        ("prop_name", "tier_attr", "addon_attr", "addon_scale"),
        EFFECTIVE_PROPERTIES,
        ids=[p[0] for p in EFFECTIVE_PROPERTIES],
    )
    def test_zero_base_with_addon_returns_zero(self, prop_name, tier_attr, addon_attr, addon_scale):
        tier = MagicMock()
        setattr(tier, tier_attr, 0)
        ws = MagicMock()
        ws._plan_tier = tier
        setattr(ws, addon_attr, 5)  # any positive addon must be ignored
        assert getattr(Workspace, prop_name).fget(ws) == 0

    @pytest.mark.parametrize(
        ("prop_name", "tier_attr", "addon_attr", "addon_scale"),
        EFFECTIVE_PROPERTIES,
        ids=[p[0] for p in EFFECTIVE_PROPERTIES],
    )
    def test_nonzero_base_stacks_addon(self, prop_name, tier_attr, addon_attr, addon_scale):
        tier = MagicMock()
        setattr(tier, tier_attr, 100)
        ws = MagicMock()
        ws._plan_tier = tier
        setattr(ws, addon_attr, 5)
        assert getattr(Workspace, prop_name).fget(ws) == 100 + 5 * addon_scale

    @pytest.mark.parametrize(
        ("prop_name", "tier_attr", "addon_attr", "addon_scale"),
        EFFECTIVE_PROPERTIES,
        ids=[p[0] for p in EFFECTIVE_PROPERTIES],
    )
    def test_none_addon_treated_as_zero(self, prop_name, tier_attr, addon_attr, addon_scale):
        """The ``addon_*_bonus`` columns are ``nullable=False`` with
        ``server_default="0"`` so the DB never serves ``None``. This case
        covers transient ORM objects (unflushed instances where the
        attribute is still unset) and the defensive ``or 0`` coalescing
        in ``_zero_floor`` — the helper must not raise ``TypeError`` if
        it ever sees ``None``.
        """
        tier = MagicMock()
        setattr(tier, tier_attr, 100)
        ws = MagicMock()
        ws._plan_tier = tier
        setattr(ws, addon_attr, None)
        assert getattr(Workspace, prop_name).fget(ws) == 100


class TestEffectiveMaxConnectors:
    """Spec 2026-06-02: the ai-worker connector seat cap is plan tier base +
    the ``extra_connectors`` addon (mirrors ``effective_max_members``).
    Tier bases FREE=0 / BASIC=3 / PRO=10.
    """

    @pytest.mark.parametrize(
        ("plan_name", "expected"),
        [("free", 0), ("basic", 3), ("pro", 10)],
    )
    def test_tier_base_with_no_addon(self, plan_name, expected):
        from config.plan_tiers import get_plan_tier

        ws = MagicMock()
        ws._plan_tier = get_plan_tier(plan_name)
        ws.addon_connector_bonus = 0
        assert Workspace.effective_max_connectors.fget(ws) == expected

    def test_addon_stacks_on_tier_base(self):
        """The extra_connectors addon adds seats on top of the tier base."""
        from config.plan_tiers import get_plan_tier

        ws = MagicMock()
        ws._plan_tier = get_plan_tier("pro")  # base 10
        ws.addon_connector_bonus = 5
        assert Workspace.effective_max_connectors.fget(ws) == 15
