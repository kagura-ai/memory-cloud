"""Day-5 pre-declared confirmatory analysis — the H4 verdict CLI (prereg-v1 F3).

Turns N update-correctness result JSONs (produced by ``tests.eval.update_runner``,
each carrying top-level ``label`` and ``arms.<arm>.per_query`` — a list of
``{"query_id", "outcome", "current_rank", "stale_rank"}`` in corpus order, for
the three arms ``vanilla_rag`` / ``mc_update`` / ``mc_prod``) into the
PRE-DECLARED H4 verdict for the Day-5 update-correctness experiment (a single
longitudinal update-slice corpus, nominally 3 identical re-runs — the
judge-LLM makes the mc_update arm nondeterministic). All statistics are
imported from ``tests.eval.stats`` (Task 1) — this module does not reimplement
any of them.

Pre-declared (prereg-v1 H4 + Day-5 design doc D1/D3-D5; see ``_DESIGN_NOTES``):

- Gated family: exactly the H4 conditional contrast, ``mc_update`` vs
  ``vanilla_rag`` — the two arms that differ ONLY in update machinery (D1).
  ``mc_prod`` (production posture: hybrid + neural) is supporting only, never
  gated.
- Outcome classification (D4), per query per arm: ``current_over_stale`` |
  ``stale_over_current`` | ``current_only`` | ``stale_only`` | ``neither``.
- H4 (gated): "complete pairs" are queries where BOTH ``vanilla_rag`` AND
  ``mc_update`` land in {current_over_stale, stale_over_current} (both docs
  retrievable in top-k). ``correct = 1`` iff the outcome is
  ``current_over_stale``, else 0. diffs = mc_update_correct - vanilla_rag_correct
  per complete-pair query (query_id-keyed join), paired BCa bootstrap (10,000
  resamples, seed 20260703). Pass = CI lower bound > 0 AND point estimate >=
  delta_update (0.15). Underpowered (reported, not a separate gate) when
  complete pairs < 25 (half of N_update = 50) — no post-hoc delta change.
- Decomposition (all three arms, inferential run): counts + rates of the 5
  outcomes over every update query. Dedup REMOVAL of the stale doc lands in
  ``current_only`` — "update-by-removal", a success mode outside the gated
  conditional, reported not folded in (D4).
- Supporting `update_success@10` (all three arms, inferential run,
  unconditional over every update query): success = 1 iff current_rank is not
  None AND (stale_rank is None OR current_rank < stale_rank). For
  ``mc_update`` and ``mc_prod``: paired BCa of (arm - vanilla_rag), same seed,
  over ALL update queries (query_id join) — explicitly keyed ``supporting``,
  never gated.
- sigma_d / achieved_power: sample SD of the H4 complete-pair diffs and the
  closed-form two-sided normal-approximation power at delta_update, for the
  prereg's power re-estimation (D7 — reported from the realized run, not
  re-tuned).
- Re-runs (D5): exactly 3 identical invocations nominally; run 0 is the
  inferential run (pre-declared) used for H4/decomposition/supporting/power
  above; every run (including non-inferential ones) contributes to the
  descriptive ``across_runs`` (min/median/max of the gated conditional mean
  diff, and of each arm's update_success@10 mean).

Pure stdlib + ``tests.eval.stats`` only — no app/src imports — so this CLI
runs locally without the backend stack (DB, Qdrant, LiteLLM, ...).

Usage:
    PYTHONPATH=src:. python -m tests.eval.day5_analysis \\
        --results results/day5-update-run0-2026-07-06.json ... \\
        --inferential-run day5-update-run0 \\
        [--out results/day5-analysis-2026-07-06.json]
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

from tests.eval.stats import achieved_power, paired_bca_ci, sigma_d

# Pre-declared constants (prereg-v1 H4 / Day-5 design doc) — bake in, do not
# change without a corresponding prereg amendment. Shared byte-for-byte with
# ``tests.eval.update_runner``'s own copy (this module does not import that
# one — it must stay importable with no app/src stack on the path).
K = 10
SEED = 20260703
N_RESAMPLES = 10_000
ALPHA = 0.05
DELTA_UPDATE = 0.15
MIN_COMPLETE_PAIRS = 25

#: Arm names. Order is fixed — it drives the alignment-guard reference arm
#: (``vanilla_rag`` first) and the decomposition/supporting dict key order.
ARM_VANILLA_RAG = "vanilla_rag"
ARM_MC_UPDATE = "mc_update"
ARM_MC_PROD = "mc_prod"
ARMS: tuple[str, ...] = (ARM_VANILLA_RAG, ARM_MC_UPDATE, ARM_MC_PROD)

#: The five possible per-query outcomes (``update_runner.classify_update_outcome``).
OUTCOME_CURRENT_OVER_STALE = "current_over_stale"
OUTCOME_STALE_OVER_CURRENT = "stale_over_current"
OUTCOME_CURRENT_ONLY = "current_only"
OUTCOME_STALE_ONLY = "stale_only"
OUTCOME_NEITHER = "neither"
ALL_OUTCOMES: tuple[str, ...] = (
    OUTCOME_CURRENT_OVER_STALE,
    OUTCOME_STALE_OVER_CURRENT,
    OUTCOME_CURRENT_ONLY,
    OUTCOME_STALE_ONLY,
    OUTCOME_NEITHER,
)
#: "Both retrievable in top-k" — the H4 gated conditional's complete-pair set.
DETERMINATE_OUTCOMES = frozenset({OUTCOME_CURRENT_OVER_STALE, OUTCOME_STALE_OVER_CURRENT})

_RESULTS_DIR = Path(__file__).resolve().parent / "results"

_DESIGN_NOTES: tuple[str, ...] = (
    "D1: gated arms differ ONLY in update machinery -- vanilla_rag (sleep_mode=skip, "
    "reinforce_enabled=False, neural off, search_mode=semantic) vs mc_update "
    "(sleep_mode=full, one Sleep full pass w/ judge-LLM as configured, "
    "reinforce_enabled=True, search_mode=semantic); mc_prod (hybrid + neural, production "
    "posture) is supporting only, scored after mc_update so its Hebbian writes cannot "
    "contaminate the gated pass.",
    "D3: longitudinal simulation -- every stale (-v1) memory's created_at/updated_at "
    "backdated 30 days in BOTH contexts before scoring; a seconds-apart ingest would make "
    "every recency mechanism inert by construction.",
    "D4: gated conditional = complete pairs (both arms' outcome in {current_over_stale, "
    "stale_over_current}); diffs = mc_update_correct - vanilla_rag_correct, paired BCa "
    "(10k resamples, seed 20260703) on the mean diff; pass = ci_low > 0 AND mean >= "
    "delta_update (0.15); underpowered if complete pairs < 25; dedup removal of the stale "
    "doc lands in current_only ('update-by-removal') -- reported, not gated.",
)


def _fatal(message: str) -> NoReturn:
    """Fail loud with a prefixed, actionable message (never a bare traceback)."""
    raise SystemExit(f"day5_analysis: {message}")


def _load_run(path: Path) -> dict[str, Any]:
    """Load and structurally validate one result JSON.

    Fatal (``SystemExit``) if the file cannot be parsed, lacks ``label``, or
    any of the 3 arms is missing its ``per_query`` list — every downstream
    statistic assumes this shape.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _fatal(f"{path}: cannot read/parse as JSON ({exc})")

    label = data.get("label")
    if not label:
        _fatal(f"{path}: missing 'label'")

    arms = data.get("arms") or {}
    for arm in ARMS:
        arm_block = arms.get(arm)
        if not isinstance(arm_block, dict) or "per_query" not in arm_block:
            _fatal(
                f"{path}: arm {arm!r} is missing 'per_query' (label={label!r}) — "
                f"every arm must carry per-query records"
            )

    return {"path": str(path), "label": label, "arms": arms}


