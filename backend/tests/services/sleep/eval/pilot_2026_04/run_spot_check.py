"""Author blind spot-check for pilot #249.

Reads ``_local/pairs.jsonl`` (the full, un-redacted file with annotations
already populated by ``run_annotation.py``), picks 10 pairs deterministically,
asks the author to label them blind, then reveals the comparison and writes
``spot_check.md`` next to this script.

Gate semantics (from gate1 design review):

- **Average human-vs-LLM agreement < 70%** → silver standard is broken.
  Script exits with code 2 and ``spot_check.md`` documents the off-ramp:
  PR-as-draft, follow-up issue, no full-eval recommendation.
- **Average ≥ 70%** → silver standard holds. Exit 0; proceed to ``findings.md``.

Determinism:

- Sample seed is 4242 (intentionally distinct from sampling_script's 42 so
  spot-check picks are decorrelated from sampling order).
- Picks are written to ``spot_check.md`` so a re-run of the spot-check (with
  the same seed) selects the same pairs.

This is a CLI tool that requires interactive input. Run it from a real
terminal (not Claude Code's non-interactive bash):

    cd backend/tests/services/sleep/eval/pilot_2026_04
    python run_spot_check.py

"""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
DEFAULT_PAIRS = HERE / "_local" / "pairs.jsonl"
DEFAULT_OUT = HERE / "spot_check.md"
DEFAULT_SEED = 4242
DEFAULT_N = 10
PASS_THRESHOLD = 0.70

LABELS = (
    "unrelated",
    "semantic_only",
    "inferential_causal",
    "inferential_procedural",
    "inferential_supersedes",
    "inferential_contradicts",
)


def pick_pairs(rows: list[dict], n: int, seed: int) -> list[dict]:
    """Deterministically pick n pairs from rows. random.Random(seed).sample
    is stable across Python versions for the same seed."""
    if len(rows) <= n:
        return list(rows)
    indices = sorted(random.Random(seed).sample(range(len(rows)), n))
    return [rows[i] for i in indices]


def prompt_label(prompt: str) -> str:
    """Prompt the user for a 1-6 label number, retry until valid.
    Returns the label name (e.g., 'unrelated')."""
    while True:
        raw = input(prompt).strip()
        if not raw:
            print("(empty input — please enter 1-6)")
            continue
        try:
            idx = int(raw)
        except ValueError:
            print(f"(not a number — please enter 1-{len(LABELS)})")
            continue
        if not 1 <= idx <= len(LABELS):
            print(f"(out of range — please enter 1-{len(LABELS)})")
            continue
        return LABELS[idx - 1]


def display_pair(idx: int, total: int, row: dict) -> None:
    """Print one pair for blind labeling. Annotations stripped from display."""
    print()
    print("=" * 70)
    print(f"PAIR {idx}/{total}    pair_id={row['pair_id']}    stratum={row['stratum']}")
    print(f"context: {row['context_name']}    cosine: {row['cosine_similarity']:.4f}")
    if row.get("has_existing_edge"):
        print(
            f"existing_edge_type: {row.get('existing_edge_type')} (note: pair has a real edge already)"
        )
    if row["stratum"] == "D" and row.get("d_shared_tag_count") is not None:
        print(
            f"(stratum D — hard negative, shared {row['d_shared_tag_count']} tags "
            f"with picked pair {row.get('d_ranked_from_pair_id')})"
        )
    print("-" * 70)
    print(f"src.summary  : {row['src_summary']}")
    print(f"src.tags     : {row.get('src_tags') or []}")
    print(f"src.created  : {row.get('src_created_at', '')}")
    print()
    print(f"dst.summary  : {row['dst_summary']}")
    print(f"dst.tags     : {row.get('dst_tags') or []}")
    print(f"dst.created  : {row.get('dst_created_at', '')}")
    print("-" * 70)
    print("Choose label:")
    for i, label in enumerate(LABELS, 1):
        print(f"  {i}) {label}")


def collect_author_labels(picks: list[dict]) -> list[dict]:
    """Walk the picked pairs, ask for label + rationale, return parallel
    list of {label, rationale} dicts. Annotations are NOT shown until all
    10 are collected (prevents anchoring on LLM output)."""
    print()
    print("=" * 70)
    print("Pilot #249 — author blind spot-check")
    print("=" * 70)
    print(f"You will label {len(picks)} pairs blind. LLM annotations are HIDDEN")
    print("until you have labeled all of them. Take your time — there is no")
    print("partial credit, only the final agreement number matters.")
    print()
    print("After labeling all pairs, you will see a comparison table and the")
    print("script will write spot_check.md with the verdict.")
    print()
    input("Press Enter to start, or Ctrl-C to abort...")

    labels: list[dict] = []
    for idx, row in enumerate(picks, 1):
        display_pair(idx, len(picks), row)
        label = prompt_label("Your label [1-6]: ")
        rationale = input("Your rationale (one line, optional): ").strip()
        labels.append({"label": label, "rationale": rationale})
    return labels


