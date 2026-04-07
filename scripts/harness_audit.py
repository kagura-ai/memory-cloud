#!/usr/bin/env python3
"""Meta-consistency audit for the memory-cloud harness.

Phase 7 of the harness loop. Runs cross-file consistency checks that
Phase 6 (scripts/harness_contract_lint.py) does not cover:

  1. Path drift — every repo path referenced in a harness file must
     exist. Forward references explicitly marked with "lands via PR #N"
     or "Phase N" are allowlisted.
  2. Channel enum vs Makefile — every `make test-*` value in the
     contract's Channel enum must be a real Makefile target.
  3. Field-name drift — identifiers matching `[a-z][a-z_]*_pct` in any
     harness file must appear in the canonical contract doc. This
     catches the five_hour_pct bug class.
  4. recall() API shape — every recall( call in a harness file must
     include context_id= and filters= (or be explicitly commented
     otherwise).

Output format: [C] critical / [W] warning / [I] info, one finding per
line. Exit 0 if zero [C] findings, 1 otherwise.

Pure stdlib. No third-party dependencies.

Usage:
    python3 scripts/harness_audit.py
    python3 scripts/harness_audit.py --verbose
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

CONTRACT_DOC = Path(".claude/rules/harness-contract.md")
HARNESS_AGENTS_GLOB = ".claude/agents/harness-*.md"
HARNESS_COMMAND = Path(".claude/commands/harness-loop.md")
HARNESS_AUDIT_COMMAND = Path(".claude/commands/harness-audit.md")
MAKEFILE = Path("Makefile")

# Repo path pattern: directories at repo root that hold real code.
# The full path may include slashes, alphanumerics, underscores, dots,
# hyphens, and glob stars. We anchor on the root directory name to
# avoid matching random strings like `tests` inside prose.
ROOT_DIRS = r"(?:backend|frontend|\.claude|\.github|scripts|docs|tests|claude-skills)"
PATH_RE = re.compile(rf"`({ROOT_DIRS}/[a-zA-Z0-9_./*\-]+)`")

# A field-name-style token ending in _pct — catches the five_hour_pct
# bug class. This is narrow on purpose; other drift classes can be
# added as separate checks once this one proves useful.
PCT_FIELD_RE = re.compile(r"\b([a-z][a-z_]*_pct)\b")

# make test-* enum values in the Channel enum table rows
MAKE_CHANNEL_RE = re.compile(r"`make (test-[a-z-]+)`")

# recall( calls — capture the immediate argument region
RECALL_RE = re.compile(r"recall\s*\(([^)]{0,500})", re.DOTALL)

# Forward-reference allowlist markers on the same line or nearby
FORWARD_REF_MARKERS = (
    "lands via",
    "landing via",
    "Phase 4",
    "Phase 7",
    "separate PR",
    "not yet authored",
    "TBD",
    "forward reference",
    "forward ref",
)


@dataclass
class Finding:
    severity: str  # "C", "W", or "I"
    file: str
    line: int | None
    message: str

    def format(self) -> str:
        loc = f"{self.file}:{self.line}" if self.line else self.file
        return f"[{self.severity}] {loc} — {self.message}"


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)

    def add(self, severity: str, file: str, line: int | None, message: str) -> None:
        self.findings.append(Finding(severity, file, line, message))

    def by_severity(self, sev: str) -> list[Finding]:
        return [f for f in self.findings if f.severity == sev]

    def has_critical(self) -> bool:
        return any(f.severity == "C" for f in self.findings)


def collect_harness_files() -> list[Path]:
    """Return the set of existing harness files on this branch."""
    files: list[Path] = []
    if CONTRACT_DOC.is_file():
        files.append(CONTRACT_DOC)
    files.extend(sorted(Path(".").glob(HARNESS_AGENTS_GLOB)))
    if HARNESS_COMMAND.is_file():
        files.append(HARNESS_COMMAND)
    if HARNESS_AUDIT_COMMAND.is_file():
        files.append(HARNESS_AUDIT_COMMAND)
    return files


def is_forward_ref_line(line: str) -> bool:
    return any(marker in line for marker in FORWARD_REF_MARKERS)


def has_forward_ref_nearby(lines: list[str], idx: int, window: int = 3) -> bool:
    """Check the current line plus `window` lines on either side for a
    forward-reference marker. The Evaluator's References section marks
    forward refs on the line *after* the path reference, so we look
    both ways."""
    start = max(0, idx - window)
    end = min(len(lines), idx + window + 1)
    return any(is_forward_ref_line(lines[i]) for i in range(start, end))


PLACEHOLDER_SEGMENTS = {"foo", "bar", "baz", "foo.py", "bar.py", "example"}


def is_placeholder_path(path: str) -> bool:
    """Heuristic: skip obviously-illustrative paths in prose.

    Real path references in harness files always name concrete repo
    locations (like `backend/src/mcp_server/`). Example paths used to
    explain concepts use ellipses, angle-bracket placeholders, or
    conventional placeholder names like `foo` / `bar`.
    """
    if "..." in path or "<" in path or ">" in path:
        return True
    segments = set(path.split("/"))
    if segments & PLACEHOLDER_SEGMENTS:
        return True
    return False


def check_path_drift(files: list[Path], report: Report) -> None:
    """Every `backend/...` (etc.) path in backticks must resolve, unless
    the surrounding context marks it as a forward reference or the path
    is an obvious prose placeholder."""
    for f in files:
        lines = f.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            for match in PATH_RE.finditer(line):
                path = match.group(1)
                # Glob wildcards in the match can't be ls-verified; skip.
                if "*" in path:
                    continue
                if is_placeholder_path(path):
                    continue
                if os.path.exists(path):
                    continue
                if has_forward_ref_nearby(lines, i):
                    continue
                report.add(
                    "C",
                    str(f),
                    i + 1,
                    f"path `{path}` does not exist and is not marked as a forward reference",
                )


def check_channel_makefile(report: Report) -> None:
    """Every `make test-*` channel in the contract's Channel enum must
    be a real Makefile target."""
    if not CONTRACT_DOC.is_file():
        return
    if not MAKEFILE.is_file():
        report.add(
            "W", str(MAKEFILE), None, "Makefile not found — skipping channel check"
        )
        return

    contract_text = CONTRACT_DOC.read_text(encoding="utf-8")
    makefile_text = MAKEFILE.read_text(encoding="utf-8")
    makefile_targets = set(
        re.findall(r"^([a-z][a-z0-9-]*):", makefile_text, re.MULTILINE)
    )

    channels = set(MAKE_CHANNEL_RE.findall(contract_text))
    for channel in sorted(channels):
        if channel not in makefile_targets:
            report.add(
                "C",
                str(CONTRACT_DOC),
                None,
                f"Channel enum references `make {channel}` but target `{channel}` is not in Makefile",
            )


def check_field_name_drift(files: list[Path], report: Report) -> None:
    """Identifiers matching [a-z][a-z_]*_pct must appear in the contract
    doc (the five_hour_pct bug class)."""
    if not CONTRACT_DOC.is_file():
        report.add(
            "W",
            str(CONTRACT_DOC),
            None,
            "contract doc not found — skipping field drift check",
        )
        return

    contract_text = CONTRACT_DOC.read_text(encoding="utf-8")
    # Build the set of *_pct tokens from the contract — these are allowlisted.
    known_pct_tokens = set(PCT_FIELD_RE.findall(contract_text))

    for f in files:
        if f == CONTRACT_DOC:
            continue
        lines = f.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            for match in PCT_FIELD_RE.finditer(line):
                token = match.group(1)
                if token not in known_pct_tokens:
                    report.add(
                        "C",
                        str(f),
                        i + 1,
                        f"field name `{token}` is not defined in {CONTRACT_DOC.name} — possible drift (see five_hour_pct class)",
                    )


def check_recall_api_shape(files: list[Path], report: Report) -> None:
    """Every recall( call in a harness file must include context_id=
    and either filters= or a nearby comment explaining why not."""
    for f in files:
        text = f.read_text(encoding="utf-8")
        for match in RECALL_RE.finditer(text):
            args = match.group(1)
            # Prose mention filter: a real function call has at least
            # one `=` (keyword argument marker). Without one, the
            # parenthetical is natural language like "every recall( call
            # must include ..." and should be skipped.
            if "=" not in args:
                continue
            # Count the line number of the match start.
            line_no = text.count("\n", 0, match.start()) + 1
            if "context_id" not in args:
                report.add(
                    "W",
                    str(f),
                    line_no,
                    "recall() call missing `context_id=` argument",
                )
            if "filters" not in args and "type=" in args:
                report.add(
                    "W",
                    str(f),
                    line_no,
                    'recall() call uses `type=` directly — canonical form is `filters={"type": ...}`',
                )


def check_forbidden_patterns_section(files: list[Path], report: Report) -> None:
    """Informational: each harness agent/command should have a
    Forbidden patterns section mirroring the contract's rules."""
    for f in files:
        if f == CONTRACT_DOC:
            continue
        if "harness-audit" in f.name:
            continue  # audit itself doesn't need one
        text = f.read_text(encoding="utf-8")
        if "## Forbidden patterns" not in text:
            report.add(
                "I",
                str(f),
                None,
                "no `## Forbidden patterns` section — consider adding one to mirror the contract's anti-softening rules",
            )


def check_reads_schema_first(files: list[Path], report: Report) -> None:
    """Informational: each agent/command should explicitly read the
    contract doc first."""
    for f in files:
        if f == CONTRACT_DOC:
            continue
        text = f.read_text(encoding="utf-8")
        if "harness-contract.md" not in text:
            report.add(
                "I",
                str(f),
                None,
                "does not reference `.claude/rules/harness-contract.md` — all harness files should treat it as authoritative",
            )


def print_report(report: Report, verbose: bool = False) -> None:
    crit = report.by_severity("C")
    warn = report.by_severity("W")
    info = report.by_severity("I")

    print("## Harness audit\n")

    print(f"### [C] Critical ({len(crit)})")
    if crit:
        for f in crit:
            print(f"- {f.format()}")
    else:
        print("- none")
    print()

    print(f"### [W] Warnings ({len(warn)})")
    if warn:
        for f in warn:
            print(f"- {f.format()}")
    else:
        print("- none")
    print()

    if verbose or info:
        print(f"### [I] Info ({len(info)})")
        if info:
            for f in info:
                print(f"- {f.format()}")
        else:
            print("- none")
        print()

    print(f"{len(crit)} critical, {len(warn)} warnings, {len(info)} info")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Meta-consistency audit for the memory-cloud harness."
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Always show [I] info section"
    )
    args = parser.parse_args(argv)

    report = Report()
    files = collect_harness_files()

    if not files:
        print(
            "## Harness audit\n\nNo harness files found on this branch. Nothing to audit."
        )
        return 0

    check_path_drift(files, report)
    check_channel_makefile(report)
    check_field_name_drift(files, report)
    check_recall_api_shape(files, report)
    check_forbidden_patterns_section(files, report)
    check_reads_schema_first(files, report)

    print_report(report, verbose=args.verbose)

    return 1 if report.has_critical() else 0


if __name__ == "__main__":
    sys.exit(main())
