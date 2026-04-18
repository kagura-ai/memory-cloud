"""Unit tests for PSI computation (issue #343)."""

from __future__ import annotations

import math

import pytest

from services.bm25_drift.psi_calculator import (
    MIN_DF,
    MIN_MEMORY_POINTS,
    MIN_SURVIVING_TERMS,
    NUM_BINS,
    PSI_MINOR_THRESHOLD,
    PSI_SIGNIFICANT_THRESHOLD,
    classify_status,
    compute_psi,
)


class TestClassifyStatus:
    def test_below_minor_is_stable(self) -> None:
        assert classify_status(0.0) == "stable"
        assert classify_status(PSI_MINOR_THRESHOLD - 1e-6) == "stable"

    def test_minor_band(self) -> None:
        assert classify_status(PSI_MINOR_THRESHOLD) == "minor_drift"
        assert classify_status(PSI_SIGNIFICANT_THRESHOLD - 1e-6) == "minor_drift"

    def test_significant(self) -> None:
        assert classify_status(PSI_SIGNIFICANT_THRESHOLD) == "significant_drift"
        assert classify_status(1.0) == "significant_drift"


def _identical_term_universe(num_terms: int, df_value: int) -> dict[int, int]:
    """Build a {hash: df} dict where every term has the same df."""
    return dict.fromkeys(range(num_terms), df_value)


class TestMinNGates:
    def test_below_min_memory_returns_insufficient(self) -> None:
        df = _identical_term_universe(MIN_SURVIVING_TERMS + 10, MIN_DF + 5)
        result = compute_psi(
            df_memory=df,
            df_global=df,
            m_memory=MIN_MEMORY_POINTS - 1,  # one short
            n_global=MIN_MEMORY_POINTS - 1,
        )
        assert result.status == "insufficient_data"
        assert result.psi is None
        assert result.top_divergent_terms == []

    def test_below_min_terms_returns_insufficient(self) -> None:
        # Only 5 unique terms — well below MIN_SURVIVING_TERMS.
        df_memory = _identical_term_universe(5, MIN_DF + 5)
        df_global = dict(df_memory)
        result = compute_psi(
            df_memory=df_memory,
            df_global=df_global,
            m_memory=MIN_MEMORY_POINTS + 50,
            n_global=MIN_MEMORY_POINTS + 50,
        )
        assert result.status == "insufficient_data"
        assert result.psi is None
        # Still report num_terms for diagnostics — confirmed by Stats PhD
        # spec: status='insufficient_data' must remain distinguishable from
        # absent measurement, so the surviving-term count helps the
        # operator reason about why the gate fired.
        assert result.num_terms == 5

    def test_cochran_rule_drops_low_df(self) -> None:
        # Terms with df < MIN_DF are dropped from BOTH distributions.
        big = dict.fromkeys(range(100), MIN_DF + 5)
        small = dict.fromkeys(range(100, 200), MIN_DF - 1)  # below threshold
        df_memory = {**big, **small}
        df_global = {**big, **small}
        result = compute_psi(
            df_memory=df_memory,
            df_global=df_global,
            m_memory=MIN_MEMORY_POINTS + 100,
            n_global=MIN_MEMORY_POINTS + 100,
        )
        # Only the 100 high-df terms survive Cochran's filter.
        assert result.num_terms == 100


class TestPsiMath:
    def test_identical_distributions_psi_is_zero(self) -> None:
        df = _identical_term_universe(NUM_BINS * 10, MIN_DF + 10)
        result = compute_psi(
            df_memory=df,
            df_global=df,
            m_memory=MIN_MEMORY_POINTS + 100,
            n_global=MIN_MEMORY_POINTS + 100,
        )
        assert result.status == "stable"
        # PSI should be exactly 0.0 (or float-noise close to 0) when both
        # distributions are identical: P_memory(b) == P_global(b) for every
        # bin, so each summand (q-p)*log((q+eps)/(p+eps)) collapses.
        assert result.psi is not None
        assert abs(result.psi) < 1e-9

    def test_significant_drift_when_df_global_is_doubled(self) -> None:
        # A heavily resource-laden corpus: df_global ~= 2 * df_memory for
        # most terms. This shifts IDF_global down systematically (rare
        # terms in memory become "common" globally), pushing the
        # distribution mass into different bins.
        num_terms = 200
        df_memory = {i: MIN_DF + (i % 7) for i in range(num_terms)}
        df_global = {i: df_memory[i] * 4 for i in range(num_terms)}
        # Add resource-only terms to inflate the global vocabulary.
        for j in range(num_terms, num_terms + 200):
            df_global[j] = MIN_DF + 10
        result = compute_psi(
            df_memory=df_memory,
            df_global=df_global,
            m_memory=500,
            n_global=2000,
        )
        assert result.status == "significant_drift"
        assert result.psi is not None
        assert result.psi >= PSI_SIGNIFICANT_THRESHOLD

    def test_top_divergent_terms_sorted_by_abs_delta(self) -> None:
        df_memory = dict.fromkeys(range(60), MIN_DF + 5)
        df_global = dict.fromkeys(range(60), MIN_DF + 5)
        # One term has a wildly different global df → biggest delta.
        df_global[0] = MIN_DF + 500
        result = compute_psi(
            df_memory=df_memory,
            df_global=df_global,
            m_memory=400,
            n_global=2000,
        )
        assert result.psi is not None
        assert result.top_divergent_terms, "should report top terms"
        # The biggest-delta term should land first.
        assert result.top_divergent_terms[0]["index"] == 0

    def test_eps_smoothing_handles_empty_bin(self) -> None:
        # Concentrate every memory term into a single IDF region by
        # setting df_memory uniform, then make df_global vary widely so
        # IDF_global terms scatter into bins that have zero memory mass.
        num_terms = 200
        df_memory = dict.fromkeys(range(num_terms), MIN_DF + 10)
        df_global = {i: MIN_DF + (i % 50) for i in range(num_terms)}
        result = compute_psi(
            df_memory=df_memory,
            df_global=df_global,
            m_memory=500,
            n_global=2000,
        )
        # No NaN/Inf — eps-smoothing should keep PSI finite.
        assert result.psi is not None
        assert math.isfinite(result.psi)


class TestTopDivergentTermsShape:
    def test_term_entries_use_int_index_no_plaintext(self) -> None:
        df_memory = dict.fromkeys(range(60), MIN_DF + 5)
        df_global = dict.fromkeys(range(60), MIN_DF + 50)
        result = compute_psi(
            df_memory=df_memory,
            df_global=df_global,
            m_memory=400,
            n_global=2000,
        )
        for entry in result.top_divergent_terms:
            assert set(entry.keys()) == {
                "index",
                "df_memory",
                "df_global",
                "idf_memory",
                "idf_global",
                "delta",
            }
            assert isinstance(entry["index"], int)


@pytest.mark.parametrize("count", [0, 5, NUM_BINS - 1])
def test_zero_or_few_memory_points_short_circuits(count: int) -> None:
    """The min-N gate fires before any expensive computation runs."""
    result = compute_psi(
        df_memory={1: MIN_DF + 5},
        df_global={1: MIN_DF + 5},
        m_memory=count,
        n_global=count,
    )
    assert result.status == "insufficient_data"
    assert result.psi is None
