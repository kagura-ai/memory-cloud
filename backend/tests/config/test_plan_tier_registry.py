"""Plan tier registry invariants (#1548 — XL / ``promax`` tier).

Tiers are consumed from four independent places (``PlanName``, ``PLAN_TIERS``,
``TIER_RATE_LIMITS``, the ``valid_plan_name`` DB CHECK). A tier present in one
and missing from another only fails at request time for a workspace on that
tier — these tests pin the cross-registry consistency so a fifth tier cannot
repeat the mistake.
"""

from __future__ import annotations

import dataclasses

import pytest
from pydantic import ValidationError

from config.plan_tiers import (
    PLAN_ORDER,
    PLAN_TIERS,
    PlanName,
    get_plan_tier,
    has_feature,
    plan_at_least,
    plan_rank,
)
from config.rate_limits import TIER_RATE_LIMITS


def test_promax_is_a_plan_name() -> None:
    assert PlanName.PROMAX == "promax"
    assert PlanName("promax") is PlanName.PROMAX


def test_registry_lists_every_plan_name_in_upgrade_order() -> None:
    assert list(PLAN_TIERS) == list(PlanName)
    assert PLAN_ORDER == ("free", "basic", "pro", "promax")
    # Iteration order of PLAN_TIERS *is* the upgrade order relied on by the
    # plan endpoints — XL must be last.
    assert PLAN_ORDER[-1] == PlanName.PROMAX


def test_every_plan_name_has_a_rate_limit() -> None:
    assert set(TIER_RATE_LIMITS) == set(PlanName)
    rpm = [TIER_RATE_LIMITS[PlanName(name)].requests_per_minute for name in PLAN_ORDER]
    assert rpm == sorted(rpm), "requests/minute must not shrink on upgrade"


def test_promax_tier_values() -> None:
    xl = get_plan_tier("promax")
    assert xl.name == "promax"
    assert xl.display_name == "XL"
    assert xl.max_contexts_per_workspace == 1000
    assert xl.memory_limit == 100_000
    assert xl.max_members_per_workspace == 50
    # Legacy USD field: no pricing lives in this repo (#1096) — placeholder only.
    assert xl.price_monthly == 0


def test_promax_is_never_below_pro() -> None:
    pro = get_plan_tier("pro")
    xl = get_plan_tier("promax")
    for f in dataclasses.fields(pro):
        if f.name in ("name", "display_name", "price_monthly", "features"):
            continue
        pro_v, xl_v = getattr(pro, f.name), getattr(xl, f.name)
        if pro_v is None or xl_v is None:
            continue
        assert xl_v >= pro_v, f"{f.name}: promax {xl_v} < pro {pro_v}"
    assert xl.features >= pro.features


@pytest.mark.parametrize("feature", ["shared_contexts", "team_invitations", "public_contexts"])
def test_promax_has_every_pro_feature(feature: str) -> None:
    assert has_feature("promax", feature)


def test_plan_rank_and_at_least() -> None:
    assert [plan_rank(p) for p in PLAN_ORDER] == [0, 1, 2, 3]
    assert plan_rank("not-a-plan") == 0  # unknown ranks as the lowest tier
    assert plan_at_least("promax", "pro")
    assert plan_at_least("pro", "pro")
    assert not plan_at_least("basic", "pro")
    assert not plan_at_least(None, "basic")


def test_admin_plan_change_accepts_every_registered_tier() -> None:
    from api.routes.admin_plans import AdminUpdatePlanRequest

    for name in PLAN_ORDER:
        assert AdminUpdatePlanRequest(plan_name=name).plan_name == name
    with pytest.raises(ValidationError):
        AdminUpdatePlanRequest(plan_name="enterprise")
