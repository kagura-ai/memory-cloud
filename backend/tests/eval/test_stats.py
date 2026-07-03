"""Unit tests for ``tests.eval.stats`` — the Day-4 confirmatory statistics.

Pure functions, no infra — these pin the exact resampling / bias-correction
math so a regression in the confirmatory analysis (paired BCa bootstrap,
permutation omnibus, power) is caught independently of the live harness and
of the Day-4 analysis script (Task 3) that imports these names.

Written before ``tests/eval/stats.py`` existed (TDD RED step).
"""

from __future__ import annotations

import random
import statistics

import pytest

from tests.eval.stats import (
    achieved_power,
    paired_bca_ci,
    permutation_omnibus,
    sigma_d,
    unpaired_percentile_ci_diff,
)


def _iid_arms(
    n_queries: int, k: int, *, shift: dict[int, float] | None = None
) -> dict[str, list[float]]:
    """``k`` arms that are near-identical copies of a shared per-query signal.

    Each arm is ``base + shift.get(arm_idx, 0) + tiny_independent_noise``, so
    with no shift the arms are (up to noise) the same distribution — the null
    permutation_omnibus is built for — and a shift on one arm index is a
    clean, large, arm-level effect.
    """
    shift = shift or {}
    base = [random.Random(555).gauss(0.0, 1.0) for _ in range(n_queries)]
    arms: dict[str, list[float]] = {}
    for a in range(k):
        noise_rng = random.Random(1000 + a)
        arms[f"arm{a}"] = [b + shift.get(a, 0.0) + noise_rng.gauss(0.0, 0.01) for b in base]
    return arms


class TestPairedBcaCiDeterminism:
    def test_repeat_call_same_seed_identical(self):
        diffs = [random.Random(11).gauss(0.2, 0.1) for _ in range(50)]
        r1 = paired_bca_ci(diffs, seed=42, n_resamples=500)
        r2 = paired_bca_ci(diffs, seed=42, n_resamples=500)
        assert r1 == r2


class TestPairedBcaCiShape:
    def test_within_tolerance_of_percentile_and_contains_mean(self):
        diffs = [random.Random(7).gauss(0.1, 0.05) for _ in range(200)]
        n_resamples = 10_000
        seed = 123

        bca = paired_bca_ci(diffs, seed=seed, n_resamples=n_resamples)

        # Plain percentile CI computed inline, using the IDENTICAL resampling
        # sequence (same seed/n/n_resamples as paired_bca_ci uses internally)
        # so this isolates the BCa bias/acceleration adjustment, not
        # resampling noise between two independent bootstrap runs.
        rng = random.Random(seed)
        n = len(diffs)
        boot_means = []
        for _ in range(n_resamples):
            resample = [diffs[rng.randrange(n)] for _ in range(n)]
            boot_means.append(sum(resample) / n)
        boot_means.sort()
        lo = boot_means[int(0.025 * n_resamples)]
        hi = boot_means[int(0.975 * n_resamples)]

        assert bca["ci_low"] == pytest.approx(lo, abs=0.01)
        assert bca["ci_high"] == pytest.approx(hi, abs=0.01)
        assert bca["ci_low"] <= bca["mean"] <= bca["ci_high"]


class TestPairedBcaCiLocationShift:
    def test_shift_moves_mean_and_bounds_by_exact_constant(self):
        diffs = [random.Random(3).gauss(0.0, 0.2) for _ in range(80)]
        shifted = [d + 0.5 for d in diffs]

        base = paired_bca_ci(diffs, seed=99, n_resamples=2_000)
        shifted_r = paired_bca_ci(shifted, seed=99, n_resamples=2_000)

        # Same seed => identical resample indices for both calls, so adding a
        # constant to every diff shifts the whole bootstrap distribution (and
        # z0/a, which depend only on shape) by exactly that constant.
        assert shifted_r["mean"] == pytest.approx(base["mean"] + 0.5, abs=1e-12)
        assert shifted_r["ci_low"] == pytest.approx(base["ci_low"] + 0.5, abs=1e-12)
        assert shifted_r["ci_high"] == pytest.approx(base["ci_high"] + 0.5, abs=1e-12)


class TestPairedBcaCiDegenerate:
    def test_all_equal_diffs_collapse_to_constant(self):
        diffs = [0.37] * 30
        result = paired_bca_ci(diffs, seed=1, n_resamples=500)
        assert result["mean"] == 0.37
        assert result["ci_low"] == 0.37
        assert result["ci_high"] == 0.37
        assert result["n"] == 30


