"""Deterministic tests for the #1210 CI contract gates.

The gate script turns the eval program's headline numbers into standing
contracts (claims → contracts). These tests pin:

1. Metric derivation from a REAL archived results file
   (``results/day5-update-run0-2026-07-03.json`` — the campaign run whose
   numbers appear in the tech report: mc_update 0.92, vanilla 0.56).
2. The canary criterion from the #1210 acceptance list: a results file where
   the stale-deletion failure mode reappears (``stale_only > 0``) MUST breach
   — this is "revert the #1198 veto turns the workflow red" made testable
   without actually reverting.
3. The 3-value exit model (COO gate1 design): PASS(0) / BREACH(1) / INFRA(3),
   with ``advisory`` mode always exiting 0 while still reporting breaches.
4. Contracts on fields that may be absent in older files (e.g.
   ``llm_call_failures_total``) SKIP instead of breaching or passing
   vacuously.
"""

import json
from pathlib import Path

import pytest

from tests.eval.ci_gates import (
    EXIT_BREACH,
    EXIT_INFRA,
    EXIT_PASS,
    derive_update_metrics,
    evaluate_contracts,
    exit_code_for,
    main,
    render_summary,
)

_EVAL_ROOT = Path(__file__).resolve().parent
_ARCHIVED_RUN = _EVAL_ROOT / "results" / "day5-update-run0-2026-07-03.json"
_BASELINE = _EVAL_ROOT / "fixtures" / "ci_baseline.json"


def _load_archived() -> dict:
    return json.loads(_ARCHIVED_RUN.read_text(encoding="utf-8"))


