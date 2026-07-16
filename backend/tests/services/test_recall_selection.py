"""Truthful bootstrap-eval recall selection evidence (#1306)."""

from __future__ import annotations

import math

import pytest

from services.recall_selection import RecallSelectionConfig, plan_recall_selection

ELIGIBLE = tuple(f"00000000-0000-0000-0000-00000000000{i}" for i in range(1, 6))


def test_deterministic_policy_reports_truthful_zeroes_for_the_full_pool() -> None:
    plan = plan_recall_selection(
        ELIGIBLE,
        top_k=2,
        config=RecallSelectionConfig(seed=188, exploration_floor=0.0, candidate_pool_k=100),
    )

    assert plan.selected_ids == ELIGIBLE[:2]
    assert plan.selection_probabilities == {
        ELIGIBLE[0]: 1.0,
        ELIGIBLE[1]: 1.0,
        ELIGIBLE[2]: 0.0,
        ELIGIBLE[3]: 0.0,
        ELIGIBLE[4]: 0.0,
    }
    assert plan.policy["name"] == "deterministic_top_k_v1"
    assert plan.policy["minimum_selection_probability"] == 0.0


def test_exploration_floor_is_exact_positive_and_finite_for_every_candidate() -> None:
    plan = plan_recall_selection(
        ELIGIBLE,
        top_k=2,
        config=RecallSelectionConfig(seed=188, exploration_floor=0.1, candidate_pool_k=100),
    )

    probabilities = plan.selection_probabilities
    assert set(probabilities) == set(ELIGIBLE)
    assert all(math.isfinite(value) and 0.0 < value <= 1.0 for value in probabilities.values())
    assert probabilities[ELIGIBLE[0]] == pytest.approx(0.85)
    assert probabilities[ELIGIBLE[1]] == pytest.approx(0.85)
    assert probabilities[ELIGIBLE[2]] == pytest.approx(0.1)
    assert sum(probabilities.values()) == pytest.approx(2.0)
    assert plan.policy == {
        "name": "deterministic_uniform_mixture_v1",
        "version": 1,
        "evaluation_seed": 188,
        "replay_identity": "bootstrap-recall-v1:188",
        "exploration_floor": 0.1,
        "uniform_mixture_probability": 0.25,
        "candidate_pool_k": 100,
        "eligible_count": 5,
        "selected_count": 2,
        "minimum_selection_probability": 0.1,
    }


def test_same_seed_and_policy_replay_the_same_selection() -> None:
    config = RecallSelectionConfig(seed=90210, exploration_floor=0.2, candidate_pool_k=100)

    first = plan_recall_selection(ELIGIBLE, top_k=2, config=config)
    second = plan_recall_selection(ELIGIBLE, top_k=2, config=config)

    assert first == second


def test_impossible_floor_fails_instead_of_fabricating_propensity() -> None:
    with pytest.raises(ValueError, match="maximum feasible floor"):
        plan_recall_selection(
            ELIGIBLE,
            top_k=2,
            config=RecallSelectionConfig(seed=1, exploration_floor=0.5, candidate_pool_k=100),
        )


def test_floor_at_tolerance_boundary_keeps_probabilities_consistent() -> None:
    # The feasibility guard admits floors up to maximum_feasible_floor + 1e-12.
    # Inside that band the actual procedure is "always the uniform arm", whose
    # exact per-candidate marginal is selected_count/n — the evidence must
    # report THAT (the clamped effective floor), not the slightly-infeasible
    # requested value.
    boundary_floor = 2 / 5 + 5e-13  # max feasible for top_k=2, n=5, inside tolerance
    plan = plan_recall_selection(
        ELIGIBLE,
        top_k=2,
        config=RecallSelectionConfig(
            seed=7, exploration_floor=boundary_floor, candidate_pool_k=100
        ),
    )

    assert plan.policy["uniform_mixture_probability"] == 1.0
    assert plan.policy["exploration_floor"] == 2 / 5  # the effective (clamped) floor
    probabilities = plan.selection_probabilities
    # Always-uniform ⇒ every candidate's exact marginal is selected_count/n.
    assert all(value == 2 / 5 for value in probabilities.values())
    assert sum(probabilities.values()) == pytest.approx(2.0)


def test_evidence_contains_identities_but_no_candidate_content() -> None:
    plan = plan_recall_selection(
        ELIGIBLE,
        top_k=2,
        config=RecallSelectionConfig(seed=1, exploration_floor=0.1, candidate_pool_k=100),
    )

    serialized = repr(plan)
    assert set(plan.selection_probabilities) == set(ELIGIBLE)
    assert "summary" not in serialized
    assert "content" not in serialized
