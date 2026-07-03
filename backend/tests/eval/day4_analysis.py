"""Day-4 pre-declared confirmatory analysis — H0/H1/H3 verdicts (prereg-v1).

Turns N per-embedder retrieval-eval result JSONs (produced by
``tests.eval.runner``, each carrying top-level ``embedding_model``, ``label``,
and ``arms.<arm>.per_query`` — a list of ``{"query_id", "bucket", "split",
"p@5", "rr@10"}`` in corpus order) into the PRE-DECLARED hypothesis verdicts
for the Day-4 static factorial (2 embedders x 4 arms x 3 re-runs on the frozen
``kagura_L`` corpus). All statistics are imported from ``tests.eval.stats``
(Task 1) — this module does not reimplement any of them.

Pre-declared (prereg-v1 + Day-4 mini-annex, D1-D3; see ``_DESIGN_NOTES``):

- Gated family: exactly the four #967 arms — keyword, semantic, hybrid,
  hybrid_neural. The Sleep static arm is DROPPED (D1): ``recall()`` never
  reads graph edges (Issue #120), so a "hybrid_neural + Sleep" arm scored on
  the recall surface would be definitionally identical to hybrid_neural.
- Primary metric: held-out per-query P@5. Secondary: RR@10 (MRR, reported
  only, never gated).
- H0 (gate): within-query permutation omnibus over the 4 gated arms,
  10,000 permutations, seed 20260703, reject at p < 0.05.
- H1 (contrast; ALWAYS computed and reported, but only licensed to be
  interpreted as a real effect when H0 rejects — ``"gated_by_h0": true``):
  hybrid - best_single(keyword, semantic) — best_single is whichever of the
  two has the higher held-out mean P@5 (ties go to keyword), paired BCa
  bootstrap, 10,000 resamples, seed 20260703. Pass = CI lower bound > 0 AND
  point estimate >= delta_hybrid (0.05); ``pass_gated`` additionally requires
  H0 to have rejected.
- H3 (supporting): production arm (hybrid_neural) public - heldout mean-P@5
  gap, unpaired percentile bootstrap, 10,000 resamples, seed 20260703. Within
  the leak tolerance when the gap <= delta_leak (0.05). ``None`` when the
  corpus carries no public queries (nothing to compute a gap against).
- sigma_d / achieved_power: sample SD of the H1 per-query paired diffs and
  the closed-form two-sided normal-approximation power at delta_hybrid, for
  the prereg's Appendix A power re-estimation stamp.
- Re-runs: each embedder gets (nominally) 3 identical-invocation runs; ONE is
  pre-declared the inferential run (used for every inferential statistic
  above — see ``_resolve_inferential``); every run (including non-inferential
  ones) contributes to the descriptive ``arm_means_across_runs`` (min/median/
  max of held-out mean P@5 per arm).

Pure stdlib + ``tests.eval.stats`` only — no app/src imports — so this CLI
runs locally without the backend stack (DB, Qdrant, LiteLLM, ...).

Usage:
    PYTHONPATH=src:. python -m tests.eval.day4_analysis \\
        --results results/day4-qwen3-embedding-0.6b-run0-2026-07-05.json ... \\
        --inferential-run day4-qwen3-embedding-0.6b-run0 \\
        [--out results/day4-analysis-2026-07-05.json]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from itertools import zip_longest
from pathlib import Path
from statistics import mean, median
from typing import Any, NoReturn

from tests.eval.stats import (
    achieved_power,
    paired_bca_ci,
    permutation_omnibus,
    sigma_d,
    unpaired_percentile_ci_diff,
)

#: The four pre-registered arms (#967); order is fixed — it drives the H0
#: omnibus dict key order and the H1 best_single tie-break (keyword first).
GATED_ARMS: tuple[str, ...] = ("keyword", "semantic", "hybrid", "hybrid_neural")
#: Shared seed for every resampling procedure below — one seed for the whole
#: confirmatory analysis, per the prereg (byte-reproducible across machines).
SEED = 20260703
N_RESAMPLES = 10_000
ALPHA = 0.05
#: H1 pass threshold: hybrid must beat best_single by at least this much.
DELTA_HYBRID = 0.05
#: H3 leak tolerance: the public - heldout mean-P@5 gap must not exceed this.
DELTA_LEAK = 0.05
#: The arm mirrored to production; H3's subject.
PRODUCTION_ARM = "hybrid_neural"

_RESULTS_DIR = Path(__file__).resolve().parent / "results"

_DESIGN_NOTES: tuple[str, ...] = (
    "D1: sleep static arm dropped — recall() never reads graph edges (Issue #120); "
    "gated family = the prereg's exact 4 arms.",
    "D2: qwen3-embedding:0.6b = inferential/primary; qwen3-embedding:4b = replication "
    "(descriptive).",
    "D3: permutation omnibus + paired BCa (10k, seed 20260703); run 0 inferential; "
    "3 re-runs min/median/max.",
)


def _fatal(message: str) -> NoReturn:
    """Fail loud with a prefixed, actionable message (never a bare traceback)."""
    raise SystemExit(f"day4_analysis: {message}")


def _load_run(path: Path) -> dict[str, Any]:
    """Load and structurally validate one result JSON.

    Fatal (``SystemExit``) if the file cannot be parsed, lacks
    ``embedding_model``/``label``, or any gated arm is missing its
    ``per_query`` list — every downstream statistic assumes this shape.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _fatal(f"{path}: cannot read/parse as JSON ({exc})")

    embedding_model = data.get("embedding_model")
    label = data.get("label")
    if not embedding_model:
        _fatal(f"{path}: missing 'embedding_model'")
    if not label:
        _fatal(f"{path}: missing 'label'")

    arms = data.get("arms") or {}
    for arm in GATED_ARMS:
        arm_block = arms.get(arm)
        if not isinstance(arm_block, dict) or "per_query" not in arm_block:
            _fatal(
                f"{path}: gated arm {arm!r} is missing 'per_query' (label={label!r}) — "
                f"every gated arm must carry per-query records"
            )

    return {"path": str(path), "embedding_model": embedding_model, "label": label, "arms": arms}


