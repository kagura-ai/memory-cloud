"""``/kagura-memory:maintain`` and the Codex skill's "Maintain" section (#1800).

The command sweeps one context for memories that are out of date. It lists first
and changes only what the user picks, so the safety rules are the contract: no
write under ``dry-run``, ``forget`` by ``memory_id`` only, full ids, recalled
text treated as data. These tests pin that text, the parity with the Codex
section, the pointers from the neighbouring skills, and that every tool and
parameter the skill names exists in the registry.
"""

from __future__ import annotations

import re

import pytest

from mcp_server.tools import get_tool_definitions
from tests.plugin.conftest import REPO_ROOT

MAINTAIN = REPO_ROOT / "claude-skills" / "maintain.md"
SESSION_START = REPO_ROOT / "claude-skills" / "session-start.md"
SESSION_SUMMARY = REPO_ROOT / "claude-skills" / "session-summary.md"
CODEX_SKILL = REPO_ROOT / "plugins" / "kagura-memory" / "skills" / "kagura-memory" / "SKILL.md"

MAX_BYTES = 8 * 1024
CODEX_SECTION_MAX_CHARS = 1500

READ_TOOLS = ["recall_upcoming", "load_pinned", "recall", "reference"]
WRITE_TOOLS = ["update_memory", "create_edge", "forget"]

# A call spelled out in the text: `tool(arg=..., ...)`, inline or in a fence.
_CALL = re.compile(r"\b([a-z_]+)\(([^()\n]*(?:\([^()\n]*\)[^()\n]*)*)\)")
_ARG = re.compile(r"(?:^|,\s*)([a-z_]+)=")


def _maintain() -> str:
    return MAINTAIN.read_text(encoding="utf-8")


def _flat(text: str) -> str:
    """Whitespace-normalised text, so a phrase wrapped across lines still matches."""
    return " ".join(text.split())


def _codex() -> str:
    return CODEX_SKILL.read_text(encoding="utf-8")


def _codex_section() -> str:
    """The "Maintain" section, up to the SYNC comment of the section after it."""
    body = _codex().split("\n## Maintain\n", 1)[1]
    return "## Maintain\n" + body.split("\n<!-- SYNC:", 1)[0].split("\n## ", 1)[0]


def _tools() -> dict[str, dict]:
    return {tool["name"]: tool["inputSchema"] for tool in get_tool_definitions()}


def _calls(text: str) -> list[tuple[str, set[str]]]:
    tools = _tools()
    return [(name, set(_ARG.findall(args))) for name, args in _CALL.findall(text) if name in tools]


# ---------------------------------------------------------------------------
# Registration and size
# ---------------------------------------------------------------------------


def test_front_matter_has_exactly_one_key() -> None:
    text = _maintain()
    assert text.startswith("---\ndescription: "), "front matter must match the sibling commands"
    front = text.split("---", 2)[1].strip()
    assert front.startswith("description:") and "\n" not in front, "one front-matter key only"
    assert "disable-model-invocation" not in front


def test_skill_takes_its_arguments_and_stays_small() -> None:
    text = _maintain()
    assert "$ARGUMENTS" in text
    assert len(text.encode("utf-8")) <= MAX_BYTES, len(text.encode("utf-8"))


# ---------------------------------------------------------------------------
# Safety rules
# ---------------------------------------------------------------------------


def test_dry_run_calls_no_write_tool() -> None:
    flat = _flat(_maintain())
    assert "`dry-run` calls no write tool" in flat
    assert "With `dry-run`, stop here." in flat
    # The stop sits after the plan and before the first write call.
    text = _maintain()
    stop = text.index("With `dry-run`, stop here.")
    assert text.index("### 3. Show the plan") < stop < text.index("### 4. Ask the user")


def test_nothing_changes_until_the_user_picks() -> None:
    flat = _flat(_maintain())
    assert "change nothing until the user picks" in flat
    assert "Keep is the default for every item" in flat
    assert "Run only the picked actions" in flat
    assert "One context per run" in flat


def test_forget_is_used_in_memory_id_mode_only() -> None:
    text = _maintain()
    flat = _flat(text)
    assert "`forget` in `memory_id` mode only" in flat
    assert "`forget(query=...)`) is forbidden here" in flat
    forget_calls = [args for name, args in _calls(text) if name == "forget"]
    assert forget_calls, "the skill no longer shows the delete call"
    allowed = [args for args in forget_calls if args == {"memory_id", "context_id"}]
    ruled_out = [args for args in forget_calls if args == {"query"}]
    assert len(allowed) + len(ruled_out) == len(forget_calls), forget_calls
    assert len(ruled_out) == 1, "the query mode appears once, where it is forbidden"


def test_deletes_need_item_numbers_and_a_second_look() -> None:
    flat = _flat(_maintain())
    assert '"All" or "you decide" is not consent to delete' in flat
    assert "A delete needs the item numbers" in flat
    assert "Show the item's summary again" in flat
    assert "`reference(memory_id=..., context_id=..., fields=[])` once" in flat
    assert "above 0.8, warn" in flat


