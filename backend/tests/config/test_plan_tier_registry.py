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
    feature_denied_message,
    get_plan_tier,
    get_required_plan_for_feature,
    has_feature,
    plan_at_least,
    plan_rank,
    required_plan_display_name,
)
from config.rate_limits import TIER_RATE_LIMITS

# #1551: creation of these is XL-only. Their numeric caps on M/L stay > 0 so
# objects that already exist keep serving (block-new-only).
XL_ONLY_FEATURES = ("resources", "connectors", "public_contexts")


def test_promax_is_a_plan_name() -> None:
    assert PlanName.PROMAX == "promax"
    assert PlanName("promax") is PlanName.PROMAX


def test_registry_lists_every_plan_name_in_upgrade_order() -> None:
    assert list(PLAN_TIERS) == list(PlanName)
    assert PLAN_ORDER == ("free", "basic", "pro", "promax")
    # Iteration order of PLAN_TIERS *is* the upgrade order relied on by the
    # plan endpoints — XL must be last.
    assert PLAN_ORDER[-1] == PlanName.PROMAX


def test_db_check_constraint_lists_every_plan_name() -> None:
    """The ``valid_plan_name`` CHECK on ``workspaces.plan_name`` is the fourth
    registry. ``test_schema_drift`` pins ORM ↔ migration; this pins ORM ↔ code,
    so adding a tier to ``PlanName`` without the model + migration fails here."""
    from sqlalchemy import CheckConstraint

    from models.auth import Workspace

    checks = [
        c
        for c in Workspace.__table__.constraints
        if isinstance(c, CheckConstraint) and c.name == "valid_plan_name"
    ]
    assert len(checks) == 1
    expected = "plan_name IN (" + ", ".join(f"'{name}'" for name in PLAN_ORDER) + ")"
    assert str(checks[0].sqltext) == expected


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


def test_owned_workspace_grants_are_0_0_2_19_and_monotonic() -> None:
    """#1550: the tier GRANTS owned-workspace slots on top of the per-user base
    (1) and slot bonus — totals 1 / 1 / 3 / 20 with bonus 0."""
    grants = [get_plan_tier(p).owned_workspace_grant for p in PLAN_ORDER]
    assert grants == [0, 0, 2, 19]
    assert grants == sorted(grants), "a higher tier never grants fewer workspaces"


def test_admin_plan_change_accepts_every_registered_tier() -> None:
    from api.routes.admin_plans import AdminUpdatePlanRequest

    for name in PLAN_ORDER:
        assert AdminUpdatePlanRequest(plan_name=name).plan_name == name
    with pytest.raises(ValidationError):
        AdminUpdatePlanRequest(plan_name="enterprise")


# ---------------------------------------------------------------------------
# #1551 — resources / connectors / public are XL-only ("may create" gate)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("feature", XL_ONLY_FEATURES)
def test_xl_only_features_are_promax_exclusive(feature: str) -> None:
    assert [has_feature(p, feature) for p in PLAN_ORDER] == [False, False, False, True]
    assert get_required_plan_for_feature(feature) == PlanName.PROMAX


def test_pro_no_longer_carries_public_contexts() -> None:
    assert "public_contexts" not in get_plan_tier("pro").features
    assert "public_contexts" in get_plan_tier("promax").features


@pytest.mark.parametrize("feature", ["shared_contexts", "team_invitations", "memory_analysis"])
def test_team_features_stay_on_pro(feature: str) -> None:
    """Shared / team / analysis are NOT part of the XL re-map."""
    assert get_required_plan_for_feature(feature) == PlanName.PRO
    assert has_feature("pro", feature) and has_feature("promax", feature)


@pytest.mark.parametrize("plan", PLAN_ORDER)
def test_every_tier_has_secret_store(plan: str) -> None:
    assert has_feature(plan, "secret_store")


@pytest.mark.parametrize("plan", PLAN_ORDER)
def test_resources_implies_public_contexts(plan: str) -> None:
    """``setup_resource`` inserts a *public* context, so any tier that may
    create resources must also be allowed to make contexts public."""
    if has_feature(plan, "resources"):
        assert has_feature(plan, "public_contexts"), plan


def test_serve_caps_for_existing_objects_are_unchanged() -> None:
    """Block-new-only: the numeric caps existing M/L objects rely on do not
    move to 0 — only the feature flag gates creation."""
    basic, pro, xl = get_plan_tier("basic"), get_plan_tier("pro"), get_plan_tier("promax")
    assert (basic.max_resource_tokens, basic.max_connectors) == (3, 3)
    assert (pro.max_resource_tokens, pro.max_connectors) == (30, 10)
    assert (pro.public_calls_per_day, pro.bound_public_calls_per_minute) == (1000, 100)
    # Final XL seat counts (#1548 shipped these as provisional).
    assert (xl.max_resource_tokens, xl.max_connectors) == (150, 50)


def test_feature_denied_message_names_the_registry_tier() -> None:
    """Refusal text derives the tier from the registry — never a hardcoded
    "Pro" — so a display-name override or a new tier flows through."""
    xl_display = get_plan_tier("promax").display_name
    msg = feature_denied_message("pro", "resources")
    assert "resources" in msg and "pro plan" in msg and xl_display in msg
    assert "Pro plan" not in msg
    # Legacy rows with plan_name NULL are read as free (member_credentials.py).
    assert "free plan" in feature_denied_message(None, "connectors")
    assert required_plan_display_name("public_contexts") == xl_display
    assert required_plan_display_name("shared_contexts") == get_plan_tier("pro").display_name
    # Unknown feature: fall back rather than 500 on a typo.
    assert required_plan_display_name("not-a-feature") == "higher"
