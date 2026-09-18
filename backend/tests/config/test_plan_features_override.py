"""``PLAN_<KEY>_FEATURES`` — env override for a tier's feature set (#1559).

``_apply_settings_overrides`` runs at import time against the module-level
registry, so each test hands it a ``Settings`` built in-process and lets it
mutate *copies* of ``PLAN_TIERS`` / ``FEATURE_MIN_PLANS`` monkeypatched onto
the module (the gate helpers resolve those globals at call time).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import config.plan_tiers as plan_tiers
from config.plan_tiers import (
    PLAN_BASIC,
    PLAN_FREE,
    PLAN_ORDER,
    PLAN_PRO,
    PLAN_PROMAX,
    PlanName,
    feature_denied_message,
    get_plan_tier,
    get_required_plan_for_feature,
    has_feature,
    required_plan_display_name,
    required_plan_name,
)
from config.settings import Settings

_DEFAULT_TIERS = (PLAN_FREE, PLAN_BASIC, PLAN_PRO, PLAN_PROMAX)


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Isolate the module registry and stub the startup logger."""
    monkeypatch.setattr(plan_tiers, "PLAN_TIERS", dict(plan_tiers.PLAN_TIERS))
    monkeypatch.setattr(plan_tiers, "FEATURE_MIN_PLANS", dict(plan_tiers.FEATURE_MIN_PLANS))
    fake_logger = MagicMock()
    monkeypatch.setattr(plan_tiers, "logger", fake_logger)
    return fake_logger


def _apply(**overrides: str) -> None:
    plan_tiers._apply_settings_overrides(Settings(_env_file=None, **overrides))


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


def test_known_features_is_the_union_of_code_defaults_and_the_matrix() -> None:
    expected = frozenset(plan_tiers.FEATURE_MIN_PLANS).union(*(t.features for t in _DEFAULT_TIERS))
    assert plan_tiers.KNOWN_FEATURES == expected
    # ``secret_store`` is on every tier and so has no matrix row — the union
    # (not the matrix alone) is what makes it a legal override name.
    assert "secret_store" in plan_tiers.KNOWN_FEATURES
    assert {"resources", "connectors", "public_contexts"} <= plan_tiers.KNOWN_FEATURES


def test_default_registry_matches_the_hand_written_matrix() -> None:
    """The matrix is only *re*-derived under an override, so pin that the
    hand-written default agrees with the tiers it describes."""
    derived = plan_tiers._derive_feature_min_plans(plan_tiers.PLAN_TIERS)
    for feature, plan in plan_tiers.FEATURE_MIN_PLANS.items():
        assert derived[feature] == plan, feature
    assert derived["secret_store"] == PlanName.FREE


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_override_replaces_the_tier_features_whitespace_tolerant(registry: MagicMock) -> None:
    _apply(
        plan_basic_features=(
            " api_keys, oauth ,reranking,managed_embeddings , secret_store,"
            "resources, connectors,public_contexts ,"
        )
    )

    basic = get_plan_tier("basic")
    assert basic.features == frozenset(
        {
            "api_keys",
            "oauth",
            "reranking",
            "managed_embeddings",
            "secret_store",
            "resources",
            "connectors",
            "public_contexts",
        }
    )
    assert has_feature("basic", "resources")
    # Untouched tiers keep the code default; the numeric fields on the
    # overridden tier are untouched too.
    assert get_plan_tier("free").features == PLAN_FREE.features
    assert get_plan_tier("pro").features == PLAN_PRO.features
    assert basic.max_resource_tokens == PLAN_BASIC.max_resource_tokens


def test_blank_override_is_ignored(registry: MagicMock) -> None:
    before = dict(plan_tiers.FEATURE_MIN_PLANS)
    _apply(plan_free_features="   ")
    assert get_plan_tier("free") is PLAN_FREE
    assert plan_tiers.FEATURE_MIN_PLANS == before


def test_unknown_feature_names_are_rejected_at_import(registry: MagicMock) -> None:
    with pytest.raises(ValueError, match="PLAN_FREE_FEATURES") as exc_info:
        _apply(plan_free_features="api_keys,secret_store,warp_drive,teleport")
    message = str(exc_info.value)
    assert "teleport" in message and "warp_drive" in message
    # Nothing was applied: the registry is untouched on a rejected override.
    assert get_plan_tier("free") is PLAN_FREE


# ---------------------------------------------------------------------------
# Mirrored boolean: the shared-context gates read ``allows_shared_contexts``
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("plan", PLAN_ORDER)
def test_allows_shared_contexts_mirrors_the_feature_by_default(plan: str) -> None:
    tier = get_plan_tier(plan)
    assert tier.allows_shared_contexts is ("shared_contexts" in tier.features)