def _load_baseline() -> dict:
    return json.loads(_BASELINE.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- derivation


def test_derive_update_metrics_matches_published_numbers() -> None:
    """The derivation reproduces the tech report's campaign-run numbers."""
    metrics = derive_update_metrics(_load_archived())
    mc = metrics["arms"]["mc_update"]
    vr = metrics["arms"]["vanilla_rag"]
    assert mc["n"] == 50
    assert mc["update_success_at_k"] == pytest.approx(0.92)
    assert mc["outcomes"]["stale_only"] == 0
    assert vr["update_success_at_k"] == pytest.approx(0.56)
    # Conditional ordering (the prereg estimand): both-retrievable pairs only.
    assert mc["conditional_rate"] == pytest.approx(46 / 50)
    assert vr["conditional_rate"] == pytest.approx(28 / 50)
    # Old files predate the llm_call_failures_total summary field.
    assert metrics["llm_call_failures_total"] is None


# ---------------------------------------------------------------- contracts


def test_archived_run_passes_baseline_contracts() -> None:
    entries = evaluate_contracts(_load_baseline(), update_results=_load_archived())
    assert entries, "expected at least one contract entry"
    assert all(e["status"] in {"pass", "skip"} for e in entries), render_summary(entries)
    # llm_call_failures is absent in the archived file → skip, never a pass.
    failures = [e for e in entries if e["name"] == "update.llm_call_failures_zero"]
    assert failures and failures[0]["status"] == "skip"


def test_stale_only_canary_breaches() -> None:
    """#1210 acceptance: re-introduced stale-deletion turns the gate red."""
    results = _load_archived()
    for q in results["arms"]["mc_update"]["per_query"][:5]:
        q["outcome"] = "stale_only"
        q["current_rank"] = None
        q["stale_rank"] = 1
    entries = evaluate_contracts(_load_baseline(), update_results=results)
    breached = {e["name"] for e in entries if e["status"] == "breach"}
    assert "update.stale_only_zero" in breached
    assert exit_code_for(entries, mode="blocking") == EXIT_BREACH
    # Advisory mode reports the breach but does not fail the run.
    assert exit_code_for(entries, mode="advisory") == EXIT_PASS


def test_stale_only_in_mc_prod_arm_alone_breaches() -> None:
    """stale_only is summed over EVERY MC-machinery arm, not just mc_update.

    Pins the arm filter (``name != "vanilla_rag"``): a refactor narrowing it
    to ``name == "mc_update"`` would silently drop mc_prod coverage.
    """
    results = _load_archived()
    q = results["arms"]["mc_prod"]["per_query"][0]
    q["outcome"] = "stale_only"
    q["current_rank"] = None
    q["stale_rank"] = 1
    entries = evaluate_contracts(_load_baseline(), update_results=results)
    breached = {e["name"] for e in entries if e["status"] == "breach"}
    assert "update.stale_only_zero" in breached


def test_update_success_floor_breaches() -> None:
    results = _load_archived()
    # Degrade mc_update below the pinned floor: flip wins to losses.
    for q in results["arms"]["mc_update"]["per_query"][:12]:
        q["outcome"] = "stale_over_current"
        q["current_rank"] = 2
        q["stale_rank"] = 1
    entries = evaluate_contracts(_load_baseline(), update_results=results)
    breached = {e["name"] for e in entries if e["status"] == "breach"}
    assert "update.mc_update_success_floor" in breached


def test_llm_call_failures_breaches_when_present_and_nonzero() -> None:
    results = _load_archived()
    results["sleep_summary"]["llm_call_failures_total"] = 5
    entries = evaluate_contracts(_load_baseline(), update_results=results)
    breached = {e["name"] for e in entries if e["status"] == "breach"}
    assert "update.llm_call_failures_zero" in breached


def test_vanilla_sanity_band_breaches_on_harness_corruption() -> None:
    """A VR arm far outside its band means the harness/corpus broke."""
    results = _load_archived()
    for q in results["arms"]["vanilla_rag"]["per_query"]:
        q["outcome"] = "current_over_stale"
        q["current_rank"] = 1
        q["stale_rank"] = 2
    entries = evaluate_contracts(_load_baseline(), update_results=results)
    breached = {e["name"] for e in entries if e["status"] == "breach"}
    assert "update.vr_sanity_band" in breached


def test_retrieval_floor_contract() -> None:
    baseline = _load_baseline()
    retrieval_ok = {"overall": {"p@5": 0.2133}, "per_bucket": {}}
    entries = evaluate_contracts(baseline, retrieval_results=retrieval_ok)
    overall = [e for e in entries if e["name"] == "retrieval.overall_p5_floor"]
    assert overall and overall[0]["status"] == "pass"

    retrieval_bad = {"overall": {"p@5": 0.10}, "per_bucket": {}}
    entries = evaluate_contracts(baseline, retrieval_results=retrieval_bad)
    overall = [e for e in entries if e["name"] == "retrieval.overall_p5_floor"]
    assert overall and overall[0]["status"] == "breach"


# ---------------------------------------------------------------- CLI / exits


def test_main_pass_on_archived_run(tmp_path: Path) -> None:
    code = main(
        [
            "--baseline",
            str(_BASELINE),
            "--update",
            str(_ARCHIVED_RUN),
            "--mode",
            "blocking",
        ]
    )
    assert code == EXIT_PASS


def test_main_missing_results_is_infra_not_breach(tmp_path: Path) -> None:
    """A runner crash (no results file) is a measurement failure, not a
    contract breach — INFRA(3), so the workflow can warn instead of redding."""
    code = main(
        [
            "--baseline",
            str(_BASELINE),
            "--update",
            str(tmp_path / "does-not-exist.json"),
            "--mode",
            "blocking",
        ]
    )
    assert code == EXIT_INFRA


def test_main_undecodable_results_is_infra(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    code = main(["--baseline", str(_BASELINE), "--update", str(bad), "--mode", "blocking"])
    assert code == EXIT_INFRA


def test_main_requires_at_least_one_results_input() -> None:
    assert main(["--baseline", str(_BASELINE)]) == EXIT_INFRA


def test_main_required_slice_without_input_is_infra() -> None:
    """--require makes a crashed runner (no results file for that slice)
    INFRA — a missing required slice must never yield a shortened-but-green
    contract table (the exact silent-unmeasured scenario #1210 exists to
    catch)."""
    code = main(
        [
            "--baseline",
            str(_BASELINE),
            "--retrieval",
            str(_ARCHIVED_RUN),  # any readable JSON — retrieval contracts skip
            "--require",
            "update,retrieval",
        ]
    )
    assert code == EXIT_INFRA


def test_main_unknown_require_slice_is_infra() -> None:
    code = main(
        ["--baseline", str(_BASELINE), "--update", str(_ARCHIVED_RUN), "--require", "updat"]
    )
    assert code == EXIT_INFRA


def test_main_writes_breach_output_even_in_advisory_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Advisory breaches exit 0, so the workflow reads the `breach` step
    output to keep the visibility contract (auto-issue) during the soak week."""
    results = _load_archived()
    for q in results["arms"]["mc_update"]["per_query"][:3]:
        q["outcome"] = "stale_only"
        q["current_rank"] = None
        q["stale_rank"] = 1
    breached_file = tmp_path / "breached.json"
    breached_file.write_text(json.dumps(results), encoding="utf-8")
    gh_output = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(gh_output))

    code = main(
        ["--baseline", str(_BASELINE), "--update", str(breached_file), "--mode", "advisory"]
    )
    assert code == EXIT_PASS  # advisory never fails the run...
    assert "breach=true" in gh_output.read_text(encoding="utf-8")  # ...but says so

    gh_output.unlink()
    code = main(
        ["--baseline", str(_BASELINE), "--update", str(_ARCHIVED_RUN), "--mode", "advisory"]
    )
    assert code == EXIT_PASS
    assert "breach=false" in gh_output.read_text(encoding="utf-8")


def test_render_summary_is_markdown_table() -> None:
    entries = evaluate_contracts(_load_baseline(), update_results=_load_archived())
    text = render_summary(entries)
    assert "| contract |" in text
    assert "update.stale_only_zero" in text
