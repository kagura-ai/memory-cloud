"""Unit tests for ``tests.eval.day4_analysis`` — the Day-4 verdict CLI.

Pure functions over synthetic result JSONs (``tmp_path``) — no DB/stack, no
live embedder. Drives the analysis via its public ``analyze()`` entry point
(the ``_main`` argparse wrapper is exercised indirectly by the module's own
``--help`` smoke, not here) so these tests pin the confirmatory semantics
independently of the CLI plumbing.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import pytest

from tests.eval.day4_analysis import (
    ALPHA,
    DELTA_HYBRID,
    DELTA_LEAK,
    GATED_ARMS,
    PRODUCTION_ARM,
    _h1_verdict,
    analyze,
)

_N_HELDOUT = 40
_N_PUBLIC = 10


def _splits(n_heldout: int = _N_HELDOUT, n_public: int = _N_PUBLIC) -> list[str]:
    return ["heldout"] * n_heldout + ["public"] * n_public


def _per_query(values: list[float], splits: list[str]) -> list[dict[str, Any]]:
    return [
        {
            "query_id": f"q{i}",
            "bucket": "b",
            "split": split,
            "p@5": v,
            "rr@10": min(1.0, v + 0.1),
        }
        for i, (v, split) in enumerate(zip(values, splits, strict=True))
    ]


def _write_run(
    tmp_path: Path,
    *,
    embedding_model: str,
    label: str,
    arm_values: dict[str, list[float]],
    n_heldout: int = _N_HELDOUT,
    n_public: int = _N_PUBLIC,
) -> Path:
    """Write one synthetic result JSON (``tests.eval.runner`` shape) to ``tmp_path``."""
    splits = _splits(n_heldout, n_public)
    arms = {arm: {"per_query": _per_query(values, splits)} for arm, values in arm_values.items()}
    result = {"embedding_model": embedding_model, "label": label, "arms": arms}
    path = tmp_path / f"{label}.json"
    path.write_text(json.dumps(result), encoding="utf-8")
    return path


def _near_identical_arms(n: int, *, base_seed: int) -> dict[str, list[float]]:
    """4 arms sharing one base signal plus tiny independent per-arm noise —
    i.e. the H0 null (no real arm-level effect) is true by construction."""
    base = [random.Random(base_seed).uniform(0.3, 0.7) for _ in range(n)]
    arms: dict[str, list[float]] = {}
    for i, arm in enumerate(GATED_ARMS):
        noise_rng = random.Random(base_seed + 100 + i)
        arms[arm] = [b + noise_rng.uniform(-0.01, 0.01) for b in base]
    return arms


class TestH0Omnibus:
    def test_rejects_when_one_arm_clearly_shifted(self, tmp_path):
        n = _N_HELDOUT + _N_PUBLIC
        base = [random.Random(111).uniform(0.1, 0.3) for _ in range(n)]
        arm_values: dict[str, list[float]] = {}
        for i, arm in enumerate(GATED_ARMS):
            noise_rng = random.Random(500 + i)
            shift = 0.4 if arm == "hybrid" else 0.0
            arm_values[arm] = [b + shift + noise_rng.uniform(-0.01, 0.01) for b in base]
        path = _write_run(
            tmp_path, embedding_model="m1", label="day4-m1-run0", arm_values=arm_values
        )

        result = analyze([path], inferential_run="day4-m1-run0")

        h0 = result["per_embedder"]["m1"]["h0"]
        assert h0["reject"] is True
        assert h0["p_value"] < ALPHA

    def test_does_not_reject_when_arms_near_identical(self, tmp_path):
        n = _N_HELDOUT + _N_PUBLIC
        arm_values = _near_identical_arms(n, base_seed=222)
        path = _write_run(
            tmp_path, embedding_model="m1", label="day4-m1-run0", arm_values=arm_values
        )

        result = analyze([path], inferential_run="day4-m1-run0")

        h0 = result["per_embedder"]["m1"]["h0"]
        assert h0["reject"] is False
        assert h0["p_value"] > ALPHA


class TestH1:
    def test_pass_case_hybrid_beats_best_single_with_margin(self, tmp_path):
        n = _N_HELDOUT + _N_PUBLIC
        base = [random.Random(333).uniform(0.2, 0.5) for _ in range(n)]
        arm_values = {
            "keyword": base,
            "semantic": [b - 0.05 for b in base],
            "hybrid": [b + 0.15 for b in base],
            "hybrid_neural": [b + 0.15 for b in base],
        }
        path = _write_run(
            tmp_path, embedding_model="m1", label="day4-m1-run0", arm_values=arm_values
        )

        result = analyze([path], inferential_run="day4-m1-run0")

        block = result["per_embedder"]["m1"]
        h1 = block["h1"]
        assert h1["best_single"] == "keyword"
        assert h1["ci_low"] > 0
        assert h1["mean"] >= 0.05
        assert h1["pass"] is True
        assert h1["tested"] == block["h0"]["reject"]
        # Strong, noiseless, consistent 0.15 offset across 50 queries — the
        # omnibus should reject decisively, making pass_gated demonstrably True.
        assert block["h0"]["reject"] is True
        assert h1["pass_gated"] is True

    def test_fail_case_hybrid_equals_best_single(self, tmp_path):
        n = _N_HELDOUT + _N_PUBLIC
        base = [random.Random(444).uniform(0.2, 0.5) for _ in range(n)]
        arm_values = {
            "keyword": base,
            "semantic": [b - 0.05 for b in base],
            "hybrid": base,
            "hybrid_neural": base,
        }
        path = _write_run(
            tmp_path, embedding_model="m1", label="day4-m1-run0", arm_values=arm_values
        )

        result = analyze([path], inferential_run="day4-m1-run0")

        h1 = result["per_embedder"]["m1"]["h1"]
        assert h1["mean"] == 0.0
        assert h1["pass"] is False
        assert h1["pass_gated"] is False

    def test_best_single_picks_semantic_when_its_mean_is_higher(self, tmp_path):
        n = _N_HELDOUT + _N_PUBLIC
        base = [random.Random(555).uniform(0.2, 0.4) for _ in range(n)]
        arm_values = {
            "keyword": base,
            "semantic": [b + 0.1 for b in base],
            "hybrid": [b + 0.2 for b in base],
            "hybrid_neural": [b + 0.2 for b in base],
        }
        path = _write_run(
            tmp_path, embedding_model="m1", label="day4-m1-run0", arm_values=arm_values
        )

        result = analyze([path], inferential_run="day4-m1-run0")

        assert result["per_embedder"]["m1"]["h1"]["best_single"] == "semantic"

    def test_best_single_ties_pick_keyword(self, tmp_path):
        n = _N_HELDOUT + _N_PUBLIC
        base = [random.Random(666).uniform(0.2, 0.4) for _ in range(n)]
        arm_values = {
            "keyword": base,
            "semantic": base,
            "hybrid": [b + 0.2 for b in base],
            "hybrid_neural": [b + 0.2 for b in base],
        }
        path = _write_run(
            tmp_path, embedding_model="m1", label="day4-m1-run0", arm_values=arm_values
        )

        result = analyze([path], inferential_run="day4-m1-run0")

        assert result["per_embedder"]["m1"]["h1"]["best_single"] == "keyword"


class TestH1VerdictPure:
    """``_h1_verdict`` is the small pure gating function factored out of the
    H1 block precisely so combinations like "H1 passes but H0 does not
    reject" can be unit-tested directly — a hybrid arm that decisively beats
    best_single tends to also make the omnibus reject, so that combination
    is awkward (not impossible, but not something worth relying on the RNG
    for) to hit by constructing a full synthetic run."""

    def test_pass_true_but_h0_does_not_reject_yields_pass_gated_false_and_untested(self):
        verdict = _h1_verdict(mean_=0.1, ci_low=0.02, h0_reject=False)
        assert verdict == {"pass": True, "pass_gated": False, "tested": False}

    def test_pass_true_and_h0_rejects_yields_pass_gated_true_and_tested(self):
        verdict = _h1_verdict(mean_=0.1, ci_low=0.02, h0_reject=True)
        assert verdict == {"pass": True, "pass_gated": True, "tested": True}

    def test_ci_low_not_above_zero_fails_regardless_of_h0(self):
        assert _h1_verdict(mean_=0.1, ci_low=0.0, h0_reject=True)["pass"] is False
        assert _h1_verdict(mean_=0.1, ci_low=-0.01, h0_reject=True)["pass"] is False

    def test_mean_below_delta_hybrid_fails_even_with_positive_ci_low(self):
        verdict = _h1_verdict(mean_=DELTA_HYBRID - 0.001, ci_low=0.01, h0_reject=True)
        assert verdict["pass"] is False
        assert verdict["pass_gated"] is False


class TestH3:
    def test_gap_is_public_minus_heldout_on_the_production_arm(self, tmp_path):
        heldout_vals = [random.Random(777).uniform(0.25, 0.35) for _ in range(_N_HELDOUT)]
        public_vals = [random.Random(888).uniform(0.45, 0.55) for _ in range(_N_PUBLIC)]
        other = [random.Random(999).uniform(0.2, 0.4) for _ in range(_N_HELDOUT + _N_PUBLIC)]
        arm_values = {arm: list(other) for arm in GATED_ARMS if arm != PRODUCTION_ARM}
        arm_values[PRODUCTION_ARM] = heldout_vals + public_vals
        path = _write_run(
            tmp_path, embedding_model="m1", label="day4-m1-run0", arm_values=arm_values
        )

        result = analyze([path], inferential_run="day4-m1-run0")

        h3 = result["per_embedder"]["m1"]["h3"]
        assert h3 is not None
        expected_gap = round(
            sum(public_vals) / len(public_vals) - sum(heldout_vals) / len(heldout_vals), 4
        )
        assert h3["gap_mean"] == pytest.approx(expected_gap, abs=1e-4)
        assert h3["within_delta_leak"] == (h3["gap_mean"] <= DELTA_LEAK)

    def test_null_when_corpus_has_no_public_queries(self, tmp_path):
        arm_values = {
            arm: [random.Random(1000 + i).uniform(0.2, 0.5) for _ in range(_N_HELDOUT)]
            for i, arm in enumerate(GATED_ARMS)
        }
        path = _write_run(
            tmp_path,
            embedding_model="m1",
            label="day4-m1-run0",
            arm_values=arm_values,
            n_heldout=_N_HELDOUT,
            n_public=0,
        )

        result = analyze([path], inferential_run="day4-m1-run0")

        block = result["per_embedder"]["m1"]
        assert block["h3"] is None
        assert block["n_public"] == 0


class TestAlignmentGuard:
    def test_shuffled_query_id_order_in_one_arm_raises_naming_it(self, tmp_path):
        n = _N_HELDOUT + _N_PUBLIC
        splits = _splits()
        values = [random.Random(11).uniform(0.2, 0.5) for _ in range(n)]
        arms = {arm: {"per_query": _per_query(values, splits)} for arm in GATED_ARMS}

        shuffled = list(arms["hybrid"]["per_query"])
        random.Random(2).shuffle(shuffled)
        assert shuffled != arms["hybrid"]["per_query"]  # sanity: the shuffle actually moved things
        arms["hybrid"]["per_query"] = shuffled

        result_json = {"embedding_model": "m1", "label": "day4-m1-run0", "arms": arms}
        path = tmp_path / "day4-m1-run0.json"
        path.write_text(json.dumps(result_json), encoding="utf-8")

        with pytest.raises(SystemExit, match="hybrid"):
            analyze([path], inferential_run="day4-m1-run0")

    def test_swapped_split_pattern_same_length_different_identities_is_fatal(self, tmp_path):
        """Two arms can have equal-length heldout subsets that nonetheless
        name different query_ids — e.g. one arm's split labels are swapped
        for a heldout/public pair, leaving the FULL query_id sequence (and
        therefore ``_assert_aligned``) untouched but changing exactly which
        query_ids the heldout filter picks out. Positional pairing would
        silently misalign here; the query_id-keyed join must catch it."""
        n = _N_HELDOUT + _N_PUBLIC
        splits = _splits()
        values = [random.Random(21).uniform(0.2, 0.5) for _ in range(n)]
        arms = {arm: {"per_query": _per_query(values, splits)} for arm in GATED_ARMS}

        hybrid_records = [dict(rec) for rec in arms["hybrid"]["per_query"]]
        hybrid_records[0]["split"], hybrid_records[_N_HELDOUT]["split"] = (
            hybrid_records[_N_HELDOUT]["split"],
            hybrid_records[0]["split"],
        )
        arms["hybrid"]["per_query"] = hybrid_records
        # Sanity: still 40 heldout records for "hybrid" (same length as every
        # other arm) — only the query_id identities differ (q0 dropped out,
        # q40 came in), and the FULL query_id sequence is untouched.
        assert sum(1 for rec in hybrid_records if rec["split"] == "heldout") == _N_HELDOUT
        assert [rec["query_id"] for rec in hybrid_records] == [
            rec["query_id"] for rec in arms["keyword"]["per_query"]
        ]

        result_json = {"embedding_model": "m1", "label": "day4-m1-run0", "arms": arms}
        path = tmp_path / "day4-m1-run0.json"
        path.write_text(json.dumps(result_json), encoding="utf-8")

        with pytest.raises(SystemExit, match="q0"):
            analyze([path], inferential_run="day4-m1-run0")


class TestArmMeansAcrossRuns:
    def test_min_median_max_over_three_synthetic_runs(self, tmp_path):
        n_heldout = 10
        means_by_label = {"day4-m1-run0": 0.2, "day4-m1-run1": 0.5, "day4-m1-run2": 0.8}
        paths = []
        for label, m in means_by_label.items():
            arm_values = {arm: [m] * n_heldout for arm in GATED_ARMS}
            paths.append(
                _write_run(
                    tmp_path,
                    embedding_model="m1",
                    label=label,
                    arm_values=arm_values,
                    n_heldout=n_heldout,
                    n_public=0,
                )
            )

        result = analyze(paths, inferential_run="day4-m1-run0")

        arm_means = result["per_embedder"]["m1"]["arm_means_across_runs"]
        for arm in GATED_ARMS:
            assert arm_means[arm] == {"min": 0.2, "median": 0.5, "max": 0.8}


class TestDeterminism:
    def test_same_inputs_analyzed_twice_are_identical_modulo_run_date(self, tmp_path):
        n = _N_HELDOUT + _N_PUBLIC
        arm_values = _near_identical_arms(n, base_seed=42)
        path = _write_run(
            tmp_path, embedding_model="m1", label="day4-m1-run0", arm_values=arm_values
        )

        r1 = analyze([path], inferential_run="day4-m1-run0")
        r2 = analyze([path], inferential_run="day4-m1-run0")

        r1.pop("run_date")
        r2.pop("run_date")
        assert r1 == r2


class TestFatalInputErrors:
    def test_missing_per_query_for_a_gated_arm_is_fatal(self, tmp_path):
        arms = {arm: {"per_query": []} for arm in GATED_ARMS}
        del arms["hybrid"]["per_query"]
        result_json = {"embedding_model": "m1", "label": "day4-m1-run0", "arms": arms}
        path = tmp_path / "bad.json"
        path.write_text(json.dumps(result_json), encoding="utf-8")

        with pytest.raises(SystemExit, match="hybrid"):
            analyze([path], inferential_run="day4-m1-run0")

    def test_missing_embedding_model_is_fatal(self, tmp_path):
        arms = {arm: {"per_query": []} for arm in GATED_ARMS}
        result_json = {"label": "day4-m1-run0", "arms": arms}
        path = tmp_path / "bad.json"
        path.write_text(json.dumps(result_json), encoding="utf-8")

        with pytest.raises(SystemExit, match="embedding_model"):
            analyze([path], inferential_run="day4-m1-run0")

    def test_missing_label_is_fatal(self, tmp_path):
        arms = {arm: {"per_query": []} for arm in GATED_ARMS}
        result_json = {"embedding_model": "m1", "arms": arms}
        path = tmp_path / "bad.json"
        path.write_text(json.dumps(result_json), encoding="utf-8")

        with pytest.raises(SystemExit, match="label"):
            analyze([path], inferential_run="day4-m1-run0")

    def test_ambiguous_inferential_run_resolution_is_fatal(self, tmp_path):
        # Neither an exact --inferential-run match nor a unique '-run0' label
        # exists in this group -> 0 candidates either way.
        arm_values = {arm: [0.5] * _N_HELDOUT for arm in GATED_ARMS}
        path = _write_run(
            tmp_path,
            embedding_model="m1",
            label="day4-m1-alpha",
            arm_values=arm_values,
            n_heldout=_N_HELDOUT,
            n_public=0,
        )

        with pytest.raises(SystemExit, match="ambiguous|could not resolve"):
            analyze([path], inferential_run="day4-m1-run0")


class TestRunCountWarning:
    def test_group_with_two_runs_warns_but_does_not_raise(self, tmp_path, capsys):
        arm_values = {arm: [0.5] * _N_HELDOUT for arm in GATED_ARMS}
        paths = [
            _write_run(
                tmp_path,
                embedding_model="m1",
                label=f"day4-m1-run{i}",
                arm_values=arm_values,
                n_heldout=_N_HELDOUT,
                n_public=0,
            )
            for i in range(2)
        ]

        analyze(paths, inferential_run="day4-m1-run0")

        captured = capsys.readouterr()
        assert "WARNING" in captured.err
        assert "m1" in captured.err