def _resolve_inferential(
    embedding_model: str, group: list[dict[str, Any]], inferential_run: str
) -> dict[str, Any]:
    """Pick the one inferential run for ``embedding_model``'s group.

    The run whose ``label`` equals ``--inferential-run`` is the inferential
    run for its own embedding_model group, when present; every OTHER group
    (embedding_model doesn't match) falls back to the run whose label ends
    with ``-run0``. Either resolution must yield EXACTLY one candidate — 0 or
    >1 is a fatal, named ambiguity (never silently guessed).
    """
    exact = [r for r in group if r["label"] == inferential_run]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        _fatal(
            f"embedding_model {embedding_model!r}: {len(exact)} runs share the label "
            f"{inferential_run!r} — ambiguous inferential-run resolution"
        )

    run0 = [r for r in group if r["label"].endswith("-run0")]
    if len(run0) == 1:
        return run0[0]
    _fatal(
        f"embedding_model {embedding_model!r}: could not resolve an inferential run "
        f"(no run labeled {inferential_run!r}, and {len(run0)} run(s) end with '-run0'; "
        "need exactly 1 candidate)"
    )


def _assert_aligned(run: dict[str, Any]) -> None:
    """Assert the 4 gated arms' per_query lists share one query_id sequence.

    Runs BEFORE any statistic is computed on the inferential run (a Task-1
    review flag): paired/omnibus statistics silently misattribute results if
    the arms are not aligned query-for-query. Fatal, naming the FIRST
    mismatching arm/index, otherwise.
    """
    label = run["label"]
    reference_arm = GATED_ARMS[0]
    reference_ids = [rec["query_id"] for rec in run["arms"][reference_arm]["per_query"]]
    for arm in GATED_ARMS[1:]:
        ids = [rec["query_id"] for rec in run["arms"][arm]["per_query"]]
        if ids == reference_ids:
            continue
        for i, (expected, actual) in enumerate(zip_longest(reference_ids, ids)):
            if expected != actual:
                _fatal(
                    f"run {label!r}: per_query query_id mismatch between "
                    f"{reference_arm!r} and {arm!r} at index {i}: {expected!r} != {actual!r}"
                )


