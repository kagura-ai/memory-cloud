"""Unit tests for the deterministic retrieval metrics (Issue #344).

Pure functions, no infra — these run in normal CI and pin the metric math so a
regression in P@k / MRR computation is caught independently of the live harness.
"""

from __future__ import annotations

import math

import pytest

from tests.eval.metrics import (
    mean_ndcg_at_k,
    mean_precision_at_k,
    mrr_at_k,
    ndcg_at_k,
    precision_at_k,
    reciprocal_rank_at_k,
    source_recall_share,
)


class TestPrecisionAtK:
    def test_all_relevant_in_top_k(self):
        assert precision_at_k(["a", "b", "c"], {"a", "b", "c"}, 3) == 1.0

    def test_partial(self):
        # 1 of top-3 relevant.
        assert precision_at_k(["a", "x", "y"], {"a", "b"}, 3) == 1 / 3

    def test_denominator_is_k_not_len_under_retrieval(self):
        # Only 2 results returned but k=5 → under-retrieval depresses the score.
        assert precision_at_k(["a", "b"], {"a", "b"}, 5) == 2 / 5

    def test_k_zero_is_zero(self):
        assert precision_at_k(["a"], {"a"}, 0) == 0.0


class TestReciprocalRank:
    def test_first_position(self):
        assert reciprocal_rank_at_k(["a", "b"], {"a"}, 5) == 1.0

    def test_second_position(self):
        assert reciprocal_rank_at_k(["x", "a", "b"], {"a"}, 5) == 0.5

    def test_relevant_beyond_k_is_zero(self):
        assert reciprocal_rank_at_k(["x", "y", "a"], {"a"}, 2) == 0.0

    def test_none_relevant_is_zero(self):
        assert reciprocal_rank_at_k(["x", "y"], {"a"}, 5) == 0.0


class TestAggregates:
    def test_mrr_at_k_mean(self):
        rankings = [
            (["a", "x"], {"a"}),  # rr = 1.0
            (["x", "a"], {"a"}),  # rr = 0.5
        ]
        assert mrr_at_k(rankings, 5) == 0.75

    def test_mean_precision(self):
        rankings = [
            (["a", "b"], {"a", "b"}),  # p@2 = 1.0
            (["a", "x"], {"a"}),  # p@2 = 0.5
        ]
        assert mean_precision_at_k(rankings, 2) == 0.75

    def test_empty_inputs_are_zero_not_error(self):
        assert mrr_at_k([], 5) == 0.0
        assert mean_precision_at_k([], 5) == 0.0


class TestNDCG:
    def test_perfect_ranking_is_one(self):
        assert ndcg_at_k(["a", "b", "x"], {"a", "b"}, 3) == 1.0

    def test_single_relevant_at_position_two(self):
        # DCG = 1/log2(3); IDCG = 1/log2(2) = 1.0.
        expected = 1.0 / math.log2(3)
        assert ndcg_at_k(["x", "a", "y"], {"a"}, 5) == pytest.approx(expected)

    def test_ideal_caps_at_k(self):
        # 3 relevant docs but k=2 — both top-2 relevant is the best achievable.
        assert ndcg_at_k(["a", "b"], {"a", "b", "c"}, 2) == 1.0

    def test_no_relevant_retrieved_is_zero(self):
        assert ndcg_at_k(["x", "y"], {"a"}, 5) == 0.0

    def test_empty_gold_set_is_zero_not_error(self):
        assert ndcg_at_k(["x", "y"], set(), 5) == 0.0

    def test_k_zero_is_zero(self):
        assert ndcg_at_k(["a"], {"a"}, 0) == 0.0

    def test_relevant_beyond_k_ignored(self):
        assert ndcg_at_k(["x", "y", "a"], {"a"}, 2) == 0.0

    def test_mean_ndcg(self):
        rankings = [
            (["a", "x"], {"a"}),  # ndcg = 1.0
            (["x", "a"], {"a"}),  # ndcg = 1/log2(3)
        ]
        expected = (1.0 + 1.0 / math.log2(3)) / 2
        assert mean_ndcg_at_k(rankings, 5) == pytest.approx(expected)

    def test_mean_empty_is_zero_not_error(self):
        assert mean_ndcg_at_k([], 5) == 0.0


class TestArmOrder:
    def test_graph_writing_neural_arm_runs_last(self):
        # Load-bearing measurement-validity invariant (#967): with
        # ENABLE_NEURAL_MEMORY=true, recall() performs Hebbian/co-activation
        # graph WRITES, so the neural arm must come after every read-only arm
        # or it would warm the graph for the arms measured after it.
        from tests.eval.runner import _ARMS

        neural_arms = [name for name, _, neural in _ARMS if neural]
        assert neural_arms == [_ARMS[-1][0]], (
            "exactly one neural-enabled arm is expected, and it must run last"
        )


class TestSourceRecallShare:
    def test_even_split(self):
        shares = source_recall_share(["memory", "resource", "memory", "resource"], 4)
        assert shares == {"memory": 0.5, "resource": 0.5}

    def test_realized_mix_under_k(self):
        # Only 2 results but k=10 → shares over the realized 2, not diluted by k.
        shares = source_recall_share(["memory", "memory"], 10)
        assert shares == {"memory": 1.0}

    def test_empty_is_empty_dict(self):
        assert source_recall_share([], 5) == {}
