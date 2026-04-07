#!/usr/bin/env python3
"""Static lint for the memory-cloud harness contract.

Two modes:

  --schema       (default)
      Read .claude/rules/harness-contract.md, extract every fenced
      ```json block, validate each parses as JSON. Catches the bug
      class from PR #251 round 1 where a block comment was placed
      inside a fenced JSON block.

  --contract <path>
      Read a per-run contract.json from a harness run cache directory
      and validate it against the top-level schema rules: required
      fields, channel enum, gate object shape, run_id format, and
      per-contract field rules.

Pure stdlib. No third-party dependencies. Exit 0 on success, 1 on any
failure, with one line per failure printed to stderr.

Usage:
    python3 scripts/harness_contract_lint.py
    python3 scripts/harness_contract_lint.py --schema
    python3 scripts/harness_contract_lint.py --contract path/to/contract.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

CONTRACT_DOC = Path(".claude/rules/harness-contract.md")

CHANNEL_ENUM = {
    "make test-local",
    "make test-integration",
    "make test-neural",
    "make test-smoke",
    "make test-e2e",
    "make test-frontend",
    "playwright-mcp",
    "mcp-live",
    "self-review",
}

OUTCOME_ENUM = {"merged", "failed", "aborted", "escalated"}

GATE_KEYS = {"gate1_planner_review", "gate2_pre_pr_review", "gate3_release_audit"}

BUDGET_KEYS = {
    "max_iterations",
    "max_input_tokens_per_run",
    "abort_if_five_hour_pct_above",
    "clear_if_context_pct_above",
}

TOP_LEVEL_REQUIRED = {
    "schema_version",
    "run_id",
    "issue",
    "branch",
    "created_at",
    "areas",
    "contracts",
    "gates",
    "budget",
}

CONTRACT_ENTRY_REQUIRED = {
    "id",
    "statement",
    "channel",
    "evidence_target",
    "promotion_candidate",
    "reason",
}

RUN_ID_RE = re.compile(r"^hr-\d{8}-\d{3}$")
CONTRACT_ID_RE = re.compile(r"^C-\d+-\d{2}$")
JSON_FENCE_RE = re.compile(r"```json\n(.*?)\n```", re.DOTALL)


def lint_schema_doc(doc_path: Path) -> list[str]:
    """Extract every ```json fenced block from the contract doc and
    validate each parses as JSON. Returns a list of error messages."""
    errors: list[str] = []
    if not doc_path.is_file():
        return [f"contract doc not found at {doc_path}"]

    text = doc_path.read_text(encoding="utf-8")
    blocks = JSON_FENCE_RE.findall(text)
    if not blocks:
        errors.append(f"no fenced ```json blocks found in {doc_path}")
        return errors

    for i, block in enumerate(blocks, start=1):
        try:
            json.loads(block)
        except json.JSONDecodeError as exc:
            errors.append(
                f"{doc_path}: ```json block #{i} does not parse — "
                f"{exc.msg} at line {exc.lineno} col {exc.colno}"
            )
    return errors