def _p5(run: dict[str, Any], arm: str, split: str) -> list[float]:
    """Per-query P@5 values for ``arm``, filtered to ``split``, in order."""
    return [rec["p@5"] for rec in run["arms"][arm]["per_query"] if rec["split"] == split]


def _rr10(run: dict[str, Any], arm: str, split: str) -> list[float]:
    """Per-query RR@10 values for ``arm``, filtered to ``split``, in order."""
    return [rec["rr@10"] for rec in run["arms"][arm]["per_query"] if rec["split"] == split]


def _analyze_group(
    embedding_model: str, group: list[dict[str, Any]], inferential_run_label: str
) -> dict[str, Any]:
    """The full H0/H1/H3 + power + cross-run verdict block for one embedder."""
    if len(group) != 3:
        print(
            f"day4_analysis: WARNING embedding_model {embedding_model!r} has "
            f"{len(group)} run(s) (expected 3)",
            file=sys.stderr,
        )

    run_labels = sorted(r["label"] for r in group)
    inferential = _resolve_inferential(embedding_model, group, inferential_run_label)
    _assert_aligned(inferential)

    heldout_p5 = {arm: _p5(inferential, arm, "heldout") for arm in GATED_ARMS}
    public_p5 = {arm: _p5(inferential, arm, "public") for arm in GATED_ARMS}
    n_heldout = len(heldout_p5[GATED_ARMS[0]])
    n_public = len(public_p5[GATED_ARMS[0]])

    # 1. H0 omnibus (gate).
    h0_raw = permutation_omnibus(heldout_p5, n_permutations=N_RESAMPLES, seed=SEED)
    h0_reject = h0_raw["p_value"] < ALPHA
    h0 = {"stat": h0_raw["stat"], "p_value": h0_raw["p_value"], "reject": h0_reject}

    # 2. H1 (computed always; interpretation gated by H0).
    keyword_mean = mean(heldout_p5["keyword"])
    semantic_mean = mean(heldout_p5["semantic"])
    best_single = "semantic" if semantic_mean > keyword_mean else "keyword"
    h1_diffs = [h - b for h, b in zip(heldout_p5["hybrid"], heldout_p5[best_single], strict=True)]
    h1_ci = paired_bca_ci(h1_diffs, n_resamples=N_RESAMPLES, alpha=ALPHA, seed=SEED)
    h1_pass = h1_ci["ci_low"] > 0 and h1_ci["mean"] >= DELTA_HYBRID
    h1 = {
        "best_single": best_single,
        "mean": h1_ci["mean"],
        "ci_low": h1_ci["ci_low"],
        "ci_high": h1_ci["ci_high"],
        "pass": h1_pass,
        "pass_gated": h1_pass and h0_reject,
        "gated_by_h0": True,
        "tested": h0_reject,
    }

    # 3. H3 (supporting) — production-arm public/heldout leak gap.
    if n_public == 0:
        print(
            f"day4_analysis: h3 skipped for embedding_model {embedding_model!r} — "
            "inferential run has 0 public queries",
            file=sys.stderr,
        )
        h3: dict[str, Any] | None = None
    else:
        h3_ci = unpaired_percentile_ci_diff(
            public_p5[PRODUCTION_ARM],
            heldout_p5[PRODUCTION_ARM],
            n_resamples=N_RESAMPLES,
            alpha=ALPHA,
            seed=SEED,
        )
        h3 = {
            "gap_mean": h3_ci["mean"],
            "ci_low": h3_ci["ci_low"],
            "ci_high": h3_ci["ci_high"],
            "within_delta_leak": h3_ci["mean"] <= DELTA_LEAK,
        }

    # 4. sigma_d + achieved power, from the H1 per-query paired diffs.
    sd = sigma_d(h1_diffs)
    power = achieved_power(DELTA_HYBRID, sd, len(h1_diffs), alpha=ALPHA)

    # 5. Across runs (all runs of the group): held-out mean P@5 per arm.
    arm_means_across_runs: dict[str, dict[str, float]] = {}
    for arm in GATED_ARMS:
        means = [mean(_p5(r, arm, "heldout")) for r in group]
        arm_means_across_runs[arm] = {"min": min(means), "median": median(means), "max": max(means)}

    # 6. MRR secondary (inferential run, held-out, report only).
    mrr_secondary = {arm: mean(_rr10(inferential, arm, "heldout")) for arm in GATED_ARMS}

    return {
        "run_labels": run_labels,
        "inferential_run": inferential["label"],
        "n_heldout": n_heldout,
        "n_public": n_public,
        "h0": h0,
        "h1": h1,
        "h3": h3,
        "sigma_d": sd,
        "achieved_power": {"delta": DELTA_HYBRID, "n": len(h1_diffs), "power": power},
        "arm_means_across_runs": arm_means_across_runs,
        "mrr_secondary": mrr_secondary,
    }


