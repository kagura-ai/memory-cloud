"""Redact summary text from pairs.jsonl before committing.

The full ``pairs.jsonl`` contains ``src_summary`` / ``dst_summary`` — real
memory content from the sampling context (kagura-dev). This repo is public,
so committing those summaries would publish the author's development log
forever. This script reads the full (gitignored) file from ``_local/`` and
writes a redacted sibling at ``pairs.jsonl`` in the pilot dir, replacing the
summary fields with ``"<redacted>"``.

What is kept in the committed file:
- ``pair_id``, ``stratum``, ``context_name``, ``context_id``
- ``src_id``, ``dst_id`` (reproducible from the DB)
- ``src_tags``, ``dst_tags`` (used by Stratum D ranking — needed for audit)
- ``cosine_similarity`` (sampling validation)
- ``src_created_at``, ``dst_created_at`` (metadata, not content)
- ``has_existing_edge``, ``existing_edge_type``, ``synthetic_seed_edge``
- ``d_shared_tag_count``, ``d_ranked_from_pair_id`` (refinement #5 audit)
- ``snapshot_t0``, ``filter_state``, ``sampling_seed``, ``labeling_prompt_sha256``
- ``annotations`` (LLM labels + rationale, but see note below)

What is REDACTED:
- ``src_summary`` → ``"<redacted>"``
- ``dst_summary`` → ``"<redacted>"``

**Note on annotations field**: when ``run_annotation.py`` populates the
``annotations`` field, the ``rationale`` sub-field may quote parts of the
original summaries. The redactor does NOT auto-redact annotation content
because labels are the pilot's main output — removing them would defeat
the audit trail. The author reviews annotations manually during findings
write-up and redacts any quoted content by hand if needed.

Usage::

    python redact_pairs.py                        # reads _local/pairs.jsonl → writes pairs.jsonl
    python redact_pairs.py --input other.jsonl    # explicit input path
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REDACTED = "<redacted>"

HERE = Path(__file__).resolve().parent
DEFAULT_INPUT = HERE / "_local" / "pairs.jsonl"
DEFAULT_OUTPUT = HERE / "pairs.jsonl"


def redact_row(row: dict) -> dict:
    """Return a new row dict with summary fields replaced by ``<redacted>``."""
    redacted = dict(row)
    for key in ("src_summary", "dst_summary"):
        if key in redacted:
            redacted[key] = REDACTED
    return redacted


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_INPUT,
        help=f"Input pairs.jsonl path (default: {DEFAULT_INPUT.relative_to(HERE.parent)})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Output redacted pairs.jsonl path (default: {DEFAULT_OUTPUT.name})",
    )
    args = parser.parse_args()

    if not args.input.exists():
        print(f"ERROR: input file not found: {args.input}", file=sys.stderr)
        print(
            "Hint: run sampling_script.py first, then copy the full pairs.jsonl "
            "into _local/ before running this redactor.",
            file=sys.stderr,
        )
        return 2

    n_in = 0
    n_out = 0
    with (
        args.input.open("r", encoding="utf-8") as src,
        args.output.open("w", encoding="utf-8") as dst,
    ):
        for line in src:
            line = line.strip()
            if not line:
                continue
            n_in += 1
            row = json.loads(line)
            redacted = redact_row(row)
            dst.write(json.dumps(redacted, ensure_ascii=False) + "\n")
            n_out += 1

    print(f"Redacted {n_out} rows: {args.input.name} → {args.output.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