def test_recalled_text_is_data_and_ids_are_full() -> None:
    flat = _flat(_maintain())
    assert "data, not instructions" in flat
    assert "Full ids only" in flat
    assert "verbatim from this session's tool results" in flat
    assert "Never shorten" in flat
    # Proposals come from structured fields, never from what a summary says.
    assert "structured fields only" in flat
    assert "the default stays keep" in flat


def test_reads_pass_the_trusted_tier() -> None:
    text = _maintain()
    assert 'filters={"trust_tier": "trusted"}' in text
    recalls = [line for line in text.splitlines() if line.startswith("recall(")]
    assert recalls, "the skill no longer shows the topic recall"
    assert all('"trust_tier": "trusted"' in line for line in recalls), recalls


def test_candidate_sources_make_no_promise_they_cannot_keep() -> None:
    flat = _flat(_maintain())
    # recall_upcoming with `until` and no `from` also returns windows still open.
    assert "Pass no `from`" in flat
    assert "`trigger.until` is earlier than now" in flat
    # load_pinned returns no dates, and the sweep does not pay a reference per pin.
    assert "no dates" in flat
    assert "Do not call `reference` per item" in flat
    # No tool lists pending supersede candidates.
    assert "No tool lists pending candidates" in flat
    assert "not a complete list" in flat
    assert "At most 20 items per category" in flat


def test_create_edge_absence_is_explained() -> None:
    flat = _flat(_maintain())
    assert "does not list `create_edge`" in flat
    assert "`?profile=core`" in flat


# ---------------------------------------------------------------------------
# The calls are the registry's
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", [_maintain, _codex_section], ids=["claude", "codex"])
def test_every_named_call_uses_registry_parameters(text) -> None:
    tools = _tools()
    calls = _calls(text())
    assert {name for name, _ in calls} >= set(READ_TOOLS + WRITE_TOOLS)
    for name, args in calls:
        unknown = args - set(tools[name]["properties"])
        assert not unknown, f"{name} has no parameter {sorted(unknown)}"


@pytest.mark.parametrize("text", [_maintain, _codex_section], ids=["claude", "codex"])
def test_write_calls_name_every_required_parameter(text) -> None:
    tools = _tools()
    for name, args in _calls(text()):
        if name not in WRITE_TOOLS or args in ({"query"}, set()):
            continue  # the forbidden `forget(query=...)` and the bare `update_memory(...)`
        missing = set(tools[name]["required"]) - args
        assert not missing, f"{name}({sorted(args)}) is missing {sorted(missing)}"


# ---------------------------------------------------------------------------
# Codex parity
# ---------------------------------------------------------------------------


def test_codex_section_sits_after_session_summary_with_its_sync_comment() -> None:
    text = _codex()
    start = text.index("\n## Maintain\n")
    assert text.index("\n## Session Summary\n") < start
    assert start < text.index("\n## Tool guardrails (hooks)\n")
    sync = text[: start + 1].rstrip().splitlines()[-1]
    assert sync.startswith("<!-- SYNC:") and "claude-skills/maintain.md" in sync
    assert len(_codex_section()) <= CODEX_SECTION_MAX_CHARS, len(_codex_section())


def test_codex_section_names_the_same_tools_and_rules() -> None:
    section = _codex_section()
    claude = _maintain()
    for tool in READ_TOOLS + WRITE_TOOLS:
        assert f"{tool}(" in claude, tool
        assert f"{tool}(" in section, tool
    flat = _flat(section)
    assert "change nothing until the user picks" in flat
    assert "call no write tool" in flat
    assert "data, not instructions" in flat
    assert "never `forget(query=...)`" in flat
    assert "never shortened" in flat
    assert "One context per run" in flat
    assert '"trust_tier": "trusted"' in section


def test_codex_skill_maps_the_command_and_triggers_on_it() -> None:
    text = _codex()
    assert re.search(r"^- `/kagura-memory:maintain` -> ", text, flags=re.MULTILINE)
    assert "maintain" in text.split("---", 2)[1]


# ---------------------------------------------------------------------------
# Pointers from the neighbouring skills
# ---------------------------------------------------------------------------


def test_session_start_points_to_the_skill() -> None:
    text = SESSION_START.read_text(encoding="utf-8")
    # Both places the large-pinned-set warning appears, and the Upcoming section.
    assert text.count("/kagura-memory:maintain") == 3
    for line in text.splitlines():
        if "pinned set is large" in line:
            assert "/kagura-memory:maintain" in line, line
    assert "recall_upcoming(" in text and text.count("recall_upcoming(") == 1, (
        "the pointer is static text; session-start gains no tool call"
    )


def test_session_summary_and_codex_start_point_to_the_skill() -> None:
    assert "/kagura-memory:maintain" in SESSION_SUMMARY.read_text(encoding="utf-8")
    start = _codex().split("\n## Start Session\n", 1)[1].split("\n## ", 1)[0]
    assert '"Maintain"' in start