class TestPairedBcaCiErrors:
    def test_empty_diffs_raises(self):
        with pytest.raises(ValueError):
            paired_bca_ci([], seed=1)


class TestPermutationOmnibusDeterminism:
    def test_repeat_call_same_seed_identical(self):
        arms = _iid_arms(30, 3)
        r1 = permutation_omnibus(arms, seed=5, n_permutations=500)
        r2 = permutation_omnibus(arms, seed=5, n_permutations=500)
        assert r1 == r2


class TestPermutationOmnibus:
    def test_identical_distribution_arms_high_p(self):
        arms = _iid_arms(60, 4)
        result = permutation_omnibus(arms, seed=1, n_permutations=2_000)
        assert result["p_value"] > 0.05
        assert result["n_arms"] == 4
        assert result["n_queries"] == 60

    def test_one_arm_shifted_low_p(self):
        arms = _iid_arms(60, 4, shift={3: 5.0})
        result = permutation_omnibus(arms, seed=1, n_permutations=2_000)
        assert result["p_value"] < 0.001

    def test_two_arm_direction_agrees_with_bca_ci(self):
        arms = _iid_arms(60, 2, shift={1: 2.0})
        result = permutation_omnibus(arms, seed=1, n_permutations=2_000)
        assert result["p_value"] < 0.001

        diffs = [b - a for a, b in zip(arms["arm0"], arms["arm1"], strict=True)]
        ci = paired_bca_ci(diffs, seed=1, n_resamples=2_000)
        assert ci["ci_low"] > 0  # CI excludes 0, consistent with the significant omnibus


class TestPermutationOmnibusErrors:
    def test_mismatched_lengths_names_arm(self):
        with pytest.raises(ValueError, match="bad_arm"):
            permutation_omnibus({"good": [1.0, 2.0], "bad_arm": [1.0, 2.0, 3.0]}, seed=1)

    def test_fewer_than_two_arms_raises(self):
        with pytest.raises(ValueError):
            permutation_omnibus({"only": [1.0, 2.0]}, seed=1)

    def test_zero_queries_raises(self):
        with pytest.raises(ValueError):
            permutation_omnibus({"a": [], "b": []}, seed=1)


class TestUnpairedPercentileCiDiff:
    def test_determinism(self):
        a = [random.Random(1).gauss(0, 1) for _ in range(40)]
        b = [random.Random(2).gauss(0.3, 1) for _ in range(40)]
        r1 = unpaired_percentile_ci_diff(a, b, seed=7, n_resamples=500)
        r2 = unpaired_percentile_ci_diff(a, b, seed=7, n_resamples=500)
        assert r1 == r2

    def test_mean_and_counts(self):
        a = [1.0, 2.0, 3.0]
        b = [10.0, 20.0]
        result = unpaired_percentile_ci_diff(a, b, seed=3, n_resamples=500)
        assert result["mean"] == pytest.approx((2.0) - (15.0))
        assert result["n_a"] == 3
        assert result["n_b"] == 2
        assert result["ci_low"] <= result["mean"] <= result["ci_high"]

    def test_empty_group_raises(self):
        with pytest.raises(ValueError):
            unpaired_percentile_ci_diff([], [1.0], seed=1)
        with pytest.raises(ValueError):
            unpaired_percentile_ci_diff([1.0], [], seed=1)


class TestSigmaD:
    def test_matches_statistics_stdev(self):
        diffs = [1.0, 2.0, 3.0, 4.0]
        assert sigma_d(diffs) == statistics.stdev(diffs)

    def test_single_value_raises(self):
        with pytest.raises(ValueError):
            sigma_d([1.0])


class TestAchievedPower:
    def test_prereg_anchor_n200(self):
        # prereg §5 anchor: delta=0.05, sd=0.25, n=200 -> ~0.80 power.
        assert achieved_power(0.05, 0.25, 200) == pytest.approx(0.80, abs=0.02)

    def test_prereg_anchor_n50(self):
        assert achieved_power(0.05, 0.25, 50) == pytest.approx(0.30, abs=0.05)

    def test_zero_sd_positive_delta_is_full_power(self):
        assert achieved_power(0.1, 0.0, 100) == 1.0

    def test_zero_sd_nonpositive_delta_is_zero_power(self):
        assert achieved_power(0.0, 0.0, 100) == 0.0
        assert achieved_power(-0.1, 0.0, 100) == 0.0

    def test_n_less_than_one_raises(self):
        with pytest.raises(ValueError):
            achieved_power(0.1, 0.2, 0)

    def test_negative_sd_raises(self):
        with pytest.raises(ValueError):
            achieved_power(0.1, -0.2, 100)