def _resolve_inferential(runs: list[dict[str, Any]], inferential_run: str) -> dict[str, Any]:
    """Pick the one pre-declared inferential run out of ``runs``.

    The run whose ``label`` equals ``--inferential-run`` is inferential when
    present; otherwise the run whose label ends with ``-run0`` is used. Either
    resolution must yield EXACTLY one candidate — 0 or >1 is a fatal, named
    ambiguity (never silently guessed).
    """
    exact = [r for r in runs if r["label"] == inferential_run]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        _fatal(
            f"{len(exact)} runs share the label {inferential_run!r} — ambiguous "
            "inferential-run resolution"
        )

    run0 = [r for r in runs if r["label"].endswith("-run0")]
    if len(run0) == 1:
        return run0[0]
    _fatal(
        f"could not resolve an inferential run (no run labeled {inferential_run!r}, and "
        f"{len(run0)} run(s) end with '-run0'; need exactly 1 candidate)"
    )


def _assert_aligned(run: dict[str, Any]) -> None:
    """Assert the 3 arms' per_query lists share one query_id sequence.

    Runs BEFORE any statistic is computed on the inferential run: paired
    statistics silently misattribute results if the arms are not aligned
    query-for-query. Fatal, naming the FIRST mismatching arm/index, otherwise.
    """
    label = run["label"]
    reference_arm = ARMS[0]
    reference_ids = [rec["query_id"] for rec in run["arms"][reference_arm]["per_query"]]
    for arm in ARMS[1:]:
        ids = [rec["query_id"] for rec in run["arms"][arm]["per_query"]]
        if ids == reference_ids:
            continue
        for i, (expected, actual) in enumerate(zip_longest(reference_ids, ids)):
            if expected != actual:
                _fatal(
                    f"run {label!r}: per_query query_id mismatch between "
                    f"{reference_arm!r} and {arm!r} at index {i}: {expected!r} != {actual!r}"
                )