def reveal_comparison(picks: list[dict], author_labels: list[dict]) -> dict[str, Any]:
    """Print the comparison table and compute agreement metrics."""
    print()
    print("=" * 90)
    print("COMPARISON: author vs LLM annotators")
    print("=" * 90)
    print(f"{'pair_id':10s} {'author':24s} {'openai':24s} {'gemini':24s}  vs_o  vs_g")
    print("-" * 90)

    auth_vs_openai = 0
    auth_vs_gemini = 0
    rows_for_md: list[dict] = []
    for row, author in zip(picks, author_labels, strict=True):
        anns = row.get("annotations") or {}
        openai_label = (anns.get("openai") or {}).get("label") or "(no label)"
        gemini_label = (anns.get("gemini") or {}).get("label") or "(no label)"
        author_label = author["label"]
        match_o = author_label == openai_label
        match_g = author_label == gemini_label
        if match_o:
            auth_vs_openai += 1
        if match_g:
            auth_vs_gemini += 1
        marker_o = "✓" if match_o else "✗"
        marker_g = "✓" if match_g else "✗"
        print(
            f"{row['pair_id']:10s} {author_label:24s} {openai_label:24s} {gemini_label:24s}  {marker_o}     {marker_g}"
        )
        rows_for_md.append(
            {
                "pair_id": row["pair_id"],
                "stratum": row["stratum"],
                "cosine": row["cosine_similarity"],
                "author_label": author_label,
                "author_rationale": author["rationale"],
                "openai_label": openai_label,
                "openai_confidence": (anns.get("openai") or {}).get("confidence"),
                "openai_rationale": (anns.get("openai") or {}).get("rationale"),
                "gemini_label": gemini_label,
                "gemini_confidence": (anns.get("gemini") or {}).get("confidence"),
                "gemini_rationale": (anns.get("gemini") or {}).get("rationale"),
                "match_openai": match_o,
                "match_gemini": match_g,
            }
        )

    n = len(picks)
    rate_o = auth_vs_openai / n
    rate_g = auth_vs_gemini / n
    avg = (rate_o + rate_g) / 2
    passed = avg >= PASS_THRESHOLD

    print("-" * 90)
    print(f"author vs openai : {auth_vs_openai}/{n}  ({rate_o:.0%})")
    print(f"author vs gemini : {auth_vs_gemini}/{n}  ({rate_g:.0%})")
    print(f"average          : {avg:.0%}")
    print(f"threshold        : {PASS_THRESHOLD:.0%}")
    print(f"VERDICT          : {'PASS' if passed else 'ABORT'}")
    print("=" * 90)

    return {
        "n": n,
        "auth_vs_openai": auth_vs_openai,
        "auth_vs_gemini": auth_vs_gemini,
        "rate_openai": rate_o,
        "rate_gemini": rate_g,
        "average": avg,
        "passed": passed,
        "rows": rows_for_md,
    }


