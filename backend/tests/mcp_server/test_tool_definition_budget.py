"""Size budgets and a structural snapshot for the MCP tool definitions (#1602).

``tools/list`` is paid for by every client on every session, and most of it is
prose: tool descriptions and parameter descriptions. #1602 trimmed that prose to
what an agent needs to call a tool correctly and moved tutorials to
``docs/mcp-tools.md`` and the plugin's ``guide`` skill. Two guards keep it that
way:

* **Budgets** — sizes are measured the way an agent reads a definition: compact
  JSON, UTF-8 (``ensure_ascii=False``). Each budget has a ceiling and a floor:
  a definition may not grow past its ceiling, and a ceiling may not sit more
  than ``MAX_SLACK`` above the measured size, so the constants have to follow
  the text down instead of drifting into a number nobody checks.
* **Skeleton** — trimming is a text-only change. Every definition with its
  ``description`` strings removed must equal the committed snapshot, which was
  generated from the registry *before* any text was touched. Names, types,
  ``required``, enums, bounds, ``additionalProperties`` and ``readOnly`` flags
  are therefore pinned, in order.

A deliberate schema change (a new tool or parameter) regenerates the snapshot::

    UPDATE_TOOL_SKELETON=1 pytest tests/mcp_server/test_tool_definition_budget.py

and the diff of ``fixtures/tool_schema_skeleton.json`` is reviewed like code.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import pytest

from mcp_server.tools import get_tool_definitions
from mcp_server.tools._profiles import CORE_TOOLS

SKELETON_PATH = Path(__file__).parent / "fixtures" / "tool_schema_skeleton.json"

# Ceilings, in characters of compact UTF-8 JSON. Measured after the trim: full
# list 81,705 (was 110,608), core list 28,254 (was 44,513), recall 6,362 (was
# 11,298), remember 5,904 (was 10,361).
#
# The full list was aimed at 78,000 and stops at 81,705: what is left is the
# schema skeleton (21,391), the "Returns:" contracts, one line of meaning per
# parameter and the rules an agent must not lose (SECURITY, supersedes, trust
# tier, error codes). Going lower means cutting those, not prose.
FULL_LIST_BUDGET = 84_000
CORE_LIST_BUDGET = 31_000
RECALL_BUDGET = 6_500
REMEMBER_BUDGET = 6_000
PER_TOOL_BUDGET = 6_500

# A ceiling more than this far above the measured size is a stale constant.
MAX_SLACK = 0.15

# The ``core`` profile (#1601); ``test_tool_profiles`` pins its membership and
# its size on the wire (``json.dumps``), this file its size as an agent reads it.
CORE_TOOL_NAMES = frozenset(CORE_TOOLS)


def _size(obj: Any) -> int:
    """Characters of ``obj`` as compact UTF-8 JSON."""
    return len(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))


def _tool(name: str) -> dict:
    return next(tool for tool in get_tool_definitions() if tool["name"] == name)


def _strip_descriptions(node: Any) -> Any:
    """Copy ``node`` without its ``description`` *strings*.

    A JSON Schema annotation is ``"description": "<text>"``. A parameter that is
    itself named ``description`` (``create_context`` has one) maps to a schema
    object, not a string, so it stays in the skeleton.
    """
    if isinstance(node, dict):
        return {
            key: _strip_descriptions(value)
            for key, value in node.items()
            if not (key == "description" and isinstance(value, str))
        }
    if isinstance(node, list):
        return [_strip_descriptions(item) for item in node]
    return node


def _skeleton() -> list[dict]:
    return [_strip_descriptions(tool) for tool in get_tool_definitions()]


def _assert_within(size: int, budget: int, what: str) -> None:
    assert size <= budget, (
        f"{what} is {size} characters (budget {budget}). Trim the text, or move a "
        "tutorial to docs/mcp-tools.md — do not just raise the budget."
    )
    floor = int(budget * (1 - MAX_SLACK))
    assert size >= floor, (
        f"{what} is {size} characters, more than {MAX_SLACK:.0%} under its budget of "
        f"{budget}. Lower the budget so it keeps guarding the size."
    )


# --------------------------------------------------------------------- budgets


def test_full_list_stays_within_its_budget():
    _assert_within(_size(get_tool_definitions()), FULL_LIST_BUDGET, "the full tools/list")


def test_core_list_stays_within_its_budget():
    core = [tool for tool in get_tool_definitions() if tool["name"] in CORE_TOOL_NAMES]
    assert {tool["name"] for tool in core} == CORE_TOOL_NAMES
    _assert_within(_size(core), CORE_LIST_BUDGET, "the core tools/list")


@pytest.mark.parametrize(
    ("name", "budget"),
    [("recall", RECALL_BUDGET), ("remember", REMEMBER_BUDGET)],
)
def test_the_two_largest_tools_stay_within_their_budgets(name, budget):
    _assert_within(_size(_tool(name)), budget, f"the {name} definition")


def test_no_single_tool_exceeds_the_per_tool_budget():
    sizes = {tool["name"]: _size(tool) for tool in get_tool_definitions()}
    over = {name: size for name, size in sizes.items() if size > PER_TOOL_BUDGET}
    assert over == {}, f"tools over {PER_TOOL_BUDGET} characters: {over}"
    largest = max(sizes, key=lambda name: sizes[name])
    _assert_within(sizes[largest], PER_TOOL_BUDGET, f"the largest definition ({largest})")


# -------------------------------------------------------------------- skeleton


def test_schema_structure_matches_the_committed_skeleton():
    """Descriptions may change; nothing else in a definition may.

    Compared as serialized text so key order counts too — a client sees
    parameters in the order the registry lists them.
    """
    current = json.dumps(_skeleton(), ensure_ascii=False, indent=2) + "\n"
    if os.environ.get("UPDATE_TOOL_SKELETON") == "1":
        SKELETON_PATH.parent.mkdir(parents=True, exist_ok=True)
        SKELETON_PATH.write_text(current, encoding="utf-8")
    committed = SKELETON_PATH.read_text(encoding="utf-8")
    if current != committed:
        expected = {tool["name"]: tool for tool in json.loads(committed)}
        actual = {tool["name"]: tool for tool in _skeleton()}
        changed = sorted(
            name
            for name in expected.keys() | actual.keys()
            if expected.get(name) != actual.get(name)
        )
        order = "" if changed else " (same content, different order)"
        pytest.fail(
            f"tool definitions changed structurally: {changed}{order}. If that is "
            "intended, regenerate the snapshot (see this module's docstring)."
        )


def test_every_tool_and_parameter_still_has_a_description():
    """The skeleton test ignores description text, so an emptied one needs its own guard."""

    def undescribed(schema: dict, path: str) -> list[str]:
        missing = []
        for param, spec in (schema.get("properties") or {}).items():
            where = f"{path}.{param}"
            if not (isinstance(spec.get("description"), str) and spec["description"].strip()):
                missing.append(where)
        return missing

    missing = []
    for tool in get_tool_definitions():
        if not tool.get("description", "").strip():
            missing.append(tool["name"])
        missing += undescribed(tool.get("inputSchema", {}), tool["name"])
    assert missing == [], f"definitions without a description: {missing}"


# ------------------------------------------------------------------- keep-list


def _all_text(name: str) -> str:
    """A tool's description plus every top-level parameter description."""
    tool = _tool(name)
    props = tool.get("inputSchema", {}).get("properties", {})
    return "\n".join([tool["description"], *(p.get("description", "") for p in props.values())])


