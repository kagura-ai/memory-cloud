"""CI contract gates over live eval results (#1210) — claims become contracts.

The kagura-memory-eval program's headline numbers (update_success@10 0.92-0.96
vs vanilla 0.54-0.56, ``stale_only`` 0/50 post-#1198) were protected by nothing
but memory. This module turns them into standing contracts a scheduled
workflow evaluates every night against fresh live-run results.

Three-value exit model (COO gate1 design for #1210):

- ``EXIT_PASS`` (0) — every evaluated contract holds (or mode is advisory).
- ``EXIT_BREACH`` (1) — a contract is violated. This is the only red.
- ``EXIT_INFRA`` (3) — the measurement itself is unavailable (results file
  missing/undecodable — typically an upstream runner crash or provider
  outage). The workflow warns and files an infra issue instead of redding:
  "we could not measure" must never masquerade as "the contract holds" OR
  as "the contract broke".

Contract semantics:

- ``update.stale_only_zero`` — the worst failure mode (judged merge deletes
  the CURRENT fact, #1195) stays extinct. HARD even with a flaky judge: the
  #1198 ``_enforce_newer_wins`` veto is deterministic regardless of which
  winner the judge proposes.
- ``update.mc_update_success_floor`` — the headline metric stays above the
  minimum observed across archived judged runs (baseline governance: see
  ``fixtures/ci_baseline.json``).
- ``update.vr_sanity_band`` — the no-update-path vanilla arm stays a coin
  flip; drift outside the band means the harness or corpus broke, not the
  product.
- ``update.llm_call_failures_zero`` — silent judge death (#1177) cannot hide
  behind a green run. SKIPs (never passes vacuously) when the field is
  absent (pre-#1183 result files).
- ``retrieval.overall_p5_floor`` — golden-corpus drift signal with a jitter
  margin (absolute numbers are drift signals, not quality claims — #344).

Fields absent from a results file SKIP their contract with a note; a skip is
surfaced in the summary so a schema regression cannot silently disable a
contract.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

EXIT_PASS = 0
EXIT_BREACH = 1
EXIT_INFRA = 3

_SUCCESS_OUTCOMES = {"current_over_stale", "current_only"}


def derive_update_metrics(results: dict[str, Any]) -> dict[str, Any]:
    """Aggregate an update-runner results dict into per-arm contract inputs.

    Mirrors the tech report's definitions:

    - ``update_success_at_k``: current retrieved with no stale above it
      (``current_over_stale`` + ``current_only``) / N — the unconditional
      supporting metric.
    - ``conditional_rate``: current_over_stale / both-retrievable pairs — the
      pre-registered H4 estimand (None when no pair is both-retrievable).
    """
    arms: dict[str, Any] = {}
    for arm_name, block in results.get("arms", {}).items():
        per_query = block.get("per_query", [])
        n = len(per_query)
        outcomes: dict[str, int] = {}
        for q in per_query:
            outcomes[q["outcome"]] = outcomes.get(q["outcome"], 0) + 1
        both = outcomes.get("current_over_stale", 0) + outcomes.get("stale_over_current", 0)
        arms[arm_name] = {
            "n": n,
            "outcomes": {
                key: outcomes.get(key, 0)
                for key in (
                    "current_over_stale",
                    "stale_over_current",
                    "current_only",
                    "stale_only",
                    "neither",
                )
            },
            "update_success_at_k": (
                sum(outcomes.get(o, 0) for o in _SUCCESS_OUTCOMES) / n if n else None
            ),
            "conditional_rate": (outcomes.get("current_over_stale", 0) / both if both else None),
        }
    sleep_summary = results.get("sleep_summary") or {}
    return {
        "arms": arms,
        "llm_call_failures_total": sleep_summary.get("llm_call_failures_total"),
        "k": results.get("k"),
    }


def _entry(name: str, status: str, observed: Any, bound: Any, note: str = "") -> dict[str, Any]:
    return {"name": name, "status": status, "observed": observed, "bound": bound, "note": note}


def evaluate_contracts(
    baseline: dict[str, Any],
    update_results: dict[str, Any] | None = None,
    retrieval_results: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Evaluate every applicable contract; returns one entry per contract."""
    entries: list[dict[str, Any]] = []

    if update_results is not None:
        cfg = baseline["update"]
        metrics = derive_update_metrics(update_results)
        mc = metrics["arms"].get("mc_update")
        vr = metrics["arms"].get("vanilla_rag")

        # Sum over every MC-machinery arm (mc_update AND mc_prod) — VR has no
        # update machinery to blame. mc_update and mc_prod score the same MC
        # context, so one real deletion may appear in both arms; the double
        # count is harmless while stale_only_max is pinned to exactly 0 (any
        # nonzero total breaches regardless of multiplicity).
        stale_only = sum(
            arm["outcomes"]["stale_only"]
            for name, arm in metrics["arms"].items()
            if name != "vanilla_rag"
        )
        entries.append(
            _entry(
                "update.stale_only_zero",
                "breach" if stale_only > cfg["stale_only_max"] else "pass",
                stale_only,
                f"<= {cfg['stale_only_max']}",
                "the #1195 failure mode must stay extinct (#1198 veto is deterministic)",
            )
        )

        if mc is None or mc["update_success_at_k"] is None:
            entries.append(
                _entry(
                    "update.mc_update_success_floor",
                    "breach",
                    None,
                    f">= {cfg['mc_update_success_min']}",
                    "mc_update arm missing from results — harness regression",
                )
            )
        else:
            entries.append(
                _entry(
                    "update.mc_update_success_floor",
                    (
                        "pass"
                        if mc["update_success_at_k"] >= cfg["mc_update_success_min"]
                        else "breach"
                    ),
                    round(mc["update_success_at_k"], 4),
                    f">= {cfg['mc_update_success_min']}",
                )
            )

        lo, hi = cfg["vr_update_success_band"]
        if vr is None or vr["update_success_at_k"] is None:
            entries.append(
                _entry(
                    "update.vr_sanity_band",
                    "breach",
                    None,
                    f"[{lo}, {hi}]",
                    "vanilla arm missing from results — harness regression",
                )
            )
        else:
            entries.append(
                _entry(
                    "update.vr_sanity_band",
                    ("pass" if lo <= vr["update_success_at_k"] <= hi else "breach"),
                    round(vr["update_success_at_k"], 4),
                    f"[{lo}, {hi}]",
                    "outside the band = harness/corpus broke, not the product",
                )
            )

        failures = metrics["llm_call_failures_total"]
        if failures is None:
            entries.append(
                _entry(
                    "update.llm_call_failures_zero",
                    "skip",
                    None,
                    f"<= {cfg['llm_call_failures_max']}",
                    "field absent (pre-#1183 results file) — skipped, not passed",
                )
            )
        else:
            entries.append(
                _entry(
                    "update.llm_call_failures_zero",
                    "breach" if failures > cfg["llm_call_failures_max"] else "pass",
                    failures,
                    f"<= {cfg['llm_call_failures_max']}",
                    "silent judge death (#1177) cannot hide behind a green run",
                )
            )

    if retrieval_results is not None:
        cfg = baseline["retrieval"]
        overall_p5 = (retrieval_results.get("overall") or {}).get("p@5")
        if overall_p5 is None:
            entries.append(
                _entry(
                    "retrieval.overall_p5_floor",
                    "skip",
                    None,
                    f">= {cfg['overall_p5_min']}",
                    "overall p@5 absent from retrieval results",
                )
            )
        else:
            entries.append(
                _entry(
                    "retrieval.overall_p5_floor",
                    "pass" if overall_p5 >= cfg["overall_p5_min"] else "breach",
                    overall_p5,
                    f">= {cfg['overall_p5_min']}",
                    "drift signal vs frozen golden corpus (#344), jitter margin included",
                )
            )

    return entries


