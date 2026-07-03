"""Confirmatory statistics for the Day-4 pre-registered retrieval evaluation.

Pure Python 3.11 stdlib only (``statistics.NormalDist``, ``random.Random``,
``math`` — no numpy/scipy, which are not dependencies of the backend test
environment). This is the ONLY statistics implementation for the prereg;
downstream analysis (Task 3) imports exactly these five functions.

Estimands, per the prereg:

- ``paired_bca_ci``: BCa (bias-corrected and accelerated) bootstrap CI for the
  mean of paired per-query differences — the primary interval for hypothesis
  contrasts (>= 10,000 resamples per the prereg).
- ``permutation_omnibus``: a within-query (paired) permutation test of the
  null that k arms share the same mean — gates the family before per-pair
  contrasts are interpreted.
- ``unpaired_percentile_ci_diff``: percentile-method CI for a difference of
  means between two INDEPENDENT groups (not paired by query) — used where
  pairing isn't available.
- ``sigma_d`` / ``achieved_power``: sample sd of paired differences and the
  closed-form two-sided normal-approximation power, for the prereg's power
  re-estimation.

All resampling is seed-explicit (``random.Random(seed)``) for reproducibility;
every function documents its exact draw order so results are byte-for-byte
reproducible across runs and machines.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from statistics import NormalDist
from statistics import stdev as _stdev


def _nearest_rank_idx(p: float, b: int) -> int:
    """Nearest-rank index into a length-``b`` sorted sample for quantile ``p``.

    ``idx = min(b-1, max(0, ceil(p*b) - 1))`` — shared by the BCa and the
    plain percentile method so both index the same way into their sorted
    bootstrap distributions.
    """
    return min(b - 1, max(0, math.ceil(p * b) - 1))


def paired_bca_ci(
    diffs: Sequence[float],
    *,
    n_resamples: int = 10_000,
    alpha: float = 0.05,
    seed: int,
) -> dict[str, float]:
    """BCa bootstrap CI for the mean of paired per-query differences.

    Statistic is the sample mean of ``diffs``. Bootstrap resamples ``diffs``
    with replacement ``n_resamples`` times using ``random.Random(seed)``
    (``rng.randrange(n)`` per draw, ``n`` draws per resample, in that order —
    this exact sequence is part of the contract so a caller replaying the same
    seed/n/n_resamples can reproduce the underlying bootstrap distribution).

    Bias correction ``z0`` uses the midpoint tie rule
    ``(#{boot < observed} + 0.5*#{boot == observed}) / B``, with the
    proportion clamped into the open interval ``(1/(B+1), B/(B+1))`` before
    ``inv_cdf`` so ``z0`` is always finite. Acceleration ``a`` is the jackknife
    estimate over leave-one-out means (0 if the denominator vanishes — e.g. a
    3rd-moment-symmetric sample). Adjusted percentiles are computed per the
    standard BCa formula and mapped to order statistics of the sorted
    bootstrap distribution via ``_nearest_rank_idx``.

    Degenerate input (every diff equal) short-circuits to that constant for
    mean/ci_low/ci_high before any bootstrapping — avoids a zero jackknife
    denominator and a meaningless CI around a fixed point.

    Raises ``ValueError`` if ``diffs`` is empty.
    """
    if not diffs:
        raise ValueError("paired_bca_ci: diffs is empty — nothing to bootstrap")

    n = len(diffs)
    observed = sum(diffs) / n

    first = diffs[0]
    if all(d == first for d in diffs):
        # Also covers n == 1 (a single value is trivially "all equal"), which
        # would otherwise make the n-1 jackknife denominator below zero.
        return {
            "mean": float(observed),
            "ci_low": float(observed),
            "ci_high": float(observed),
            "n": n,
        }

    rng = random.Random(seed)
    boot_means: list[float] = []
    for _ in range(n_resamples):
        resample_sum = 0.0
        for _ in range(n):
            resample_sum += diffs[rng.randrange(n)]
        boot_means.append(resample_sum / n)

    dist = NormalDist()
    b = n_resamples
    n_less = sum(1 for m in boot_means if m < observed)
    n_equal = sum(1 for m in boot_means if m == observed)
    proportion = (n_less + 0.5 * n_equal) / b
    proportion = min(max(proportion, 1 / (b + 1)), b / (b + 1))
    z0 = dist.inv_cdf(proportion)

    # Jackknife acceleration over leave-one-out means.
    total = sum(diffs)
    thetas = [(total - d) / (n - 1) for d in diffs]
    theta_bar = sum(thetas) / n
    numerator = sum((theta_bar - t) ** 3 for t in thetas)
    denominator = 6 * (sum((theta_bar - t) ** 2 for t in thetas)) ** 1.5
    a = numerator / denominator if denominator != 0 else 0.0

    z_alpha_2 = dist.inv_cdf(alpha / 2)
    z_1_alpha_2 = dist.inv_cdf(1 - alpha / 2)

    def _adjusted_percentile(z: float) -> float:
        s = z0 + z
        arg = z0 + s / (1 - a * s)
        return min(max(dist.cdf(arg), 0.0), 1.0)

    alpha1 = _adjusted_percentile(z_alpha_2)
    alpha2 = _adjusted_percentile(z_1_alpha_2)

    sorted_boot = sorted(boot_means)
    idx1 = _nearest_rank_idx(alpha1, b)
    idx2 = _nearest_rank_idx(alpha2, b)

    return {
        "mean": observed,
        "ci_low": sorted_boot[idx1],
        "ci_high": sorted_boot[idx2],
        "n": n,
    }


def permutation_omnibus(
    arm_values: dict[str, Sequence[float]],
    *,
    n_permutations: int = 10_000,
    seed: int,
) -> dict[str, float]:
    """Within-query (paired) permutation test that k arms share equal means.

    Observed statistic ``T = sum_arms (mean_arm - grand_mean)**2`` where
    ``grand_mean`` is the mean of the arm means. Each of ``n_permutations``
    iterations independently shuffles every query's k-tuple of arm values
    (``random.Random(seed)``, one ``rng.shuffle`` call per query per
    iteration, queries visited in ``arm_values``'s common order) and
    recomputes T; ``p_value = (1 + #{T_perm >= T_obs}) / (1 + n_permutations)``
    (add-one smoothing — never exactly 0, so a "highly significant" result
    still reads as a probability, not a false certainty).

    Raises ``ValueError`` if fewer than 2 arms, if arms have unequal lengths
    (naming the offending arm), or if there are 0 queries.
    """
    arm_names = list(arm_values.keys())
    if len(arm_names) < 2:
        raise ValueError(f"permutation_omnibus needs >= 2 arms, got {len(arm_names)}")

    n = len(arm_values[arm_names[0]])
    for name in arm_names:
        length = len(arm_values[name])
        if length != n:
            raise ValueError(
                f"permutation_omnibus: arm {name!r} has {length} queries, expected "
                f"{n} (all arms must have equal length)"
            )
    if n == 0:
        raise ValueError("permutation_omnibus: 0 queries — nothing to test")

    k = len(arm_names)
    rows = [[arm_values[name][i] for name in arm_names] for i in range(n)]

    def _stat(data_rows: list[list[float]]) -> float:
        arm_means = [sum(row[j] for row in data_rows) / n for j in range(k)]
        grand_mean = sum(arm_means) / k
        return sum((m - grand_mean) ** 2 for m in arm_means)

    t_obs = _stat(rows)

    rng = random.Random(seed)
    count = 0
    for _ in range(n_permutations):
        permuted_rows: list[list[float]] = []
        for row in rows:
            shuffled = list(row)
            rng.shuffle(shuffled)
            permuted_rows.append(shuffled)
        if _stat(permuted_rows) >= t_obs:
            count += 1

    p_value = (1 + count) / (1 + n_permutations)
    return {"stat": t_obs, "p_value": p_value, "n_queries": n, "n_arms": k}


def unpaired_percentile_ci_diff(
    a: Sequence[float],
    b: Sequence[float],
    *,
    n_resamples: int = 10_000,
    alpha: float = 0.05,
    seed: int,
) -> dict[str, float]:
    """Percentile-method bootstrap CI for ``mean(a) - mean(b)``.

    ``a`` and ``b`` are resampled independently (their own size, with
    replacement) ``n_resamples`` times using a single ``random.Random(seed)``
    — ``a`` drawn before ``b`` within each iteration — and the CI is the
    ``alpha/2`` / ``1 - alpha/2`` order statistics of the resampled
    differences, via the same nearest-rank rule as ``paired_bca_ci``.

    Raises ``ValueError`` if either group is empty.
    """
    if not a:
        raise ValueError("unpaired_percentile_ci_diff: group 'a' is empty")
    if not b:
        raise ValueError("unpaired_percentile_ci_diff: group 'b' is empty")

    na = len(a)
    nb = len(b)
    mean_diff = sum(a) / na - sum(b) / nb

    rng = random.Random(seed)
    boot_diffs: list[float] = []
    for _ in range(n_resamples):
        sum_a = sum(a[rng.randrange(na)] for _ in range(na))
        sum_b = sum(b[rng.randrange(nb)] for _ in range(nb))
        boot_diffs.append(sum_a / na - sum_b / nb)
    boot_diffs.sort()

    idx_lo = _nearest_rank_idx(alpha / 2, n_resamples)
    idx_hi = _nearest_rank_idx(1 - alpha / 2, n_resamples)

    return {
        "mean": mean_diff,
        "ci_low": boot_diffs[idx_lo],
        "ci_high": boot_diffs[idx_hi],
        "n_a": na,
        "n_b": nb,
    }


def sigma_d(diffs: Sequence[float]) -> float:
    """Sample standard deviation of paired differences (n-1 denominator).

    Raises ``ValueError`` if fewer than 2 values (``statistics.stdev``'s own
    floor, surfaced here with an actionable message for the prereg context).
    """
    if len(diffs) < 2:
        raise ValueError(f"sigma_d needs >= 2 values, got {len(diffs)}")
    return _stdev(diffs)


def achieved_power(delta: float, sd: float, n: int, *, alpha: float = 0.05) -> float:
    """Two-sided normal-approximation power for a paired mean-difference test.

    ``Phi(delta*sqrt(n)/sd - z_{1-alpha/2})``. When ``sd == 0`` every
    resample is a perfect detection of a nonzero ``delta`` (power 1.0) or a
    trivially unfalsifiable null (power 0.0) — returned directly rather than
    dividing by zero.

    Raises ``ValueError`` if ``n < 1`` or ``sd < 0``.
    """
    if n < 1:
        raise ValueError(f"achieved_power needs n >= 1, got {n}")
    if sd < 0:
        raise ValueError(f"achieved_power needs sd >= 0, got {sd}")
    if sd == 0:
        return 1.0 if delta > 0 else 0.0

    dist = NormalDist()
    z_crit = dist.inv_cdf(1 - alpha / 2)
    return dist.cdf(delta * math.sqrt(n) / sd - z_crit)