def test_override_keeps_allows_shared_contexts_in_lock_step(registry: MagicMock) -> None:
    _apply(
        plan_basic_features="api_keys,oauth,secret_store,shared_contexts",
        plan_pro_features="api_keys,oauth,secret_store",
    )
    assert get_plan_tier("basic").allows_shared_contexts is True
    assert get_plan_tier("pro").allows_shared_contexts is False


# ---------------------------------------------------------------------------
# FEATURE_MIN_PLANS follows the EFFECTIVE tiers
# ---------------------------------------------------------------------------


def test_feature_min_plans_is_recomputed_from_the_effective_tiers(registry: MagicMock) -> None:
    _apply(
        plan_basic_features=(
            "api_keys,oauth,reranking,managed_embeddings,secret_store,"
            "resources,connectors,public_contexts"
        )
    )

    for feature in ("resources", "connectors", "public_contexts"):
        assert get_required_plan_for_feature(feature) == PlanName.BASIC
    # Refusal text names the new minimum tier, not the code-default XL.
    msg = feature_denied_message("free", "resources")
    assert get_plan_tier("basic").display_name in msg
    assert get_plan_tier("promax").display_name not in msg
    # Features the override did not move keep their code-default minimum.
    assert get_required_plan_for_feature("shared_contexts") == PlanName.PRO
    assert get_required_plan_for_feature("api_keys") == PlanName.FREE


def test_removing_a_feature_moves_its_minimum_up(registry: MagicMock) -> None:
    _apply(plan_basic_features="api_keys,oauth,managed_embeddings,secret_store")
    assert get_required_plan_for_feature("reranking") == PlanName.PRO
    assert required_plan_display_name("reranking") == get_plan_tier("pro").display_name


def test_a_feature_on_no_tier_drops_out_of_the_matrix(registry: MagicMock) -> None:
    """Every tier loses ``managed_embeddings`` (a BYOK-only self-host): the
    matrix has no row for it, so the refusal text takes the "higher" fallback
    instead of naming a tier that does not have it either."""
    _apply(
        plan_free_features="api_keys,oauth,secret_store",
        plan_basic_features="api_keys,oauth,reranking,secret_store",
        plan_pro_features="api_keys,oauth,reranking,secret_store,shared_contexts",
        plan_promax_features="api_keys,oauth,reranking,secret_store,shared_contexts",
    )
    assert "managed_embeddings" not in plan_tiers.FEATURE_MIN_PLANS
    with pytest.raises(ValueError, match="Unknown feature"):
        get_required_plan_for_feature("managed_embeddings")
    # The envelope-building gates use the non-raising twin.
    assert required_plan_name("managed_embeddings") is None
    assert required_plan_display_name("managed_embeddings") == "higher"
    assert not any(has_feature(p, "managed_embeddings") for p in PLAN_ORDER)


# ---------------------------------------------------------------------------
# Registry invariants are enforced on the override
# ---------------------------------------------------------------------------


def test_override_without_secret_store_is_rejected(registry: MagicMock) -> None:
    with pytest.raises(ValueError, match="PLAN_PRO_FEATURES.*secret_store"):
        _apply(plan_pro_features="api_keys,oauth,shared_contexts")
    assert get_plan_tier("pro") is PLAN_PRO


def test_resources_without_public_contexts_is_rejected(registry: MagicMock) -> None:
    """``setup_resource`` inserts a *public* context, so ``resources`` on a
    tier that may not make contexts public would create objects it cannot
    then serve as intended."""
    with pytest.raises(ValueError, match="PLAN_BASIC_FEATURES.*public_contexts"):
        _apply(plan_basic_features="api_keys,oauth,secret_store,resources")
    assert get_plan_tier("basic") is PLAN_BASIC


def test_default_registry_satisfies_the_invariants_the_override_enforces() -> None:
    for plan in PLAN_ORDER:
        tier = get_plan_tier(plan)
        assert "secret_store" in tier.features, plan
        if "resources" in tier.features:
            assert "public_contexts" in tier.features, plan


# ---------------------------------------------------------------------------
# Startup log
# ---------------------------------------------------------------------------


def test_effective_feature_set_is_logged_once_per_tier(registry: MagicMock) -> None:
    _apply(plan_basic_features="api_keys,oauth,secret_store,resources,public_contexts")

    registry.info.assert_called_once()
    event, kwargs = registry.info.call_args.args[0], registry.info.call_args.kwargs
    assert event == "plan_tier_features_effective"
    assert kwargs["overridden"] == ["basic"]
    assert kwargs["basic"] == sorted(get_plan_tier("basic").features)
    assert kwargs["promax"] == sorted(PLAN_PROMAX.features)
    assert set(PLAN_ORDER) <= set(kwargs)


def test_no_override_still_logs_the_effective_set(registry: MagicMock) -> None:
    _apply()
    registry.info.assert_called_once()
    assert registry.info.call_args.kwargs["overridden"] == []
