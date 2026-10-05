"""The smoke-test runbook stays in step with the tool registry (#1886).

``claude-skills/smoke-test.md`` promises that every registered MCP tool is
either exercised or listed with a reason. It is a live runbook, not a test
suite, so nothing ran it when the registry grew from 64 to 70 tools and the
default ``tools/list`` became the core profile. These tests read the runbook as
data: its Coverage cross-check must name every tool exactly once, its stated
totals must equal the registry, every tool it claims to exercise must have a
call in the steps, and the report's denominators must match its table.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from mcp_server.tools._definitions import get_tool_definitions
from mcp_server.tools._profiles import CORE_TOOLS, DEFAULT_PROFILE

REPO_ROOT = Path(__file__).resolve().parents[3]
RUNBOOK = REPO_ROOT / "claude-skills" / "smoke-test.md"
REGISTRY = [tool["name"] for tool in get_tool_definitions()]

CROSS_CHECK_HEADING = "### 8. Coverage cross-check"
REPORT_HEADING = "### 9. Report"
# A coverage group opens with a bold label that states its size: "**… (41 tools):**".
_GROUP = re.compile(r"^\*\*(?P<label>[^*\n]*?)\((?P<count>\d+) tools\)[^*\n]*\*\*", re.MULTILINE)
_TABLE_ROW_NAME = re.compile(r"^\| `([a-z_]+)` \|", re.MULTILINE)
_NAME = re.compile(r"`([a-z_]+)`")
_SUM_LINE = re.compile(r"^(\d+(?: \+ \d+)+) = \*\*(\d+)\*\*", re.MULTILINE)


def _runbook() -> str:
    return RUNBOOK.read_text(encoding="utf-8")


def _between(text: str, start: str, end: str | None) -> str:
    begin = text.index(start)
    return text[begin : text.index(end, begin) if end else len(text)]


def coverage_groups(text: str) -> list[tuple[str, int, list[str]]]:
    """The cross-check's groups as ``(label, stated size, tool names)``.

    A group's body runs to the next group or to the sum line. A table group
    names its tools in the first cell of each row (the reasons mention other
    tools); a prose group is a comma-separated list of backticked names.
    """
    section = _between(text, CROSS_CHECK_HEADING, REPORT_HEADING)
    heads = list(_GROUP.finditer(section))
    sum_line = _SUM_LINE.search(section)
    groups = []
    for i, head in enumerate(heads):
        if i + 1 < len(heads):
            end = heads[i + 1].start()
        else:
            end = sum_line.start() if sum_line else len(section)
        body = section[head.end() : end]
        names = _TABLE_ROW_NAME.findall(body) or _NAME.findall(body)
        groups.append((head.group("label").strip(), int(head.group("count")), names))
    return groups


def coverage_problems(text: str, registry: list[str]) -> list[str]:
    """Everything that makes the runbook's coverage claim untrue for ``registry``."""
    problems: list[str] = []
    groups = coverage_groups(text)
    if not groups:
        return ["the Coverage cross-check has no '(N tools)' groups"]
    named = Counter(name for _, _, names in groups for name in names)
    for name in registry:
        if named[name] == 0:
            problems.append(f"{name} is registered but in no coverage group")
    for name, times in named.items():
        if times > 1:
            problems.append(f"{name} is in {times} coverage groups")
        if name not in registry:
            problems.append(f"{name} is in a coverage group but not registered")
    for label, stated, names in groups:
        if stated != len(names):
            problems.append(f"group {label!r} states {stated} tools and lists {len(names)}")

    section = _between(text, CROSS_CHECK_HEADING, REPORT_HEADING)
    sum_line = _SUM_LINE.search(section)
    if sum_line is None:
        problems.append("the cross-check has no 'a + b + … = **total**' line")
    else:
        parts = [int(n) for n in sum_line.group(1).split(" + ")]
        if parts != [stated for _, stated, _ in groups]:
            problems.append(f"the sum line {parts} does not follow the group sizes")
        if sum(parts) != int(sum_line.group(2)):
            problems.append("the sum line does not add up")

    totals = {int(n) for n in re.findall(r"\*\*(\d+) tools\*\*", text)}
    totals |= {int(n) for n in re.findall(r"= (\d+) tools\b", text)}
    totals |= {int(n) for n in re.findall(r"= \*\*(\d+)\*\*", text)}
    if not totals:
        problems.append("the runbook states no registry total")
    for total in sorted(totals - {len(registry)}):
        problems.append(f"the runbook states a total of {total}; the registry has {len(registry)}")

    steps = text[: text.index(CROSS_CHECK_HEADING)]
    excluded = {
        name for label, _, names in groups if "exclusion" in label.lower() for name in names
    }
    for name in named:
        called = re.search(rf"^{re.escape(name)}\(", steps, re.MULTILINE) is not None
        if name in excluded and called:
            problems.append(f"{name} is a documented exclusion but the steps call it")
        if name not in excluded and not called:
            problems.append(f"{name} is listed as exercised but no step calls it")
    return problems


