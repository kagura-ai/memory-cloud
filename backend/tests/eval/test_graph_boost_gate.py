"""Unit tests for the #1213 graph-boost placebo gate (deterministic)."""

import pytest

from tests.eval.graph_boost_gate import (
    GATE_DENSITY_ARTIFACT,
    GATE_NO_EFFECT,
    GATE_REGRESSION,
    GATE_SHIP,
    GATE_UNDERPOWERED,
    NON_INFERIORITY_EPSILON,
    evaluate_gate,
    per_query_deltas,
)

# 60 paired companion queries (>= MIN_PROBES); gold is always {"g"}. A "hit" ranking has the
# gold inside the top-5, a "miss" ranking does not.
_HIT = (["g", "x1", "x2", "x3", "x4"], {"g"})
_MISS = (["x1", "x2", "x3", "x4", "x5"], {"g"})


def _arm(hits: int, total: int = 60) -> list[tuple[list[str], set[str]]]:
    return [_HIT] * hits + [_MISS] * (total - hits)


def _flat(hits: int, total: int = 40) -> list[tuple[list[str], set[str]]]:
    return [_HIT] * hits + [_MISS] * (total - hits)


class TestPerQueryDeltas:
    def test_paired_shape(self) -> None:
        deltas = per_query_deltas(_arm(60), _arm(0), 5)
        assert len(deltas) == 60
        assert all(d == pytest.approx(0.2) for d in deltas)

    def test_length_mismatch_raises(self) -> None:
        with pytest.raises(ValueError, match="differ in length"):
            per_query_deltas(_arm(5, total=10), _arm(5, total=11), 5)


class TestVerdicts:
    def test_ship_when_beats_both_and_non_inferior(self) -> None:
        result = evaluate_gate(
            boosted_real=_arm(54),
            unboosted=_arm(18),
            boosted_rewired_arms={42: _arm(18)},
            nongraph_boosted=_flat(30),
            nongraph_unboosted=_flat(30),
            seed=42,
        )
        assert result["contracts"] == {
            "powered": True,
            "min_probes": 50,
            "beats_unboosted": True,
            "beats_placebo": True,
            "non_inferior": True,
        }
        assert result["verdict"] == GATE_SHIP
        assert result["ships"] is True

    def test_density_artifact_blocks_ship(self) -> None:
        """Beats no-graph but NOT the rewired placebo — the §6 kill-shot:
        the boost is riding graph density, not edge-specific structure."""
        result = evaluate_gate(
            boosted_real=_arm(54),
            unboosted=_arm(18),
            boosted_rewired_arms={42: _arm(54)},
            nongraph_boosted=_flat(30),
            nongraph_unboosted=_flat(30),
            seed=42,
        )
        assert result["contracts"]["beats_unboosted"] is True
        assert result["contracts"]["beats_placebo"] is False
        assert result["verdict"] == GATE_DENSITY_ARTIFACT
        assert result["ships"] is False

    def test_no_effect_is_a_valid_close(self) -> None:
        result = evaluate_gate(
            boosted_real=_arm(30),
            unboosted=_arm(30),
            boosted_rewired_arms={42: _arm(30)},
            nongraph_boosted=_flat(30),
            nongraph_unboosted=_flat(30),
            seed=42,
        )
        assert result["verdict"] == GATE_NO_EFFECT
        assert result["ships"] is False

    def test_nongraph_regression_vetoes_even_a_winner(self) -> None:
        """The fusion-dilution lesson: a boost that wins its own bucket but
        regresses held-out non-graph P@5 beyond epsilon does not ship."""
        result = evaluate_gate(
            boosted_real=_arm(54),
            unboosted=_arm(18),
            boosted_rewired_arms={42: _arm(18)},
            nongraph_boosted=_flat(20),
            nongraph_unboosted=_flat(30),
            seed=42,
        )
        assert result["contracts"]["beats_unboosted"] is True
        assert result["contracts"]["beats_placebo"] is True
        assert result["nongraph"]["non_inferior"] is False
        assert result["verdict"] == GATE_REGRESSION
        assert result["ships"] is False

    def test_epsilon_tolerates_tiny_nongraph_dip(self) -> None:
        """A dip within the pre-declared epsilon must not veto."""
        # 40 queries at P@5=0.2/hit: one flipped hit moves the mean by 0.005,
        # inside epsilon=0.01.
        result = evaluate_gate(
            boosted_real=_arm(54),
            unboosted=_arm(18),
            boosted_rewired_arms={42: _arm(18)},
            nongraph_boosted=_flat(29),
            nongraph_unboosted=_flat(30),
            seed=42,
        )
        assert abs(result["nongraph"]["delta"]) <= NON_INFERIORITY_EPSILON
        assert result["verdict"] == GATE_SHIP


class TestDeterminism:
    def test_same_seed_same_result(self) -> None:
        kwargs = {
            "boosted_real": _arm(45),
            "unboosted": _arm(24),
            "boosted_rewired_arms": {42: _arm(27)},
            "nongraph_boosted": _flat(30),
            "nongraph_unboosted": _flat(30),
        }
        assert evaluate_gate(seed=7, **kwargs) == evaluate_gate(seed=7, **kwargs)


class TestPowerAndMultiSeed:
    def test_underpowered_probe_set_refuses_ship(self) -> None:
        """Gate2/CAIO: n < MIN_PROBES reports deltas but never renders ship."""
        result = evaluate_gate(
            boosted_real=_arm(18, total=20),
            unboosted=_arm(6, total=20),
            boosted_rewired_arms={42: _arm(6, total=20)},
            nongraph_boosted=_flat(30),
            nongraph_unboosted=_flat(30),
            seed=42,
        )
        assert result["contracts"]["powered"] is False
        assert result["verdict"] == GATE_UNDERPOWERED
        assert result["ships"] is False

    def test_placebo_must_hold_against_every_rewiring(self) -> None:
        """One rewiring the boost fails to beat kills the ship verdict."""
        result = evaluate_gate(
            boosted_real=_arm(54),
            unboosted=_arm(18),
            boosted_rewired_arms={42: _arm(18), 43: _arm(18), 44: _arm(54)},
            nongraph_boosted=_flat(30),
            nongraph_unboosted=_flat(30),
            seed=42,
        )
        assert result["contracts"]["beats_placebo"] is False
        assert result["verdict"] == GATE_DENSITY_ARTIFACT

    def test_empty_rewired_arms_raises(self) -> None:
        with pytest.raises(ValueError, match="at least one rewiring"):
            evaluate_gate(
                boosted_real=_arm(54),
                unboosted=_arm(18),
                boosted_rewired_arms={},
                nongraph_boosted=_flat(30),
                nongraph_unboosted=_flat(30),
                seed=42,
            )