@pytest.mark.parametrize("name", ["remember", "update_memory"])
def test_write_tools_keep_the_security_rule_and_the_location_exception(name):
    text = _all_text(name)
    assert "SECURITY" in text
    for term in ("secrets", "PII", "redact"):
        assert term in text, f"{name}: SECURITY rule lost {term!r}"
    assert "details.location" in text and "never in context" in text


def test_recall_keeps_the_signals_an_agent_acts_on():
    text = _all_text("recall")
    for term in (
        "trust_tier='trusted'",
        "degraded",
        "high|moderate|low|none",
        "external source",
        "dismiss_supersede_candidate",
        "absent, never null",
    ):
        assert term in text, f"recall lost {term!r}"


def test_tool_selection_guidance_names_the_neighbours():
    recall = _tool("recall")["description"]
    for neighbour in ("reference(", "explore(", "load_pinned(", "recall_upcoming("):
        assert neighbour in recall
    assert "update_memory" in _tool("remember")["description"]
    assert "remember" in _tool("update_memory")["description"]


def test_descriptions_carry_no_issue_numbers():
    """Provenance belongs in commits and docs; an agent cannot resolve ``#1208``."""
    offenders = []
    for tool in get_tool_definitions():
        text = json.dumps(tool, ensure_ascii=False)
        offenders += [f"{tool['name']}: {hit}" for hit in re.findall(r"(?:Issue )?#\d{2,}", text)]
    assert offenders == []
