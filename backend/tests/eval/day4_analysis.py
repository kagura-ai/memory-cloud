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
    # Explicit raise (not _fatal) so static analysis sees every path of this
    # value-returning function terminate explicitly (PR #1177 CodeQL feedback).
    raise SystemExit(
        f"day4_analysis: embedding_model {embedding_model!r}: could not resolve an "
        f"inferential run (no run labeled {inferential_run!r}, and {len(run0)} run(s) "
        "end with '-run0'; need exactly 1 candidate)"
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


def _p5_by_query_id(run: dict[str, Any], arm: str, split: str) -> dict[str, float]:
    """Per-query P@5 for ``arm`` filtered to ``split``, keyed by ``query_id``.

    Dict insertion order mirrors ``arm``'s own filtered ``per_query`` record
    order (Python dicts preserve insertion order), so a caller can recover a
    deterministic value ordering straight from any one arm's dict via
    ``_join_by_query_id``.
    """
    return {
        rec["query_id"]: rec["p@5"]
        for rec in run["arms"][arm]["per_query"]
        if rec["split"] == split
    }


def _join_by_query_id(
    by_arm: dict[str, dict[str, float]], reference_arm: str, *, label: str, split: str
) -> dict[str, list[float]]:
    """Query_id-keyed join of ``by_arm``'s ``{query_id: value}`` dicts.

    A Task-3 review flag: pairing arm-vs-arm ``split``-filtered values by list
    POSITION is only correct when every arm's filtered records name the exact
    same query_ids in the exact same order. ``_assert_aligned`` guarantees the
    FULL (unfiltered) per_query query_id sequence matches across arms, but not
    that ``split`` labels for a given query_id agree between arms — a per-arm
    split divergence would silently misalign the heldout-filtered values that
    positional zip/dict-comprehension pairing assumed were aligned.

    Fatal (``SystemExit``), naming the FIRST query_id (in ``reference_arm``'s
    own order, or — if ``reference_arm``'s key set is otherwise a subset of
    another arm's — in that other arm's own order) present in one arm's key
    set but missing from another's, if any arm's key set differs from
    ``reference_arm``'s.

    Returns ``{arm: values}`` with every arm's values reordered to
    ``reference_arm``'s own query_id order — one deterministic shared
    ordering, derived from real query identities rather than list position.
    """
    reference_ids = list(by_arm[reference_arm])
    reference_set = set(reference_ids)
    for arm, values in by_arm.items():
        if arm == reference_arm:
            continue
        arm_set = set(values)
        if arm_set == reference_set:
            continue
        for query_id in reference_ids:
            if query_id not in arm_set:
                _fatal(
                    f"run {label!r}: split={split!r} query_id {query_id!r} present in "
                    f"{reference_arm!r} but missing from {arm!r} — heldout-filtered "
                    "arms must name the exact same query_ids for query_id-keyed pairing"
                )
        for query_id in values:
            if query_id not in reference_set:
                _fatal(
                    f"run {label!r}: split={split!r} query_id {query_id!r} present in "
                    f"{arm!r} but missing from {reference_arm!r} — heldout-filtered "
                    "arms must name the exact same query_ids for query_id-keyed pairing"
                )
    return {arm: [values[query_id] for query_id in reference_ids] for arm, values in by_arm.items()}


def _h1_verdict(mean_: float, ci_low: float, h0_reject: bool) -> dict[str, bool]:
    """H1's ``pass``/``pass_gated``/``tested`` flags from their 3 inputs.

    Split out as a small pure function (no bootstrap/omnibus computation
    inside) so the pass/gated-by-H0 interaction — in particular the
    ``pass=True, tested=False`` combination, which requires an H0 non-reject
    alongside a decisive H1 contrast and is otherwise awkward to construct
    naturally — can be unit-tested directly.
    """
    h1_pass = ci_low > 0 and mean_ >= DELTA_HYBRID
    return {"pass": h1_pass, "pass_gated": h1_pass and h0_reject, "tested": h0_reject}


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

    # Query_id-keyed (not positional) per-arm heldout values — see
    # ``_join_by_query_id`` for why positional pairing across independently
    # split-filtered arms is unsafe.
    heldout_by_query = {arm: _p5_by_query_id(inferential, arm, "heldout") for arm in GATED_ARMS}
    public_p5 = {arm: _p5(inferential, arm, "public") for arm in GATED_ARMS}
    heldout_p5 = _join_by_query_id(
        heldout_by_query, GATED_ARMS[0], label=inferential["label"], split="heldout"
    )
    n_heldout = len(heldout_p5[GATED_ARMS[0]])
    n_public = len(public_p5[GATED_ARMS[0]])

    # 1. H0 omnibus (gate). All 4 arms' heldout value lists share one ordering
    # (GATED_ARMS[0]'s query_id order, via the join above) — the permutation
    # test permutes within-index, so this alignment is load-bearing.
    h0_raw = permutation_omnibus(heldout_p5, n_permutations=N_RESAMPLES, seed=SEED)
    h0_reject = h0_raw["p_value"] < ALPHA
    h0 = {"stat": h0_raw["stat"], "p_value": h0_raw["p_value"], "reject": h0_reject}

    # 2. H1 (computed always; interpretation gated by H0). Paired query_id-
    # keyed against the hybrid arm's own filtered order (not GATED_ARMS[0]'s),
    # per the H1 contrast's own reference arm.
    keyword_mean = mean(heldout_p5["keyword"])
    semantic_mean = mean(heldout_p5["semantic"])
    best_single = "semantic" if semantic_mean > keyword_mean else "keyword"
    hybrid_vs_best_single = _join_by_query_id(
        {"hybrid": heldout_by_query["hybrid"], best_single: heldout_by_query[best_single]},
        "hybrid",
        label=inferential["label"],
        split="heldout",
    )
    h1_diffs = [
        h - b
        for h, b in zip(
            hybrid_vs_best_single["hybrid"], hybrid_vs_best_single[best_single], strict=True
        )
    ]
    h1_ci = paired_bca_ci(h1_diffs, n_resamples=N_RESAMPLES, alpha=ALPHA, seed=SEED)
    h1_verdict = _h1_verdict(h1_ci["mean"], h1_ci["ci_low"], h0_reject)
    h1 = {
        "best_single": best_single,
        "mean": h1_ci["mean"],
        "ci_low": h1_ci["ci_low"],
        "ci_high": h1_ci["ci_high"],
        "pass": h1_verdict["pass"],
        "pass_gated": h1_verdict["pass_gated"],
        "gated_by_h0": True,
        "tested": h1_verdict["tested"],
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