def _outcome_by_query_id(run: dict[str, Any], arm: str) -> dict[str, str]:
    """Per-query ``outcome`` for ``arm``, keyed by query_id (dict order mirrors
    the arm's own per_query record order)."""
    return {rec["query_id"]: rec["outcome"] for rec in run["arms"][arm]["per_query"]}


def is_update_success(current_rank: int | None, stale_rank: int | None) -> bool:
    """The supporting ``update_success@10`` predicate (D4's unconditional metric).

    Success iff the current doc is retrieved in the top-k AND (the stale doc
    is absent OR ranked below the current doc). Factored out as a small pure
    function — the same predicate/name pairing is easy to get backwards
    (``<`` vs ``<=``, which rank "wins") and this is unit-tested directly.
    """
    return current_rank is not None and (stale_rank is None or current_rank < stale_rank)


def _success_by_query_id(run: dict[str, Any], arm: str) -> dict[str, int]:
    """Per-query ``update_success@10`` (0/1) for ``arm``, keyed by query_id."""
    return {
        rec["query_id"]: int(is_update_success(rec["current_rank"], rec["stale_rank"]))
        for rec in run["arms"][arm]["per_query"]
    }


def _join_by_query_id(
    by_arm: dict[str, dict[str, Any]], reference_arm: str, *, label: str
) -> dict[str, list[Any]]:
    """Query_id-keyed join of ``by_arm``'s ``{query_id: value}`` dicts.

    Pairing arm-vs-arm values by list POSITION is only correct when every
    arm's records name the exact same query_ids in the exact same order;
    ``_assert_aligned`` guarantees this for the full per_query sequence, but a
    caller building a filtered/derived per-arm dict (e.g. only complete-pair
    query_ids) needs its own guarantee — this is that guarantee, applied
    generically to whatever value type the caller extracted (``str`` outcome,
    ``int`` success, ...).

    Fatal (``SystemExit``), naming the FIRST query_id present in one arm's key
    set but missing from another's, if any arm's key set differs from
    ``reference_arm``'s. Returns ``{arm: values}`` with every arm's values
    reordered to ``reference_arm``'s own query_id order.
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
                    f"run {label!r}: query_id {query_id!r} present in {reference_arm!r} "
                    f"but missing from {arm!r} — arms must name the exact same query_ids "
                    "for query_id-keyed pairing"
                )
        for query_id in values:
            if query_id not in reference_set:
                _fatal(
                    f"run {label!r}: query_id {query_id!r} present in {arm!r} but missing "
                    f"from {reference_arm!r} — arms must name the exact same query_ids for "
                    "query_id-keyed pairing"
                )
    return {arm: [values[query_id] for query_id in reference_ids] for arm, values in by_arm.items()}


def _correct(outcome: str) -> int:
    """H4's binary correctness indicator: 1 iff current ranked over stale."""
    return 1 if outcome == OUTCOME_CURRENT_OVER_STALE else 0


