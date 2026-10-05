"""The tool counts the docs state come from the registry (#1883).

The registry grew from 68 to 70 tools and the core profile from 16 to 18 in
v0.93.0; three pages kept the old numbers. Every count is derived here — the
registry for the total, ``CORE_TOOLS`` for the default profile, the
``readOnlyHint`` annotations for the read-only class — so a page that falls
behind the code fails by name and line.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from mcp_server.tools._definitions import get_tool_definitions
from mcp_server.tools._profiles import CORE_TOOLS

# backend/tests/mcp_server/<this file> -> mcp_server -> tests -> backend -> repo root
REPO_ROOT = Path(__file__).resolve().parents[3]
DOCS = REPO_ROOT / "docs"

# Dated records, not living reference: the pre-1.0 surface enumeration is
# frozen at the commit it names, and design notes / RFCs describe the surface
# of the day they were written.
DATED_DIRS = frozenset({"api-surface-1.0", "design", "rfc"})

# Pages that must keep stating a count: the patterns below rotting into
# "matches nothing" would otherwise pass silently.
PAGES_WITH_TOTAL = (
    "README.md",
    "docs/README.md",
    "docs/api-reference.md",
    "docs/architecture.md",
    "docs/concepts.md",
    "docs/mcp-clients.md",
    "docs/mcp-tools.md",
    "docs/remote-mcp-directory-scope.md",
)
PAGES_WITH_CORE = (
    "README.md",
    "docs/mcp-clients.md",
    "docs/mcp-tools.md",
    "docs/remote-mcp-directory-scope.md",
    "docs/troubleshooting.md",
)

# "the 18 core tools", "the 18 core memory and context tools", "the 18-tool core profile"
_CORE = re.compile(r"\b(\d{1,3})(?:-tool core\b| core (?:\w+ ){0,4}tools\b)")
# "The other 36 tools", "The 36 read-only tools"
_READ_ONLY = re.compile(r"\bother (\d{1,3}) tools\b|\b(\d{1,3}) read-only tools\b")
# "70 tools", "70 MCP tools", "70 MCP Tools" — not the "other N tools" of the class table
_TOTAL = re.compile(r"(?<!other )(?<![\w~-])(\d{2,3}) (?:MCP )?[Tt]ools\b")


def _registry() -> list[dict]:
    return get_tool_definitions()


def _expected() -> dict[str, int]:
    registry = _registry()
    return {
        "total": len(registry),
        "core": len(CORE_TOOLS),
        "read-only": sum(1 for tool in registry if tool["annotations"]["readOnlyHint"]),
    }


def counts_in(text: str) -> list[tuple[str, int, int]]:
    """Every tool count ``text`` states, as ``(kind, number, line_number)``."""
    found: list[tuple[str, int, int]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        for kind, pattern in (("core", _CORE), ("read-only", _READ_ONLY), ("total", _TOTAL)):
            for match in pattern.finditer(line):
                found.append((kind, int(next(g for g in match.groups() if g)), number))
    return found


def _pages() -> list[Path]:
    pages = [REPO_ROOT / "README.md"]
    for path in sorted(DOCS.rglob("*.md")):
        if not DATED_DIRS.intersection(path.relative_to(DOCS).parts):
            pages.append(path)
    return pages


def _read(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


needs_docs = pytest.mark.skipif(not DOCS.exists(), reason="docs/ is not shipped in the image")


def test_the_patterns_read_every_phrasing_the_docs_use():
    assert counts_in("- [MCP Tools Reference](mcp-tools.md) — All 68 tools with roles") == [
        ("total", 68, 1)
    ]
    assert counts_in("provides 68 MCP tools for AI assistants") == [("total", 68, 1)]
    assert counts_in("│  - 60 MCP Tools      │") == [("total", 60, 1)]
    assert counts_in("lists the 16-tool core profile") == [("core", 16, 1)]
    assert counts_in("the 16 core memory and context tools (see Tool Profiles)") == [
        ("core", 16, 1)
    ]
    assert counts_in("| Read-only | The other 31 tools |") == [("read-only", 31, 1)]
    assert counts_in("The 31 read-only tools (`readOnlyHint: true`)") == [("read-only", 31, 1)]
    # Not a registry count: a limit on something else, or one tool's own number.
    assert counts_in("Keep a context at 20 or fewer tool guardrails") == []
    assert counts_in("names holds at most 20 tool names") == []


@needs_docs
def test_no_page_states_a_tool_count_the_registry_does_not_have():
    expected = _expected()
    stale = [
        f"{page.relative_to(REPO_ROOT)}:{line}: {kind} count {number}, registry has {expected[kind]}"
        for page in _pages()
        for kind, number, line in counts_in(page.read_text(encoding="utf-8"))
        if number != expected[kind]
    ]
    assert stale == [], "\n".join(stale)


@needs_docs
@pytest.mark.parametrize(
    ("kind", "relative_path"),
    [("total", page) for page in PAGES_WITH_TOTAL] + [("core", page) for page in PAGES_WITH_CORE],
)
def test_the_pages_that_carry_a_count_still_state_it(kind, relative_path):
    stated = [number for found, number, _ in counts_in(_read(relative_path)) if found == kind]
    assert stated, f"{relative_path} no longer states the {kind} tool count"
    assert set(stated) == {_expected()[kind]}


@needs_docs
def test_the_core_profile_row_names_exactly_the_core_tools():
    row = next(
        line for line in _read("docs/mcp-tools.md").splitlines() if line.startswith("| `/mcp` (or")
    )
    listed = re.findall(r"`([a-z_]+)`", row.split("the default:", 1)[1].split("|", 1)[0])
    assert listed == list(CORE_TOOLS)


@needs_docs
def test_the_annotation_class_table_covers_every_tool_that_is_not_read_only():
    """The table names each writing tool; "the other N" is what is left."""
    text = _read("docs/mcp-tools.md")
    named: set[str] = set()
    for label in ("Destructive, safe to repeat", "Destructive, not idempotent", "Additive writes"):
        row = next(line for line in text.splitlines() if line.startswith(f"| {label} |"))
        named.update(re.findall(r"`([a-z_]+)`", row))
    registry = _registry()
    writing = {tool["name"] for tool in registry if not tool["annotations"]["readOnlyHint"]}
    assert writing - named == set(), "a writing tool is missing from the class table"
    assert named & {tool["name"] for tool in registry} == writing