def write_spot_check_md(out_path: Path, seed: int, n: int, summary: dict) -> None:
    """Write the spot_check.md report. Includes verdict + per-pair table +
    off-ramp instructions if FAILED."""
    lines: list[str] = []
    now = datetime.now(timezone.utc).isoformat()
    lines.append("# Pilot #249 — author spot-check")
    lines.append("")
    lines.append(f"**Generated**: {now}")
    lines.append(f"**Seed**: {seed}")
    lines.append(f"**Pairs sampled**: {n}")
    lines.append("")
    lines.append("## Verdict")
    lines.append("")
    lines.append(
        f"- author vs openai : **{summary['auth_vs_openai']}/{n}** ({summary['rate_openai']:.0%})"
    )
    lines.append(
        f"- author vs gemini : **{summary['auth_vs_gemini']}/{n}** ({summary['rate_gemini']:.0%})"
    )
    lines.append(f"- average           : **{summary['average']:.0%}**")
    lines.append(f"- threshold         : **{PASS_THRESHOLD:.0%}**")
    lines.append("")
    lines.append(f"## VERDICT: {'PASS' if summary['passed'] else 'ABORT'}")
    lines.append("")

    if summary["passed"]:
        lines.append("Average LLM-human label agreement clears the 70% threshold from")
        lines.append("the gate1 design review. The silver consensus is treated as a")
        lines.append("usable signal for the qualitative findings write-up. Proceed to")
        lines.append("`findings.md` and `next_steps.md`.")
    else:
        lines.append("Average LLM-human label agreement is BELOW the 70% threshold from")
        lines.append("the gate1 design review. The silver consensus is **broken**.")
        lines.append("")
        lines.append("### Off-ramp actions (operator)")
        lines.append("")
        lines.append("1. Mark the pilot PR as draft with the comment:")
        lines.append("")
        lines.append(f"   > Spot-check failed at {summary['average']:.0%} average")
        lines.append("   > agreement (threshold 70%). The annotations are retained for")
        lines.append("   > post-mortem but should not be treated as silver consensus.")
        lines.append("   > Filing follow-up issue for prompt iteration.")
        lines.append("")
        lines.append("2. File follow-up GitHub issue:")
        lines.append("")
        lines.append("   ```")
        lines.append(
            "   gh issue create --title 'chore(sleep-eval): pilot #249 spot-check failed — prompt iteration needed' \\"
        )
        lines.append(
            "       --body '<link to this spot_check.md, labeling_prompt.md SHA, disagreement examples>'"
        )
        lines.append("   ```")
        lines.append("")
        lines.append("3. Do NOT delete `_local/pairs.jsonl` — the failure mode is itself")
        lines.append('   a finding. Document it in `findings.md` under "Attempt 1: prompt')
        lines.append('   did not transfer to author intuition" with specific disagreement')
        lines.append("   pair_ids from the table below.")
        lines.append("")
        lines.append("4. Do NOT open a follow-up full-eval issue. `next_steps.md` must")
        lines.append("   recommend AGAINST scaling up until prompt + taxonomy are revised.")
    lines.append("")
    lines.append("## Per-pair comparison")
    lines.append("")
    lines.append("| pair_id | stratum | cos | author | openai | gemini | a==o | a==g |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for r in summary["rows"]:
        lines.append(
            f"| {r['pair_id']} | {r['stratum']} | {r['cosine']:.3f} | "
            f"{r['author_label']} | {r['openai_label']} | {r['gemini_label']} | "
            f"{'✓' if r['match_openai'] else '✗'} | {'✓' if r['match_gemini'] else '✗'} |"
        )
    lines.append("")
    lines.append("## Per-pair rationales")
    lines.append("")
    for r in summary["rows"]:
        lines.append(f"### {r['pair_id']}  (stratum {r['stratum']})")
        lines.append("")
        lines.append(
            f"- **author** ({r['author_label']}): {r['author_rationale'] or '_(no rationale)_'}"
        )
        lines.append(
            f"- **openai** ({r['openai_label']}, conf {r['openai_confidence']}): {r['openai_rationale'] or '_(none)_'}"
        )
        lines.append(
            f"- **gemini** ({r['gemini_label']}, conf {r['gemini_confidence']}): {r['gemini_rationale'] or '_(none)_'}"
        )
        lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Author blind spot-check for pilot #249.")
    p.add_argument(
        "--pairs", type=Path, default=DEFAULT_PAIRS, help=f"default: {DEFAULT_PAIRS.name}"
    )
    p.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"default: {DEFAULT_OUT.name}")
    p.add_argument("--seed", type=int, default=DEFAULT_SEED)
    p.add_argument("--n", type=int, default=DEFAULT_N)
    return p


def main() -> int:
    args = _build_arg_parser().parse_args()

    if not args.pairs.exists():
        print(f"ERROR: pairs file not found: {args.pairs}", file=sys.stderr)
        print(
            "Hint: run sampling_script.py + run_annotation.py first.",
            file=sys.stderr,
        )
        return 3

    with args.pairs.open("r", encoding="utf-8") as f:
        rows = [json.loads(line) for line in f if line.strip()]

    # Refuse to spot-check if annotations aren't populated yet — that would
    # leave nothing to compare against.
    annotated_rows = [
        r
        for r in rows
        if (r.get("annotations") or {}).get("openai") or (r.get("annotations") or {}).get("gemini")
    ]
    if not annotated_rows:
        print(
            f"ERROR: no annotations found in {args.pairs}. Run run_annotation.py first.",
            file=sys.stderr,
        )
        return 4

    picks = pick_pairs(annotated_rows, args.n, args.seed)
    print(f"picked {len(picks)} pairs (seed={args.seed}) from {len(annotated_rows)} annotated rows")

    try:
        author_labels = collect_author_labels(picks)
    except (KeyboardInterrupt, EOFError):
        print("\nAborted by user. No spot_check.md written.")
        return 130

    summary = reveal_comparison(picks, author_labels)
    write_spot_check_md(args.out, seed=args.seed, n=args.n, summary=summary)
    print(f"\nWrote {args.out}")

    return 0 if summary["passed"] else 2


if __name__ == "__main__":
    sys.exit(main())
