"""The ``details`` builders behind every gate refusal (#1644).

``feature_gate_details`` / ``quota_gate_details`` are the single place a
refusal's machine-readable block is assembled, and ``lowest_tier_with_limit``
is the numeric twin of ``required_plan_name`` for caps that are not registry
features. What is pinned here: the tier comes from the registry (never a
literal), a ``PLAN_<KEY>_DISPLAY_NAME`` override flows through, and a cap no
tier raises yields ``None`` rather than an invented upgrade path.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import config.plan_tiers as plan_tiers
from config.constants import GATE_PLAN, GATE_QUOTA
from config.plan_tiers import (
    PLAN_ORDER,
    feature_gate_details,
    get_plan_tier,
    lowest_tier_with_limit,
    quota_gate_details,
)
from config.settings import Settings


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate the module registry so an override cannot leak between tests."""
    monkeypatch.setattr(plan_tiers, "PLAN_TIERS", dict(plan_tiers.PLAN_TIERS))
    monkeypatch.setattr(plan_tiers, "FEATURE_MIN_PLANS", dict(plan_tiers.FEATURE_MIN_PLANS))
    monkeypatch.setattr(plan_tiers, "logger", MagicMock())


def test_feature_gate_details_uses_registry_tier_not_a_literal() -> None:
    details = feature_gate_details("basic", "team_invitations")

    assert details["gate"] == GATE_PLAN
    assert details["feature"] == "team_invitations"
    assert details["current_plan"] == "basic"
    # The KEY a client decides with, and the LABEL a non-UI client renders —
    # both read from the registry, so a re-mapped feature moves both.
    assert details["required_plan"] == plan_tiers.FEATURE_MIN_PLANS["team_invitations"]
    assert details["required_plan_display"] == get_plan_tier(details["required_plan"]).display_name


def test_feature_gate_details_follows_a_display_name_override(registry: None) -> None:
    plan_tiers._apply_settings_overrides(
        Settings(_env_file=None, plan_pro_display_name="Team")  # type: ignore[call-arg]
    )

    details = feature_gate_details("basic", "team_invitations")

    assert details["required_plan"] == "pro", "the key is the contract; it never moves"
    assert details["required_plan_display"] == "Team"


def test_feature_gate_details_emits_none_when_no_tier_carries_the_feature() -> None:
    # ``sleep_mode`` is enforced numerically and has no FEATURE_MIN_PLANS row,
    # so it stands in for a feature an override stripped from every tier.
    details = feature_gate_details("free", "sleep_mode")

    assert details["required_plan"] is None
    # NEVER the "higher" prose fallback ``required_plan_display_name`` returns:
    # that is a sentence fragment, not a tier label.
    assert details["required_plan_display"] is None


def test_quota_gate_details_coerces_counts_and_resolves_the_upgrade_label() -> None:
    details = quota_gate_details(
        "free",
        "contexts",
        current=True,  # a bool/str count must not reach the wire untyped
        limit="1",  # type: ignore[arg-type]
        required_plan="basic",
        feature=None,
        resets_at=None,
    )

    assert details["gate"] == GATE_QUOTA
    assert details["quota_type"] == "contexts"
    assert details["current"] == 1
    assert details["limit"] == 1
    assert isinstance(details["current"], int)
    assert isinstance(details["limit"], int)
    assert details["required_plan_display"] == get_plan_tier("basic").display_name


def test_quota_gate_details_emits_none_display_for_an_unknown_required_plan() -> None:
    details = quota_gate_details("free", "agents", current=3, limit=3, required_plan="enterprise")

    assert details["required_plan"] == "enterprise"
    assert details["required_plan_display"] is None


def test_lowest_tier_with_limit_returns_the_first_tier_above_the_cap() -> None:
    free_contexts = get_plan_tier("free").max_contexts_per_workspace

    upgrade = lowest_tier_with_limit("max_contexts_per_workspace", free_contexts)

    assert upgrade is not None
    assert get_plan_tier(upgrade).max_contexts_per_workspace > free_contexts
    # "Lowest" means lowest in the upgrade order, not merely "some tier".
    for lower in PLAN_ORDER[: PLAN_ORDER.index(upgrade)]:
        assert get_plan_tier(lower).max_contexts_per_workspace <= free_contexts


def test_lowest_tier_with_limit_returns_none_when_no_tier_raises_it() -> None:
    highest = max(get_plan_tier(p).max_members_per_workspace for p in PLAN_ORDER)

    assert lowest_tier_with_limit("max_members_per_workspace", highest) is None
    # An attribute no tier declares (an env-driven cap such as agents) reads
    # as 0 everywhere and so has no upgrade path either.
    assert lowest_tier_with_limit("max_agents_per_workspace", 0) is None


def test_lowest_tier_with_limit_ignores_a_zero_floor_tier() -> None:
    # ``sleep_enabled_contexts_limit`` is 0 on the low tiers (#569 zero-floor:
    # 0 means "cannot at all", never "unlimited"), so the answer must skip
    # them and name the first tier that actually allows one.
    upgrade = lowest_tier_with_limit("sleep_enabled_contexts_limit", 0)

    assert upgrade is not None
    assert get_plan_tier(upgrade).sleep_enabled_contexts_limit > 0
    zero_floor = [p for p in PLAN_ORDER if get_plan_tier(p).sleep_enabled_contexts_limit == 0]
    assert zero_floor, "the fixture needs at least one zero-floor tier to be meaningful"
    assert upgrade not in zero_floor
