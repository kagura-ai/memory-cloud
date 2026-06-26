"""Unit tests for the #1069 reinforce ON-vs-OFF eval gate (pure logic).

DB-free: pins the two-population metric blocks, the zero-adoption surfacing
(popularity-bias) rate, and the decision gate that the staged rollout is gated
on. The live ingest/seed/score orchestration is tested separately with fakes in
``test_reinforce_runner.py``.
"""

from __future__ import annotations

from tests.eval.reinforce_gate import (
    POPULATION_CURRENT_FACT,
    POPULATION_RARE,
    ArmBlock,
    GateThresholds,
    evaluate_reinforce_gate,
    population_metrics,
    zero_adoption_surfacing_rate,
)


class TestPopulationMetrics:
    def test_perfect_ranking_scores_one(self):
        rankings = [(["a", "b"], {"a"}), (["c"], {"c"})]
        m = population_metrics(rankings)
        assert m["n"] == 2
        assert m["mrr@10"] == 1.0
        assert m["ndcg@10"] == 1.0

    def test_empty_is_zero_not_error(self):
        m = population_metrics([])
        assert m["n"] == 0
        assert m["mrr@10"] == 0.0


class TestZeroAdoptionSurfacingRate:
    def test_all_adopted_surfaced_is_zero(self):
        # Every surfaced doc is adopted → zero-adoption surfacing rate 0.
        rankings = [(["a", "b"], {"a"}), (["a"], {"a"})]
        assert zero_adoption_surfacing_rate(rankings, adopted_ids={"a", "b"}, k=10) == 0.0

    def test_counts_only_non_adopted_within_topk(self):
        # top-2 of [z, a, w]: z (new) + a (adopted) → 1 of 2 slots is zero-adoption.
        rankings = [(["z", "a", "w"], {"z"})]
        assert zero_adoption_surfacing_rate(rankings, adopted_ids={"a"}, k=2) == 0.5

    def test_empty_is_safe(self):
        assert zero_adoption_surfacing_rate([], adopted_ids=set(), k=10) == 0.0

    def test_realized_slots_not_diluted_by_empty_positions(self):
        # Only one doc returned but k=10 — rate is over the 1 realized slot, not 10.
        rankings = [(["z"], {"z"})]
        assert zero_adoption_surfacing_rate(rankings, adopted_ids=set(), k=10) == 1.0


def _arm(cf: float, rare: float, za: float) -> ArmBlock:
    return ArmBlock(
        populations={
            POPULATION_CURRENT_FACT: {"n": 5, "p@5": cf, "mrr@10": cf, "ndcg@10": cf},
            POPULATION_RARE: {"n": 5, "p@5": rare, "mrr@10": rare, "ndcg@10": rare},
        },
        zero_adoption_surfacing_rate=za,
    )


class TestEvaluateReinforceGate:
    def test_passes_when_current_fact_improves_and_rare_holds(self):
        off = _arm(cf=0.70, rare=0.80, za=0.50)
        on = _arm(cf=0.85, rare=0.80, za=0.49)  # cf up, rare flat, za retained
        v = evaluate_reinforce_gate(off, on)
        assert v["passed"] is True
        assert v["reasons"] == []
        assert v["current_fact"]["uplift"] == 0.15
        assert v["current_fact"]["improved"] is True

    def test_fails_when_current_fact_regresses(self):
        off = _arm(cf=0.80, rare=0.80, za=0.50)
        on = _arm(cf=0.70, rare=0.80, za=0.50)  # cf DOWN
        v = evaluate_reinforce_gate(off, on)
        assert v["passed"] is False
        assert any("current-fact" in r for r in v["reasons"])
        assert v["current_fact"]["improved"] is False

    def test_fails_when_rare_regresses_beyond_tolerance(self):
        off = _arm(cf=0.70, rare=0.80, za=0.50)
        on = _arm(cf=0.85, rare=0.70, za=0.50)  # rare drops 0.10 > 0.02 band
        v = evaluate_reinforce_gate(off, on)
        assert v["passed"] is False
        assert any("rare" in r for r in v["reasons"])

    def test_small_rare_dip_within_tolerance_passes(self):
        off = _arm(cf=0.70, rare=0.80, za=0.50)
        on = _arm(cf=0.85, rare=0.79, za=0.50)  # rare drops only 0.01 <= 0.02
        v = evaluate_reinforce_gate(off, on)
        assert v["passed"] is True

    def test_fails_when_zero_adoption_surfacing_collapses(self):
        off = _arm(cf=0.70, rare=0.80, za=0.50)
        on = _arm(cf=0.85, rare=0.80, za=0.30)  # retention 0.6 < 0.9
        v = evaluate_reinforce_gate(off, on)
        assert v["passed"] is False
        assert any("retention" in r for r in v["reasons"])
        assert v["zero_adoption_surfacing"]["retention"] == 0.6

    def test_retention_none_when_off_surfaced_no_zero_adoption(self):
        # OFF already surfaced no zero-adoption docs → nothing to retain, no fail.
        off = _arm(cf=0.70, rare=0.80, za=0.0)
        on = _arm(cf=0.85, rare=0.80, za=0.0)
        v = evaluate_reinforce_gate(off, on)
        assert v["zero_adoption_surfacing"]["retention"] is None
        assert v["passed"] is True

    def test_thresholds_are_configurable_for_stronger_claim(self):
        # Require a real +0.05 uplift; a flat current-fact now fails.
        off = _arm(cf=0.80, rare=0.80, za=0.50)
        on = _arm(cf=0.80, rare=0.80, za=0.50)
        strict = GateThresholds(min_current_fact_uplift=0.05)
        v = evaluate_reinforce_gate(off, on, thresholds=strict)
        assert v["passed"] is False
        assert any("uplift" in r for r in v["reasons"])
