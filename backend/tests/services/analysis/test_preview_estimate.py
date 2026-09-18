"""``preview.estimate_cost`` reads the run's own rates (#1570).

The estimate used to hardcode gpt-5-nano's rate card; it now takes the
``snapshot["rates"]`` the orchestrator freezes from ``llm_pricing`` so the
pre-flight number and the post-run ``cost_actual_cents`` share one price.
"""

from __future__ import annotations

import pytest

from services.analysis.preview import (
    _INPUT_TOKENS_PER_CALL,
    _OUTPUT_TOKENS_PER_CALL,
    estimate_cost,
)
from services.analysis.reporter import _compute_actual_cost_cents, _CostTotals

# The c03 seed's gpt-5-nano rates, per million — what the estimate hardcoded before.
_GPT5_NANO_RATES = {"input_tokens": 0.20, "output_tokens": 1.25, "cache_read_tokens": 0.02}


class TestEstimateCost:
    @pytest.mark.parametrize(
        ("memory_count", "clusters", "cents"),
        [
            (100, 10, 1),  # 11 calls → $0.0044 → ceil → 1 cent (the ≥1 cent floor)
            (8000, 90, 4),  # 91 calls → $0.0364 → 4 cents (module docstring example)
        ],
    )
    def test_seeded_rates_reproduce_the_old_numbers(self, memory_count, clusters, cents):
        estimate = estimate_cost(memory_count, rates=_GPT5_NANO_RATES)
        assert estimate.cluster_count_estimate == clusters
        assert estimate.estimated_cost_cents == cents
        assert estimate.breakdown == {
            "input_tokens": (clusters + 1) * _INPUT_TOKENS_PER_CALL,
            "output_tokens": (clusters + 1) * _OUTPUT_TOKENS_PER_CALL,
            "calls": clusters + 1,
        }

    def test_no_rates_means_estimate_unavailable(self):
        estimate = estimate_cost(100, rates=None)
        assert estimate.estimated_cost_cents is None
        # The size-derived parts are still reported.
        assert estimate.cluster_count_estimate == 10
        assert estimate.breakdown["calls"] == 11

    @pytest.mark.parametrize("present", ["input_tokens", "output_tokens"])
    def test_missing_either_token_rate_means_estimate_unavailable(self, present):
        assert estimate_cost(100, rates={present: 1.0}).estimated_cost_cents is None

    def test_under_two_memories_costs_nothing_even_without_rates(self):
        estimate = estimate_cost(1, rates=None)
        assert estimate.estimated_cost_cents == 0
        assert estimate.cluster_count_estimate == 0

    def test_model_id_is_a_label(self):
        assert estimate_cost(10, rates=_GPT5_NANO_RATES, model_id="my-llm").model_id == "my-llm"


class TestEstimateAndActualShareRates:
    """Acceptance #1570: the same snapshot priced through both paths agrees."""

    @pytest.mark.parametrize(
        "rates",
        [
            _GPT5_NANO_RATES,
            {"input_tokens": 3.0, "output_tokens": 15.0},  # a priced self-hosted LLM
        ],
    )
    def test_actual_equals_estimate_when_usage_matches_the_forecast(self, rates):
        estimate = estimate_cost(500, rates=rates)
        totals = _CostTotals(
            input_tokens=estimate.breakdown["input_tokens"],
            output_tokens=estimate.breakdown["output_tokens"],
            cached_input_tokens=0,
            calls=estimate.breakdown["calls"],
        )
        actual = _compute_actual_cost_cents(totals, {"rates": rates})
        assert estimate.estimated_cost_cents is not None
        assert actual == estimate.estimated_cost_cents

    def test_both_paths_report_unknown_without_rates(self):
        estimate = estimate_cost(500, rates={})
        totals = _CostTotals(input_tokens=10_000, output_tokens=500, cached_input_tokens=0, calls=1)
        assert estimate.estimated_cost_cents is None
        assert _compute_actual_cost_cents(totals, {"rates": {}}) is None
