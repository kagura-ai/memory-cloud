"""Unit tests for ``tests.eval.day5_analysis`` — the Day-5 H4 verdict CLI.

Pure functions over synthetic result JSONs (``tmp_path``) — no DB/stack, no
live embedder, no judge-LLM. Drives the analysis via its public ``analyze()``
entry point (the ``_main`` argparse wrapper is exercised indirectly by the
module's own ``--help`` smoke, not here) so these tests pin the confirmatory
semantics independently of the CLI plumbing.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import pytest

from tests.eval.day5_analysis import (
    ARM_MC_PROD,
    ARM_MC_UPDATE,
    ARM_VANILLA_RAG,
    ARMS,
    MIN_COMPLETE_PAIRS,
    OUTCOME_CURRENT_ONLY,
    OUTCOME_CURRENT_OVER_STALE,
    OUTCOME_NEITHER,
    OUTCOME_STALE_OVER_CURRENT,
    analyze,
    is_update_success,
)


def _current_over_stale(qid: str) -> dict[str, Any]:
    return {
        "query_id": qid,
        "outcome": OUTCOME_CURRENT_OVER_STALE,
        "current_rank": 1,
        "stale_rank": 2,
    }


def _stale_over_current(qid: str) -> dict[str, Any]:
    return {
        "query_id": qid,
        "outcome": OUTCOME_STALE_OVER_CURRENT,
        "current_rank": 2,
        "stale_rank": 1,
    }


def _current_only(qid: str, rank: int = 1) -> dict[str, Any]:
    return {
        "query_id": qid,
        "outcome": OUTCOME_CURRENT_ONLY,
        "current_rank": rank,
        "stale_rank": None,
    }


def _neither(qid: str) -> dict[str, Any]:
    return {"query_id": qid, "outcome": OUTCOME_NEITHER, "current_rank": None, "stale_rank": None}


def _write_run(
    tmp_path: Path,
    *,
    label: str,
    vr_rows: list[dict[str, Any]],
    mc_rows: list[dict[str, Any]],
    prod_rows: list[dict[str, Any]] | None = None,
) -> Path:
    """Write one synthetic result JSON (``tests.eval.update_runner`` shape) to
    ``tmp_path``. ``mc_prod`` defaults to mirroring ``mc_update`` (its own
    content is irrelevant to most tests here; only its query_id alignment
    matters, unless a test overrides it)."""
    arms = {
        ARM_VANILLA_RAG: {"per_query": vr_rows},
        ARM_MC_UPDATE: {"per_query": mc_rows},
        ARM_MC_PROD: {"per_query": prod_rows if prod_rows is not None else list(mc_rows)},
    }
    result = {"label": label, "arms": arms}
    path = tmp_path / f"{label}.json"
    path.write_text(json.dumps(result), encoding="utf-8")
    return path


class TestIsUpdateSuccessPure:
    """``is_update_success`` is the small pure predicate factored out of the
    supporting block precisely so its boundary conditions (which rank "wins",
    current-absent-is-never-success) can be unit-tested directly."""

    def test_current_present_stale_absent_is_success(self):
        assert is_update_success(current_rank=3, stale_rank=None) is True

    def test_current_absent_is_never_success(self):
        assert is_update_success(current_rank=None, stale_rank=5) is False
        assert is_update_success(current_rank=None, stale_rank=None) is False

    def test_current_ranked_above_stale_is_success(self):
        assert is_update_success(current_rank=1, stale_rank=2) is True

    def test_current_ranked_below_stale_is_not_success(self):
        assert is_update_success(current_rank=2, stale_rank=1) is False

    def test_equal_ranks_is_not_success(self):
        # Not a realistic ranking (two distinct docs can't share a rank), but
        # the predicate's own boundary (strict ``<``) is worth pinning.
        assert is_update_success(current_rank=1, stale_rank=1) is False


class TestH4GatedPass:
    def test_pass_when_mc_clearly_beats_vr_with_margin(self, tmp_path):
        qids = [f"q{i}" for i in range(40)]
        vr_rows = [_stale_over_current(q) for q in qids[:35]] + [
            _current_over_stale(q) for q in qids[35:]
        ]
        mc_rows = [_current_over_stale(q) for q in qids]
        path = _write_run(tmp_path, label="day5-update-run0", vr_rows=vr_rows, mc_rows=mc_rows)

        result = analyze([path], inferential_run="day5-update-run0")

        h4 = result["h4"]
        assert h4["n_complete_pairs"] == 40
        assert h4["mean"] == pytest.approx(35 / 40, abs=1e-4)
        assert h4["ci_low"] > 0
        assert h4["pass"] is True
        assert h4["underpowered"] is False


class TestH4GatedFail:
    def test_fail_when_mc_does_not_beat_vr(self, tmp_path):
        qids = [f"q{i}" for i in range(40)]
        # 20 ties (diff=0), 10 mc-wins (diff=+1), 10 vr-wins (diff=-1) -> mean
        # exactly 0.0, well below delta_update regardless of the CI.
        vr_rows = (
            [_current_over_stale(q) for q in qids[:20]]
            + [_stale_over_current(q) for q in qids[20:30]]
            + [_current_over_stale(q) for q in qids[30:]]
        )
        mc_rows = (
            [_current_over_stale(q) for q in qids[:20]]
            + [_current_over_stale(q) for q in qids[20:30]]
            + [_stale_over_current(q) for q in qids[30:]]
        )
        path = _write_run(tmp_path, label="day5-update-run0", vr_rows=vr_rows, mc_rows=mc_rows)

        result = analyze([path], inferential_run="day5-update-run0")

        h4 = result["h4"]
        assert h4["n_complete_pairs"] == 40
        assert h4["mean"] == 0.0
        assert h4["pass"] is False


class TestH4Underpowered:
    def test_underpowered_when_complete_pairs_below_threshold(self, tmp_path):
        complete_qids = [f"q{i}" for i in range(20)]
        excluded_qids = [f"q{i}" for i in range(20, 50)]
        vr_rows = [_stale_over_current(q) for q in complete_qids] + [
            _neither(q) for q in excluded_qids
        ]
        mc_rows = [_current_over_stale(q) for q in complete_qids] + [
            _neither(q) for q in excluded_qids
        ]
        path = _write_run(tmp_path, label="day5-update-run0", vr_rows=vr_rows, mc_rows=mc_rows)

        result = analyze([path], inferential_run="day5-update-run0")

        h4 = result["h4"]
        assert h4["n_complete_pairs"] == 20
        assert h4["n_complete_pairs"] < MIN_COMPLETE_PAIRS
        assert h4["underpowered"] is True


class TestUpdateByRemovalMigration:
    """Dedup REMOVAL of the stale doc lands the query in mc_update's
    ``current_only`` (D4) — excluded from the gated complete-pair set, but
    still an update_success@10 win for mc_update."""

    def test_mc_current_only_shrinks_complete_pairs_but_boosts_mc_success(self, tmp_path):
        normal_qids = [f"q{i}" for i in range(20)]
        removal_qids = [f"q{i}" for i in range(20, 30)]
        vr_rows = [_stale_over_current(q) for q in normal_qids] + [
            _stale_over_current(q) for q in removal_qids
        ]
        mc_rows = [_current_over_stale(q) for q in normal_qids] + [
            _current_only(q) for q in removal_qids
        ]
        path = _write_run(tmp_path, label="day5-update-run0", vr_rows=vr_rows, mc_rows=mc_rows)

        result = analyze([path], inferential_run="day5-update-run0")

        # Only the 20 "normal" queries are complete pairs -- the 10 removal
        # queries have mc_update's outcome == current_only, not determinate.
        assert result["h4"]["n_complete_pairs"] == 20
        assert result["decomposition"][ARM_MC_UPDATE]["counts"]["current_only"] == 10
        # But every one of the 30 queries is still an mc_update success
        # (current present, stale absent-or-behind) -- the removal group
        # boosts mc_update's unconditional update_success@10 rate even though
        # it shrank the gated conditional's coverage.
        assert result["supporting"]["update_success_at_10"][ARM_MC_UPDATE]["mean"] == 1.0
        assert result["supporting"]["update_success_at_10"][ARM_VANILLA_RAG]["mean"] == 0.0


class TestAlignmentGuard:
    def test_shuffled_query_id_order_in_one_arm_raises_naming_it(self, tmp_path):
        qids = [f"q{i}" for i in range(10)]
        vr_rows = [_stale_over_current(q) for q in qids]
        mc_rows = [_current_over_stale(q) for q in qids]
        prod_rows = list(mc_rows)
        random.Random(3).shuffle(prod_rows)
        assert prod_rows != mc_rows  # sanity: the shuffle actually moved things

        path = _write_run(
            tmp_path,
            label="day5-update-run0",
            vr_rows=vr_rows,
            mc_rows=mc_rows,
            prod_rows=prod_rows,
        )

        with pytest.raises(SystemExit, match=ARM_MC_PROD):
            analyze([path], inferential_run="day5-update-run0")


class TestDeterminism:
    def test_same_inputs_analyzed_twice_are_identical_modulo_run_date(self, tmp_path):
        qids = [f"q{i}" for i in range(30)]
        vr_rows = [
            _stale_over_current(q) if i % 2 == 0 else _current_over_stale(q)
            for i, q in enumerate(qids)
        ]
        mc_rows = [_current_over_stale(q) for q in qids]
        path = _write_run(tmp_path, label="day5-update-run0", vr_rows=vr_rows, mc_rows=mc_rows)

        r1 = analyze([path], inferential_run="day5-update-run0")
        r2 = analyze([path], inferential_run="day5-update-run0")

        r1.pop("run_date")
        r2.pop("run_date")
        assert r1 == r2


class TestAcrossRuns:
    def test_min_median_max_over_three_synthetic_runs(self, tmp_path):
        qids = [f"q{i}" for i in range(20)]

        def _run_with_mc_win_fraction(label: str, frac: float) -> Path:
            n_win = round(frac * len(qids))
            vr_rows = [_stale_over_current(q) for q in qids]
            mc_rows = [_current_over_stale(q) for q in qids[:n_win]] + [
                _stale_over_current(q) for q in qids[n_win:]
            ]
            return _write_run(tmp_path, label=label, vr_rows=vr_rows, mc_rows=mc_rows)

        paths = [
            _run_with_mc_win_fraction("day5-update-run0", 0.2),
            _run_with_mc_win_fraction("day5-update-run1", 0.5),
            _run_with_mc_win_fraction("day5-update-run2", 0.8),
        ]

        result = analyze(paths, inferential_run="day5-update-run0")

        gated = result["across_runs"]["gated_conditional_mean_diff"]
        assert gated["min"] == pytest.approx(0.2, abs=1e-4)
        assert gated["median"] == pytest.approx(0.5, abs=1e-4)
        assert gated["max"] == pytest.approx(0.8, abs=1e-4)

        mc_success = result["across_runs"]["update_success_at_10"][ARM_MC_UPDATE]
        assert mc_success["min"] == pytest.approx(0.2, abs=1e-4)
        assert mc_success["median"] == pytest.approx(0.5, abs=1e-4)
        assert mc_success["max"] == pytest.approx(0.8, abs=1e-4)


class TestFatalInputErrors:
    def test_missing_per_query_for_an_arm_is_fatal(self, tmp_path):
        arms = {arm: {"per_query": []} for arm in ARMS}
        del arms[ARM_MC_UPDATE]["per_query"]
        result_json = {"label": "day5-update-run0", "arms": arms}
        path = tmp_path / "bad.json"
        path.write_text(json.dumps(result_json), encoding="utf-8")

        with pytest.raises(SystemExit, match=ARM_MC_UPDATE):
            analyze([path], inferential_run="day5-update-run0")

    def test_missing_label_is_fatal(self, tmp_path):
        arms = {arm: {"per_query": []} for arm in ARMS}
        result_json = {"arms": arms}
        path = tmp_path / "bad.json"
        path.write_text(json.dumps(result_json), encoding="utf-8")

        with pytest.raises(SystemExit, match="label"):
            analyze([path], inferential_run="day5-update-run0")

    def test_ambiguous_inferential_run_resolution_is_fatal(self, tmp_path):
        qids = [f"q{i}" for i in range(10)]
        vr_rows = [_stale_over_current(q) for q in qids]
        mc_rows = [_current_over_stale(q) for q in qids]
        path = _write_run(tmp_path, label="day5-update-alpha", vr_rows=vr_rows, mc_rows=mc_rows)

        with pytest.raises(SystemExit, match="ambiguous|could not resolve"):
            analyze([path], inferential_run="day5-update-run0")

    def test_zero_complete_pairs_is_fatal(self, tmp_path):
        qids = [f"q{i}" for i in range(10)]
        vr_rows = [_neither(q) for q in qids]
        mc_rows = [_neither(q) for q in qids]
        path = _write_run(tmp_path, label="day5-update-run0", vr_rows=vr_rows, mc_rows=mc_rows)

        with pytest.raises(SystemExit, match="0 complete pairs"):
            analyze([path], inferential_run="day5-update-run0")


class TestRunCountWarning:
    def test_run_count_not_3_warns_but_does_not_raise(self, tmp_path, capsys):
        qids = [f"q{i}" for i in range(10)]
        vr_rows = [_stale_over_current(q) for q in qids]
        mc_rows = [_current_over_stale(q) for q in qids]
        paths = [
            _write_run(tmp_path, label=f"day5-update-run{i}", vr_rows=vr_rows, mc_rows=mc_rows)
            for i in range(2)
        ]

        analyze(paths, inferential_run="day5-update-run0")

        captured = capsys.readouterr()
        assert "WARNING" in captured.err
