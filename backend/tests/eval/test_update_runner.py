"""Unit tests for ``tests.eval.update_runner`` — pure helpers only.

``classify_update_outcome`` is the only pure function in the module (the rest
is live DB orchestration, exercised by the controller task's real runs, not
here). No DB/stack, no live embedder — importing the module itself must stay
stack-free too (verified separately via a bare ``python -c "import ..."``).
"""

from __future__ import annotations

import pytest

from tests.eval.update_runner import (
    K,
    classify_update_outcome,
)

CURRENT = "u01-v2"
STALE = "u01-v1"


class TestBothPresent:
    def test_current_ranked_above_stale(self):
        ranking = [CURRENT, STALE, "f01"]
        result = classify_update_outcome(ranking, CURRENT, STALE, K)
        assert result == {
            "outcome": "current_over_stale",
            "current_rank": 1,
            "stale_rank": 2,
        }

    def test_stale_ranked_above_current(self):
        ranking = [STALE, CURRENT, "f01"]
        result = classify_update_outcome(ranking, CURRENT, STALE, K)
        assert result == {
            "outcome": "stale_over_current",
            "current_rank": 2,
            "stale_rank": 1,
        }


class TestOnlyOnePresent:
    def test_current_only(self):
        ranking = ["f01", CURRENT, "f02"]
        result = classify_update_outcome(ranking, CURRENT, STALE, K)
        assert result == {"outcome": "current_only", "current_rank": 2, "stale_rank": None}

    def test_stale_only(self):
        ranking = ["f01", STALE, "f02"]
        result = classify_update_outcome(ranking, CURRENT, STALE, K)
        assert result == {"outcome": "stale_only", "current_rank": None, "stale_rank": 2}


class TestNeitherPresent:
    def test_neither_in_ranking(self):
        ranking = ["f01", "f02", "f03"]
        result = classify_update_outcome(ranking, CURRENT, STALE, K)
        assert result == {"outcome": "neither", "current_rank": None, "stale_rank": None}

    def test_empty_ranking(self):
        result = classify_update_outcome([], CURRENT, STALE, K)
        assert result == {"outcome": "neither", "current_rank": None, "stale_rank": None}


class TestKWindowCutoff:
    def test_doc_at_position_k_plus_1_counts_as_absent(self):
        # k=3: window is indices 0..2 (1-based ranks 1..3). Placing CURRENT at
        # rank 4 (index 3) must NOT count as present.
        ranking = ["f01", "f02", "f03", CURRENT]
        result = classify_update_outcome(ranking, CURRENT, STALE, k=3)
        assert result == {"outcome": "neither", "current_rank": None, "stale_rank": None}

    def test_doc_at_position_k_counts_as_present(self):
        # k=3: rank 3 (index 2) is the last in-window position.
        ranking = ["f01", "f02", CURRENT]
        result = classify_update_outcome(ranking, CURRENT, STALE, k=3)
        assert result["current_rank"] == 3
        assert result["outcome"] == "current_only"

    def test_both_present_but_stale_pushed_past_k(self):
        ranking = [CURRENT, "f01", "f02", STALE]
        result = classify_update_outcome(ranking, CURRENT, STALE, k=3)
        assert result == {"outcome": "current_only", "current_rank": 1, "stale_rank": None}


class TestOneBasedRanks:
    def test_first_position_is_rank_1(self):
        result = classify_update_outcome([CURRENT], CURRENT, STALE, K)
        assert result["current_rank"] == 1


class TestCurrentEqualsStaleGuard:
    def test_raises_value_error_naming_the_id(self):
        with pytest.raises(ValueError, match=CURRENT):
            classify_update_outcome([CURRENT], CURRENT, CURRENT, K)
