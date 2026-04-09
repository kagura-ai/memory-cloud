"""Run LLM annotation on the pilot #249 sampled pairs.

Reads the full (un-redacted) ``_local/pairs.jsonl``, asks each configured
annotator (default ``gpt-5.4 + gemini-2.5-pro``; ``claude-opus-4-6``
optional) to label each pair using the committed ``labeling_prompt.md``,
and writes the annotations back to ``_local/pairs.jsonl`` atomically.
The committed ``pairs.jsonl`` in this directory stays redacted — run
``redact_pairs.py`` afterwards to refresh it with the new labels while
keeping summaries hidden.

Annotator notes:
- Default = ``openai gemini`` (changed during Phase B.2 — see
  ``pilot_llm.py`` docstring for the rationale: ``gpt-4o`` was deprecated,
  the local ``anthropic`` SDK is incompatible with current ``httpx``,
  and ``gemini-2.5-pro`` actually gives better cross-org annotator
  diversity than ``claude+gpt`` for the DS-PhD-flagged correlation
  concern).
- ``claude`` is still supported. Pass it explicitly if you have a
  working anthropic SDK install.

Runtime guards:

- **MAX_CALLS_DEFAULT = 150** (hard cap overridable via ``--max-calls``).
  50 pairs × 3 annotators = 150 calls if all three are enabled; default
  ``openai gemini`` uses 100 calls nominal.
- **TOKEN_CEILING = 250_000**. Checked before each call. Runaway prompts
  or an infinite loop fails closed at ~$5 of waste, not $50. Bumped from
  200k after Gemini's "thinking" tokens raised the per-call estimate.
- **labeling_prompt.md must be committed** before the script will run.
  Enforces the gate1 commitment mechanically, not by convention.
- **Resume**: ``--resume`` skips pairs where the target annotator already
  has a label stored in ``annotations``. Partial progress survives
  interruption.
- **Atomic writes**: results are flushed every ``CHECKPOINT_EVERY`` pairs
  via temp file + ``os.replace``, so a crash never corrupts the file.

CLI::

    python run_annotation.py --help
    python run_annotation.py --annotator openai gemini --max-calls 4 --dry-run
    python run_annotation.py                                  # default = openai gemini
    python run_annotation.py --annotator gemini --resume
    python run_annotation.py --annotator openai gemini claude  # 3-way ensemble

Env vars required (only checked for the annotators you actually pick):

- ``claude``: ``ANTHROPIC_API_KEY``
- ``openai``: ``OPENAI_API_KEY``
- ``gemini``: ``GEMINI_API_KEY`` (or ``GOOGLE_API_KEY``)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent

# Local imports — pilot_llm.py is next to this file.
sys.path.insert(0, str(HERE))
from pilot_llm import (  # type: ignore[import-not-found]  # noqa: E402
    CLAUDE_MODEL,
    DEFAULT_ANNOTATORS,
    GEMINI_MODEL,
    OPENAI_MODEL,
    format_user_msg,
    judge_pair,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------------
# Constants — refinement #3: token budget ceiling
# ----------------------------------------------------------------------------

MAX_CALLS_DEFAULT = 150  # 50 pairs × up to 3 annotators
# Bumped for Gemini 2.5: ~2700 tokens/call (2200 prompt + 512 thinking
# + ~80 output) × 50 pairs = ~135k for Gemini alone, plus ~105k for
# OpenAI. 300k gives ~25% margin for retries / variance.
TOKEN_CEILING = 300_000
CHECKPOINT_EVERY = 5  # flush results every N pairs

DEFAULT_PAIRS_PATH = HERE / "_local" / "pairs.jsonl"
DEFAULT_PROMPT_PATH = HERE / "labeling_prompt.md"

ANNOTATOR_KEYS = {
    "claude": CLAUDE_MODEL,
    "openai": OPENAI_MODEL,
    "gemini": GEMINI_MODEL,
}

VALID_ANNOTATORS = ("claude", "openai", "gemini")


# ----------------------------------------------------------------------------
# Pre-flight gates
# ----------------------------------------------------------------------------


def check_labeling_prompt_committed(prompt_path: Path) -> bool:
    """Return True if labeling_prompt.md is tracked in git.

    The gate1 design review commits to "prompt committed before annotation
    runs". This enforces it mechanically — if someone edits the prompt
    without committing, the script refuses to annotate.
    """
    try:
        subprocess.run(
            ["git", "ls-files", "--error-unmatch", str(prompt_path)],
            cwd=str(prompt_path.parent),
            check=True,
            capture_output=True,
            text=True,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def check_env_keys(annotators: list[str]) -> list[str]:
    """Return a list of missing env-var names (empty if all present)."""
    missing = []
    if "claude" in annotators and not os.environ.get("ANTHROPIC_API_KEY"):
        missing.append("ANTHROPIC_API_KEY")
    if "openai" in annotators and not os.environ.get("OPENAI_API_KEY"):
        missing.append("OPENAI_API_KEY")
    if "gemini" in annotators and not (
        os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    ):
        missing.append("GEMINI_API_KEY (or GOOGLE_API_KEY)")
    return missing


# ----------------------------------------------------------------------------
# Pairs I/O
# ----------------------------------------------------------------------------


def load_pairs(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"pairs file not found: {path}")
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def write_pairs_atomic(path: Path, rows: list[dict[str, Any]]) -> None:
    """Write rows to ``path`` via temp file + ``os.replace`` so partial
    writes never corrupt the file mid-run."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