def _gated_diffs(run: dict[str, Any]) -> list[int]:
    """Per-complete-pair (mc_update_correct - vanilla_rag_correct) diffs for
    ``run``, query_id-keyed and restricted to complete pairs (both arms'
    outcome in ``DETERMINATE_OUTCOMES``).

    Fatal if ``run`` has 0 complete pairs — there is nothing to bootstrap
    (mirrors ``paired_bca_ci``'s own empty-input guard, but named to this
    run/label rather than a bare stdlib traceback).
    """
    label = run["label"]
    outcomes_by_arm = {
        ARM_VANILLA_RAG: _outcome_by_query_id(run, ARM_VANILLA_RAG),
        ARM_MC_UPDATE: _outcome_by_query_id(run, ARM_MC_UPDATE),
    }
    joined = _join_by_query_id(outcomes_by_arm, ARM_VANILLA_RAG, label=label)
    diffs = [
        _correct(mc_o) - _correct(vr_o)
        for vr_o, mc_o in zip(joined[ARM_VANILLA_RAG], joined[ARM_MC_UPDATE], strict=True)
        if vr_o in DETERMINATE_OUTCOMES and mc_o in DETERMINATE_OUTCOMES
    ]
    if not diffs:
        _fatal(
            f"run {label!r}: 0 complete pairs (both {ARM_VANILLA_RAG!r} and "
            f"{ARM_MC_UPDATE!r} determinate) — cannot compute the gated H4 conditional"
        )
    return diffs


def _h4_block(inferential: dict[str, Any]) -> tuple[dict[str, Any], list[int]]:
    """The H4 gated-conditional verdict block + its underlying diffs (also
    the input to ``sigma_d``/``achieved_power``)."""
    diffs = _gated_diffs(inferential)
    n_complete_pairs = len(diffs)
    ci = paired_bca_ci(diffs, n_resamples=N_RESAMPLES, alpha=ALPHA, seed=SEED)
    h4 = {
        "n_complete_pairs": n_complete_pairs,
        "mean": ci["mean"],
        "ci_low": ci["ci_low"],
        "ci_high": ci["ci_high"],
        "pass": ci["ci_low"] > 0 and ci["mean"] >= DELTA_UPDATE,
        "underpowered": n_complete_pairs < MIN_COMPLETE_PAIRS,
    }
    return h4, diffs


def _decomposition(inferential: dict[str, Any]) -> dict[str, Any]:
    """Per-arm 5-way outcome counts + rates over every update query."""
    decomposition: dict[str, Any] = {}
    for arm in ARMS:
        records = inferential["arms"][arm]["per_query"]
        n = len(records)
        counts = dict.fromkeys(ALL_OUTCOMES, 0)
        for rec in records:
            counts[rec["outcome"]] = counts.get(rec["outcome"], 0) + 1
        rates = {outcome: (counts[outcome] / n if n else 0.0) for outcome in ALL_OUTCOMES}
        decomposition[arm] = {"n": n, "counts": counts, "rates": rates}
    return decomposition


def _supporting_block(inferential: dict[str, Any]) -> dict[str, Any]:
    """The supporting (not gated) ``update_success@10`` block: per-arm rate
    over ALL update queries, plus paired BCa of (mc_update/mc_prod -
    vanilla_rag)."""
    label = inferential["label"]
    success_by_arm = {arm: _success_by_query_id(inferential, arm) for arm in ARMS}
    joined = _join_by_query_id(success_by_arm, ARM_VANILLA_RAG, label=label)

    # float(mean(...)): ``joined[arm]`` is 0/1 ints, and ``statistics.mean``
    # returns a plain ``int`` (not ``float``) when the division happens to be
    # exact (e.g. all-1s) -- forcing float keeps this field's JSON type
    # consistent across queries/runs instead of silently flipping between
    # ``1`` and ``0.8333`` depending on the data.
    update_success_at_10 = {
        arm: {"mean": float(mean(joined[arm])), "n": len(joined[arm])} for arm in ARMS
    }

    vs_vanilla_rag: dict[str, Any] = {}
    for arm in (ARM_MC_UPDATE, ARM_MC_PROD):
        diffs = [a - v for a, v in zip(joined[arm], joined[ARM_VANILLA_RAG], strict=True)]
        ci = paired_bca_ci(diffs, n_resamples=N_RESAMPLES, alpha=ALPHA, seed=SEED)
        vs_vanilla_rag[arm] = {
            "mean": ci["mean"],
            "ci_low": ci["ci_low"],
            "ci_high": ci["ci_high"],
            "n": ci["n"],
        }

    return {"update_success_at_10": update_success_at_10, "vs_vanilla_rag": vs_vanilla_rag}