def lint_contract_file(contract_path: Path) -> list[str]:
    """Validate a per-run contract.json against the top-level schema."""
    errors: list[str] = []
    if not contract_path.is_file():
        return [f"contract file not found at {contract_path}"]

    try:
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f"{contract_path}: invalid JSON — {exc.msg} at line {exc.lineno}"]

    if not isinstance(contract, dict):
        return [f"{contract_path}: top-level value must be an object"]

    # Required top-level fields
    for key in TOP_LEVEL_REQUIRED:
        if key not in contract:
            errors.append(f"{contract_path}: missing required field '{key}'")

    # run_id format
    run_id = contract.get("run_id")
    if isinstance(run_id, str) and not RUN_ID_RE.match(run_id):
        errors.append(
            f"{contract_path}: run_id '{run_id}' does not match hr-YYYYMMDD-NNN format"
        )

    # areas non-empty
    areas = contract.get("areas")
    if isinstance(areas, list) and len(areas) == 0:
        errors.append(f"{contract_path}: areas[] is empty (default to ['lib'] instead)")

    # contracts[] entries
    contracts = contract.get("contracts")
    if isinstance(contracts, list):
        if len(contracts) == 0:
            errors.append(f"{contract_path}: contracts[] is empty")
        for j, entry in enumerate(contracts):
            if not isinstance(entry, dict):
                errors.append(f"{contract_path}: contracts[{j}] is not an object")
                continue
            for key in CONTRACT_ENTRY_REQUIRED:
                if key not in entry:
                    errors.append(
                        f"{contract_path}: contracts[{j}] missing field '{key}'"
                    )
            # id format
            cid = entry.get("id")
            if isinstance(cid, str) and not CONTRACT_ID_RE.match(cid):
                errors.append(
                    f"{contract_path}: contracts[{j}].id '{cid}' does not match C-<issue>-<NN>"
                )
            # channel enum
            channel = entry.get("channel")
            if isinstance(channel, str) and channel not in CHANNEL_ENUM:
                errors.append(
                    f"{contract_path}: contracts[{j}].channel '{channel}' is not in the Channel enum"
                )
            # evidence_target null only allowed for self-review
            ev = entry.get("evidence_target")
            if ev is None and channel != "self-review":
                errors.append(
                    f"{contract_path}: contracts[{j}].evidence_target is null but channel is '{channel}' (only self-review allows null)"
                )

    # gates shape
    gates = contract.get("gates")
    if isinstance(gates, dict):
        for gk in GATE_KEYS:
            if gk not in gates:
                errors.append(f"{contract_path}: gates.{gk} is missing")
                continue
            gate = gates[gk]
            if not isinstance(gate, dict):
                errors.append(f"{contract_path}: gates.{gk} is not an object")
                continue
            if "enabled" not in gate or not isinstance(gate["enabled"], bool):
                errors.append(f"{contract_path}: gates.{gk}.enabled must be a boolean")
            if "reviewers" not in gate or not isinstance(gate["reviewers"], list):
                errors.append(f"{contract_path}: gates.{gk}.reviewers must be an array")
            elif gate.get("enabled") is False and len(gate["reviewers"]) > 0:
                errors.append(
                    f"{contract_path}: gates.{gk}.enabled=false but reviewers is non-empty (must be [])"
                )

    # budget shape
    budget = contract.get("budget")
    if isinstance(budget, dict):
        for bk in BUDGET_KEYS:
            if bk not in budget:
                errors.append(f"{contract_path}: budget.{bk} is missing")
            elif not isinstance(budget[bk], (int, float)):
                errors.append(f"{contract_path}: budget.{bk} must be a number")

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Static lint for the memory-cloud harness contract."
    )
    parser.add_argument(
        "--schema",
        action="store_true",
        help="Validate fenced ```json blocks in the contract doc (default if no mode given)",
    )
    parser.add_argument(
        "--contract",
        type=Path,
        default=None,
        metavar="PATH",
        help="Validate a per-run contract.json file against the top-level schema",
    )
    parser.add_argument(
        "--doc",
        type=Path,
        default=CONTRACT_DOC,
        help=f"Path to the contract doc (default: {CONTRACT_DOC})",
    )
    args = parser.parse_args(argv)

    # Default to --schema when neither flag is given
    if not args.schema and args.contract is None:
        args.schema = True

    all_errors: list[str] = []

    if args.schema:
        all_errors.extend(lint_schema_doc(args.doc))

    if args.contract is not None:
        all_errors.extend(lint_contract_file(args.contract))

    if all_errors:
        for err in all_errors:
            print(f"FAIL: {err}", file=sys.stderr)
        print(f"\n{len(all_errors)} lint failure(s)", file=sys.stderr)
        return 1

    print("OK: harness contract lint passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