# ----------------------------------------------------------------------------
# Annotation loop
# ----------------------------------------------------------------------------


async def annotate_one(
    row: dict[str, Any],
    annotator: str,
    system_prompt: str,
) -> dict[str, Any]:
    """Call the annotator for a single pair and return a normalized record."""
    user_msg = format_user_msg(row)
    result = await judge_pair(annotator, system_prompt, user_msg)  # type: ignore[arg-type]
    if "error" in result:
        return {
            "model": result["model"],
            "label": None,
            "rationale": None,
            "confidence": None,
            "tokens": 0,
            "error": result["error"],
            "raw": "",
            "annotated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    parsed = result.get("parsed", {})
    return {
        "model": result["model"],
        "label": parsed.get("label"),
        "rationale": parsed.get("rationale"),
        "confidence": parsed.get("confidence"),
        "tokens": result["tokens"],
        "error": None,
        "raw": result.get("raw", ""),
        "annotated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


async def run_annotation(
    *,
    pairs_path: Path,
    prompt_path: Path,
    annotators: list[str],
    max_calls: int,
    resume: bool,
    dry_run: bool,
) -> int:
    """Main loop. Returns process exit code (0 ok, non-zero on failure)."""
    # Gate: labeling_prompt.md must be tracked by git.
    if not check_labeling_prompt_committed(prompt_path):
        logger.error(
            "Pre-flight failed: %s is not tracked by git. Commit it BEFORE running annotation "
            "(gate1 commitment from the design review).",
            prompt_path,
        )
        return 3

    missing = check_env_keys(annotators)
    if missing:
        logger.error("Pre-flight failed: missing env vars: %s", ", ".join(missing))
        return 4

    system_prompt = prompt_path.read_text(encoding="utf-8")
    rows = load_pairs(pairs_path)
    logger.info("loaded %d pairs from %s", len(rows), pairs_path)

    calls_made = 0
    tokens_used = 0
    failures = 0
    per_annotator_stats: dict[str, dict[str, int]] = {
        a: {"calls": 0, "tokens": 0, "failures": 0} for a in annotators
    }

    # Estimate: 2 × per row × ~1.3k tokens = ~2.6k per pair × 50 = 130k.
    logger.info(
        "budget: max_calls=%d, token_ceiling=%d, checkpoint_every=%d",
        max_calls,
        TOKEN_CEILING,
        CHECKPOINT_EVERY,
    )
    if dry_run:
        logger.info("[DRY-RUN] will stop after %d annotation calls", min(max_calls, 4))

    limit = 4 if dry_run else max_calls

    try:
        for idx, row in enumerate(rows):
            row_annotations = row.setdefault("annotations", {})
            for annotator in annotators:
                if calls_made >= limit:
                    logger.warning(
                        "Hit max_calls ceiling (%d) — stopping early. Re-run with --resume.",
                        limit,
                    )
                    raise StopIteration
                if tokens_used >= TOKEN_CEILING:
                    logger.warning(
                        "Hit TOKEN_CEILING (%d) — stopping early. Re-run with --resume.",
                        TOKEN_CEILING,
                    )
                    raise StopIteration

                if resume and annotator in row_annotations:
                    existing = row_annotations[annotator]
                    if existing and existing.get("label") is not None:
                        continue  # already labeled, skip

                logger.info(
                    "pair %s (%d/%d) stratum=%s → %s",
                    row["pair_id"],
                    idx + 1,
                    len(rows),
                    row["stratum"],
                    annotator,
                )
                record = await annotate_one(row, annotator, system_prompt)
                row_annotations[annotator] = record
                calls_made += 1
                tokens_used += record["tokens"]
                per_annotator_stats[annotator]["calls"] += 1
                per_annotator_stats[annotator]["tokens"] += record["tokens"]
                if record.get("error"):
                    failures += 1
                    per_annotator_stats[annotator]["failures"] += 1
                    logger.warning(
                        "  ↳ error: %s",
                        record["error"][:120],
                    )
                else:
                    logger.info(
                        "  ↳ label=%s confidence=%s tokens=%d",
                        record["label"],
                        record["confidence"],
                        record["tokens"],
                    )

            # Atomic checkpoint
            if not dry_run and (idx + 1) % CHECKPOINT_EVERY == 0:
                write_pairs_atomic(pairs_path, rows)
                logger.info("  ↳ checkpoint: flushed %d rows", idx + 1)

    except StopIteration:
        pass

    # Final flush
    if not dry_run:
        write_pairs_atomic(pairs_path, rows)

    # ---- summary ----
    print()
    print("=" * 60)
    print("Annotation run summary")
    print("=" * 60)
    print(f"Total calls:   {calls_made}")
    print(f"Total tokens:  {tokens_used}")
    print(f"Failures:      {failures}")
    print(f"Dry run:       {dry_run}")
    print()
    print("Per annotator:")
    for annotator, stats in per_annotator_stats.items():
        print(
            f"  {annotator:10s} calls={stats['calls']:4d}  "
            f"tokens={stats['tokens']:7d}  failures={stats['failures']}"
        )
    print()
    if dry_run:
        print("[DRY-RUN] pairs file NOT modified.")
    else:
        print(f"Wrote annotations back to {pairs_path}")
        print("Next: python redact_pairs.py   (refreshes committed pairs.jsonl)")
    print("=" * 60)

    return 0 if failures == 0 else 1


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="LLM annotate pilot #249 sampled pairs.")
    p.add_argument(
        "--pairs",
        type=Path,
        default=DEFAULT_PAIRS_PATH,
        help=f"Path to pairs.jsonl (default: {DEFAULT_PAIRS_PATH.relative_to(HERE.parent)})",
    )
    p.add_argument(
        "--prompt",
        type=Path,
        default=DEFAULT_PROMPT_PATH,
        help="Path to labeling_prompt.md (default: next to this script)",
    )
    p.add_argument(
        "--annotator",
        nargs="+",
        choices=list(VALID_ANNOTATORS),
        default=list(DEFAULT_ANNOTATORS),
        metavar="ANNOTATOR",
        help=(
            "One or more annotators to run "
            f"(choices: {', '.join(VALID_ANNOTATORS)}; "
            f"default: {' '.join(DEFAULT_ANNOTATORS)})"
        ),
    )
    p.add_argument(
        "--max-calls",
        type=int,
        default=MAX_CALLS_DEFAULT,
        help=f"Hard cap on LLM calls (default: {MAX_CALLS_DEFAULT})",
    )
    p.add_argument(
        "--resume",
        action="store_true",
        help="Skip pairs that already have a non-null label for the target annotator",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Run at most 4 calls, do NOT write back to pairs.jsonl",
    )
    return p


def main() -> int:
    args = _build_arg_parser().parse_args()
    # argparse with nargs='+' returns a list of strings already.
    annotators = list(dict.fromkeys(args.annotator))  # dedupe, preserve order

    return asyncio.run(
        run_annotation(
            pairs_path=args.pairs,
            prompt_path=args.prompt,
            annotators=annotators,
            max_calls=args.max_calls,
            resume=args.resume,
            dry_run=args.dry_run,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