def _across_runs(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Descriptive min/median/max across ALL runs (not just inferential): the
    gated conditional's mean diff, and each arm's update_success@10 mean."""
    # float(mean(...)): same int/float consistency concern as
    # ``_supporting_block`` above -- these are means of -1/0/1 and 0/1 ints.
    gated_means = [float(mean(_gated_diffs(r))) for r in runs]
    success_means: dict[str, list[float]] = {arm: [] for arm in ARMS}
    for r in runs:
        for arm in ARMS:
            success_means[arm].append(float(mean(_success_by_query_id(r, arm).values())))

    return {
        "gated_conditional_mean_diff": {
            "min": min(gated_means),
            "median": median(gated_means),
            "max": max(gated_means),
        },
        "update_success_at_10": {
            arm: {"min": min(values), "median": median(values), "max": max(values)}
            for arm, values in success_means.items()
        },
    }


def _round_floats(obj: Any, ndigits: int = 4) -> Any:
    """Recursively round every float in a JSON-shaped structure to ``ndigits``.

    ``bool`` is an ``int`` subclass but never a ``float``, so the
    ``isinstance(obj, float)`` check never conflates a boolean flag (e.g.
    ``pass``, ``underpowered``) with a numeric value.
    """
    if isinstance(obj, float):
        return round(obj, ndigits)
    if isinstance(obj, dict):
        return {k: _round_floats(v, ndigits) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_round_floats(v, ndigits) for v in obj]
    return obj


def analyze(paths: list[Path], inferential_run: str) -> dict[str, Any]:
    """Turn N result JSONs into the pre-declared H4 verdict.

    Uses ONLY the resolved inferential run for the H4/decomposition/supporting/
    power blocks (see ``_resolve_inferential``); every run (including the
    inferential one) contributes to ``across_runs``. Fatal (``SystemExit``) on
    any structural defect: a missing ``per_query``/``label``, an unaligned
    per-query query_id sequence, an ambiguous inferential-run resolution, or 0
    complete pairs in any run. A run count != 3 is a WARNING (stderr) only —
    the analysis must still work mid-flight, before all re-runs exist.
    """
    runs = [_load_run(Path(p)) for p in paths]

    if len(runs) != 3:
        print(
            f"day5_analysis: WARNING {len(runs)} run(s) provided (expected 3)",
            file=sys.stderr,
        )

    run_labels = sorted(r["label"] for r in runs)
    inferential = _resolve_inferential(runs, inferential_run)
    _assert_aligned(inferential)

    h4, gated_diffs = _h4_block(inferential)
    decomposition = _decomposition(inferential)
    supporting = _supporting_block(inferential)
    sd = sigma_d(gated_diffs)
    power = achieved_power(DELTA_UPDATE, sd, len(gated_diffs), alpha=ALPHA)
    across_runs = _across_runs(runs)

    result: dict[str, Any] = {
        "run_date": datetime.now(UTC).strftime("%Y-%m-%d"),
        "experiment": "day5-update-correctness",
        "seed": SEED,
        "n_resamples": N_RESAMPLES,
        "alpha": ALPHA,
        "delta_update": DELTA_UPDATE,
        "k": K,
        "run_labels": run_labels,
        "inferential_run": inferential["label"],
        "h4": h4,
        "decomposition": decomposition,
        "supporting": supporting,
        "sigma_d": sd,
        "achieved_power": {"delta": DELTA_UPDATE, "n": len(gated_diffs), "power": power},
        "across_runs": across_runs,
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
        help="result JSON path(s) to analyze (nominally 3 identical re-runs)",
    )
    ap.add_argument(
        "--inferential-run",
        dest="inferential_run",
        required=True,
        help="label of the pre-declared inferential run, e.g. day5-update-run0",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=None,
        help="output path (default: results/day5-analysis-<run_date>.json)",
    )
    args = ap.parse_args()

    result = analyze(args.results, args.inferential_run)

    out_path = args.out or (_RESULTS_DIR / f"day5-analysis-{result['run_date']}.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"day5_analysis: wrote {out_path}", file=sys.stderr)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