def exit_code_for(entries: list[dict[str, Any]], mode: str) -> int:
    """Map contract entries to the process exit code for the given mode."""
    breached = any(e["status"] == "breach" for e in entries)
    if breached and mode == "blocking":
        return EXIT_BREACH
    return EXIT_PASS


def render_summary(entries: list[dict[str, Any]]) -> str:
    """Markdown table for stdout + the GitHub Actions step summary."""
    lines = [
        "| contract | status | observed | bound | note |",
        "|---|---|---|---|---|",
    ]
    for e in entries:
        lines.append(
            f"| {e['name']} | {e['status'].upper()} | {e['observed']} | {e['bound']} | {e['note']} |"
        )
    return "\n".join(lines)


def _load_json(path: str, what: str) -> dict[str, Any] | None:
    """Load a JSON input; None signals INFRA (missing/undecodable)."""
    p = Path(path)
    if not p.is_file():
        print(f"ci-gates: INFRA — {what} file not found: {p}", file=sys.stderr)
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ci-gates: INFRA — {what} file unreadable: {p} ({exc})", file=sys.stderr)
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Evaluate #1210 eval contracts (claims -> contracts)")
    ap.add_argument(
        "--baseline", required=True, help="pinned baseline JSON (fixtures/ci_baseline.json)"
    )
    ap.add_argument("--update", help="update-runner results JSON")
    ap.add_argument("--retrieval", help="retrieval-runner results JSON")
    ap.add_argument(
        "--require",
        default="",
        help=(
            "comma-separated slice names (update,retrieval) that MUST have a "
            "results input — a required slice with no input is INFRA, so a "
            "runner that crashed and produced nothing can never yield a "
            "shortened-but-green contract table"
        ),
    )
    ap.add_argument(
        "--mode",
        choices=("blocking", "advisory"),
        default="blocking",
        help="advisory reports breaches but always exits 0 (week-1 soak, #336-style staged promotion)",
    )
    args = ap.parse_args(argv)

    baseline = _load_json(args.baseline, "baseline")
    if baseline is None:
        return EXIT_INFRA

    if not args.update and not args.retrieval:
        print(
            "ci-gates: INFRA — no results input given (need --update and/or --retrieval)",
            file=sys.stderr,
        )
        return EXIT_INFRA

    required = {name.strip() for name in args.require.split(",") if name.strip()}
    unknown = required - {"update", "retrieval"}
    if unknown:
        print(f"ci-gates: INFRA — unknown --require slice(s): {sorted(unknown)}", file=sys.stderr)
        return EXIT_INFRA
    missing = [
        name
        for name in sorted(required)
        if not {"update": args.update, "retrieval": args.retrieval}[name]
    ]
    if missing:
        print(
            f"ci-gates: INFRA — required slice(s) have no results input: {', '.join(missing)} "
            "(the runner likely crashed before writing results)",
            file=sys.stderr,
        )
        return EXIT_INFRA

    update_results = None
    retrieval_results = None
    if args.update:
        update_results = _load_json(args.update, "update results")
        if update_results is None:
            return EXIT_INFRA
    if args.retrieval:
        retrieval_results = _load_json(args.retrieval, "retrieval results")
        if retrieval_results is None:
            return EXIT_INFRA

    entries = evaluate_contracts(
        baseline, update_results=update_results, retrieval_results=retrieval_results
    )
    summary = render_summary(entries)
    print(summary)

    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as fh:
            fh.write("## Eval contract gates (#1210)\n\n" + summary + "\n")

    code = exit_code_for(entries, args.mode)
    breaches = [e["name"] for e in entries if e["status"] == "breach"]
    if breaches:
        print(
            f"ci-gates: {len(breaches)} contract breach(es): {', '.join(breaches)}"
            + (" [advisory mode — not failing the run]" if args.mode == "advisory" else ""),
            file=sys.stderr,
        )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