def test_runbook_covers_the_registry() -> None:
    assert coverage_problems(_runbook(), REGISTRY) == []


def test_a_tool_removed_from_the_runbook_is_reported() -> None:
    text = _runbook()
    section = _between(text, CROSS_CHECK_HEADING, REPORT_HEADING)
    without = text.replace(section, section.replace("`changes_since`, ", "", 1))
    assert without != text
    problems = coverage_problems(without, REGISTRY)
    assert "changes_since is registered but in no coverage group" in problems


def test_a_tool_added_to_the_registry_is_reported() -> None:
    problems = coverage_problems(_runbook(), [*REGISTRY, "brand_new_tool"])
    assert "brand_new_tool is registered but in no coverage group" in problems
    assert any(p.startswith("the runbook states a total of") for p in problems)


def test_an_exercised_tool_without_a_step_is_reported() -> None:
    text = _runbook()
    without = re.sub(r"^bootstrap\(", "bootstrap_removed(", text, flags=re.MULTILINE)
    assert without != text
    assert "bootstrap is listed as exercised but no step calls it" in coverage_problems(
        without, REGISTRY
    )


def test_prerequisite_matches_the_default_profile() -> None:
    """The run needs the full list because the default view is the core profile."""
    text = _runbook()
    assert DEFAULT_PROFILE == "core", "the default view changed: rewrite the prerequisite"
    stated = {int(n) for n in re.findall(r"(\d+)-tool core profile", text)}
    assert stated == {len(CORE_TOOLS)}
    prerequisite = _between(text, "**Prerequisite", "## Steps")
    assert "?profile=full" in prerequisite
    assert "--tool-profile full" in prerequisite


def test_step_zero_stops_a_default_profile_connection() -> None:
    text = _runbook()
    step_zero = _between(text, "### 0.", "### 1.")
    assert re.search(r"^describe_tools\(\)", step_zero, re.MULTILINE)
    assert "?profile=full" in step_zero
    # Step 0 is read-only and runs before anything is created.
    assert text.index("### 0.") < text.index("create_context(")


def test_report_denominators_match_the_table() -> None:
    report = _between(_runbook(), REPORT_HEADING, None)
    numbered = [int(n) for n in re.findall(r"^\| (\d+) \|", report, re.MULTILINE)]
    assert numbered == list(range(1, len(numbered) + 1)), "main rows are not numbered 1..N"
    agent_rows = re.findall(r"^\| A(\d+) \|", report, re.MULTILINE)
    resource_rows = re.findall(r"^\| P(\d+) \|", report, re.MULTILINE)
    result = re.search(
        r"\*\*Result: N/(\d+) [^*]*\*\* \(\+ N/(\d+) Agent Control Plane rows and N/(\d+)", report
    )
    assert result is not None, "the Result line changed shape"
    assert int(result.group(1)) == len(numbered)
    assert int(result.group(2)) == len(agent_rows)
    assert int(result.group(3)) == len(resource_rows)
    stated_rows = re.search(r"the (\d+) numbered rows", report)
    assert stated_rows is not None and int(stated_rows.group(1)) == len(numbered)