def _round_floats(obj: Any, ndigits: int = 4) -> Any:
    """Recursively round every float in a JSON-shaped structure to ``ndigits``.

    ``bool`` is an ``int`` subclass but never a ``float``, so the
    ``isinstance(obj, float)`` check never conflates a boolean flag (e.g.
    ``reject``, ``pass``) with a numeric value.
    """
    if isinstance(obj, float):
        return round(obj, ndigits)
    if isinstance(obj, dict):
        return {k: _round_floats(v, ndigits) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_round_floats(v, ndigits) for v in obj]
    return obj


def analyze(paths: list[Path], inferential_run: str) -> dict[str, Any]:
    """Turn N result JSONs into the pre-declared H0/H1/H3 verdicts.

    Groups the inputs by ``embedding_model``; every group is analyzed
    independently, using ONLY its resolved inferential run for inference (see
    ``_resolve_inferential``). Fatal (``SystemExit``) on any structural
    defect: a missing ``per_query``, a missing ``embedding_model``/``label``,
    an unaligned per-query query_id sequence, or an ambiguous inferential-run
    resolution. A group with != 3 runs is a WARNING (stderr) only — the
    analysis must still work when re-run counts change (e.g. mid-flight,
    before all re-runs exist).
    """
    runs = [_load_run(Path(p)) for p in paths]

    groups: dict[str, list[dict[str, Any]]] = {}
    for run in runs:
        groups.setdefault(run["embedding_model"], []).append(run)

    per_embedder = {
        embedding_model: _analyze_group(embedding_model, group, inferential_run)
        for embedding_model, group in groups.items()
    }

    result: dict[str, Any] = {
        "run_date": datetime.now(UTC).strftime("%Y-%m-%d"),
        "experiment": "day4-static-factorial",
        "seed": SEED,
        "n_resamples": N_RESAMPLES,
        "alpha": ALPHA,
        "delta_hybrid": DELTA_HYBRID,
        "delta_leak": DELTA_LEAK,
        "gated_arms": list(GATED_ARMS),
        "per_embedder": per_embedder,
        "design_notes": list(_DESIGN_NOTES),
    }
    return _round_floats(result)


def _main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--results",
        nargs="+",
        required=True,
        type=Path,
        help="result JSON path(s) to analyze (2 embedders x 3 re-runs, typically 6 files)",
    )
    ap.add_argument(
        "--inferential-run",
        dest="inferential_run",
        required=True,
        help="label of the pre-declared inferential run, e.g. day4-qwen3-embedding-0.6b-run0",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=None,
        help="output path (default: results/day4-analysis-<run_date>.json)",
    )
    args = ap.parse_args()

    result = analyze(args.results, args.inferential_run)

    out_path = args.out or (_RESULTS_DIR / f"day4-analysis-{result['run_date']}.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"day4_analysis: wrote {out_path}", file=sys.stderr)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
