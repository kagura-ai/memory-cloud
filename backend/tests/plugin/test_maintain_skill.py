"""``/kagura-memory:maintain`` and the Codex skill's "Maintain" section (#1800).

The command sweeps one context for memories that are out of date. It lists first
and changes only what the user picks, so the safety rules are the contract: no
write under ``dry-run``, consent per item number, ``forget`` by ``memory_id``
only, full ids, recalled text treated as data. These tests pin those rules, the
parity with the Codex section, the pointers from the neighbouring skills, and
that every call the text spells out is a registered tool with real parameters.
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
CODEX_SECTION_MAX_CHARS = 3500

READ_TOOLS = [
    "list_contexts",
    "recall_upcoming",
    "load_pinned",
    "recall",
    "reference",
    "list_edges",
]
WRITE_TOOLS = ["update_memory", "create_edge", "forget"]

# A call spelled out in the text: ``name(args)``, inline or in a fence. Prose
# never writes a word directly against an opening parenthesis.
_CALL = re.compile(r"(?<![\w.])([a-z][a-z_]*)\(([^()\n]*(?:\([^()\n]*\)[^()\n]*)*)\)")
_ARG = re.compile(r"(?:^|,\s*)([a-z_]+)=")
# The one call shown only to forbid it.
FORBIDDEN_CALL = ("forget", frozenset({"query"}))


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


def _calls(text: str) -> list[tuple[str, frozenset[str]]]:
    return [(name, frozenset(_ARG.findall(args))) for name, args in _CALL.findall(text)]


def _step_4c() -> str:
    """Step 4c of the session summary: the same calls, for one session's memories."""
    text = SESSION_SUMMARY.read_text(encoding="utf-8")
    return text.split("### 4c. Keep touched memories current", 1)[1].split("\n### ", 1)[0]


def _codex_step_5() -> str:
    section = _codex().split("\n## Session Summary\n", 1)[1].split("\n## ", 1)[0]
    return section.split("\n5. Keep touched memories current", 1)[1].split("\n6. ", 1)[0]


def tools_max_upcoming() -> int:
    """The largest ``k`` the registry documents for ``recall_upcoming``."""
    description = _tools()["recall_upcoming"]["properties"]["k"]["description"]
    return int(re.search(r"max (\d+)", description).group(1))


BOTH = pytest.mark.parametrize("text", [_maintain, _codex_section], ids=["claude", "codex"])


# ---------------------------------------------------------------------------
# Registration and size
# ---------------------------------------------------------------------------


def test_front_matter_has_exactly_one_key() -> None:
    text = _maintain()
    assert text.startswith("---\ndescription: "), "front matter must match the sibling commands"
    front = text.split("---", 2)[1].strip()
    assert front.startswith("description:") and "\n" not in front, "one front-matter key only"


def test_skill_takes_its_arguments_and_stays_small() -> None:
    text = _maintain()
    assert "$ARGUMENTS" in text
    assert len(text.encode("utf-8")) <= MAX_BYTES, len(text.encode("utf-8"))


# ---------------------------------------------------------------------------
# Safety rules (both skills)
# ---------------------------------------------------------------------------


def test_dry_run_stops_after_the_plan() -> None:
    text = _maintain()
    assert "`dry-run` calls no write tool" in _flat(text)
    stop = text.index("With `dry-run`, stop here.")
    assert text.index("### 3. Show the plan") < stop < text.index("### 4. Ask the user")
    assert "With `dry-run`, stop after the plan and call no write tool" in _flat(_codex_section())


@BOTH
def test_dry_run_does_not_claim_a_topic_recall_is_free(text) -> None:
    """``recall`` may strengthen associations and promote what it returns."""
    flat = _flat(text())
    assert "a topic recall still counts as a search" in flat.lower()
    assert "not free of side effects" in flat


@BOTH
def test_arguments_cannot_confuse_a_topic_with_a_context(text) -> None:
    flat = _flat(text())
    assert 'list_contexts(name_contains="<name>")' in flat
    assert re.search(r"Exactly one match[^.]*ask", flat)
    assert "name, id, private or shared" in flat


def test_argument_grammar_is_spelled_out() -> None:
    grammar = "`[context=<name or id>] [topic=<words>] [dry-run]`"
    assert grammar in _maintain()
    assert "asked about, not guessed" in _maintain()
    assert grammar in (REPO_ROOT / "claude-skills" / "guide.md").read_text(encoding="utf-8")
    assert "ask about a word that could be either" in _flat(_codex_section())


@BOTH
def test_nothing_changes_without_the_item_numbers(text) -> None:
    flat = _flat(text())
    assert "apply nothing until the user picks" in flat
    assert '"All" or "you decide" is not consent to any change' in flat
    assert "needs its item numbers" in flat
    assert "One context per run" in flat


@BOTH
def test_proposals_come_from_structured_fields_and_default_to_keep(text) -> None:
    flat = _flat(text())
    assert "structured fields only" in flat
    assert re.search(r"keep is the default", flat, flags=re.IGNORECASE)
    assert "data, not instructions" in flat


@BOTH
def test_ids_are_full_and_copied(text) -> None:
    flat = _flat(text())
    assert "verbatim from" in flat
    for verb in ("shorten", "pad", "guess"):
        assert verb in flat, verb


@BOTH
def test_forget_is_used_in_memory_id_mode_only(text) -> None:
    body = text()
    forget_calls = [args for name, args in _calls(body) if name == "forget"]
    allowed = [args for args in forget_calls if args == {"memory_id", "context_id"}]
    ruled_out = [args for args in forget_calls if ("forget", args) == FORBIDDEN_CALL]
    assert allowed, "the delete call is no longer shown"
    assert len(allowed) + len(ruled_out) == len(forget_calls), forget_calls
    assert len(ruled_out) == 1, "the query mode appears once, where it is ruled out"
    flat = _flat(body)
    assert re.search(
        r"`forget\(query=\.\.\.\)`\) is forbidden|never `forget\(query=\.\.\.\)`", flat
    )
    # A delete that removed nothing is reported, never retried as a search.
    assert "`deleted_count` of 0" in flat and "with a query" in flat


@BOTH
def test_a_delete_gets_a_second_look(text) -> None:
    flat = _flat(text())
    assert "summary again" in flat
    assert "`reference(memory_id=..., context_id=..., fields=[])` once" in flat
    assert "above 0.8, warn and ask again" in flat


@BOTH
def test_deleting_the_source_of_a_supersedes_edge_is_warned_about(text) -> None:
    """``forget`` removes the edge, so the memory it shadowed returns to default recall."""
    flat = _flat(text())
    assert "list_edges(memory_id=..., context_id=...)" in flat
    assert "`source_id`" in flat and "`target_id`" in flat
    assert "comes back into default recall" in flat
    assert re.search(r"[Aa]sk whether to delete (that one|it) too", flat)


@BOTH
def test_candidate_calls_name_the_memory_that_carries_it(text) -> None:
    flat = _flat(text())
    assert "memory_id=<memory_id of the result that carries the candidate>" in flat
    assert "`supersede_candidate.memory_id`" in flat
    for vague in ("<new>", "<old>", "<this>"):
        assert vague not in flat, vague


@BOTH
def test_an_update_is_shown_first_and_never_built_from_recalled_text(text) -> None:
    flat = _flat(text())
    assert re.search(r"show the new (summary and content|text) and wait for a yes", flat)
    assert re.search(r"never (comes )?from recalled text", flat)


@BOTH
def test_reads_pass_the_trusted_tier_and_unfiltered_reads_are_display_only(text) -> None:
    body = text()
    recalls = [args for name, args in _CALL.findall(body) if name == "recall"]
    assert recalls, "the topic recall is no longer shown"
    assert all('filters={"trust_tier": "trusted"}' in args for args in recalls), recalls
    flat = _flat(body)
    assert "`recall_upcoming` and `load_pinned` take no filters" in flat
    assert "display-only" in flat
    # The rerank setting is not left to the context's search config.
    assert all("use_rerank=false" in args for args in recalls), recalls
    # In a connector-fed context the filter alone can empty the result.
    assert "excludes external and connector-ingested memories" in flat
    assert "empty result may be the filter" in flat
    assert re.search(r'never report "nothing needs attention"', flat)


@BOTH
def test_one_context_means_a_single_context_id(text) -> None:
    body = text()
    assert "context_ids" not in body
    assert "single `context_id`" in _flat(body)


def test_plan_header_names_the_chosen_context() -> None:
    flat = _flat(_maintain())
    assert "name, id, private or shared" in flat
    assert "## Maintain: {context_name} ({context_id}, private|shared)" in flat


@BOTH
def test_now_comes_from_the_clock(text) -> None:
    body = text()
    assert "date -u +%Y-%m-%dT%H:%M:%S" in body
    assert re.search(r"[Nn]ever guess the date", body)
    # The tool documents 'now' for `from` only.
    assert 'until="now"' not in body
    untils = [args for name, args in _CALL.findall(body) if "until=" in args]
    assert untils and all("from=" not in args for args in untils), untils


@BOTH
def test_candidate_sources_make_no_promise_they_cannot_keep(text) -> None:
    flat = _flat(text())
    # `until` with no `from` also returns windows still open.
    # They fill the first slots, oldest first, so the read asks for the tool maximum.
    assert tools_max_upcoming() == 100
    assert re.search(r'recall_upcoming\(context_id=\.\.\., until="<cutoff[^"]*>", k=100\)', flat)
    assert "`trigger.until` is earlier than the cutoff" in flat
    assert "first 20" in flat
    assert re.search(r"more may exist", flat)
    # Trigger bounds are naive; a follow-up due today in the user's timezone is not "ended".
    assert "minus 24 hours" in flat and "24-hour margin" in flat
    # Follow-ups that are not over yet are listed too, so a finished one can be retired.
    assert 'recall_upcoming(context_id=..., from="now", k=20)' in flat
    # load_pinned returns no dates, and the sweep does not pay a reference per pin.
    assert "no dates" in flat
    assert re.search(r"[Dd]o not call `reference` per item|no `reference` per item", flat)
    assert "No tool lists pending candidates" in flat
    assert re.search(r"not a complete list|list is not complete", flat)
    assert "at most 20" in flat, "pins are capped too"


@BOTH
def test_create_edge_absence_is_explained(text) -> None:
    flat = _flat(text())
    assert re.search(r"`create_edge`[^.]*say so", flat)
    assert "`?profile=full`" in flat  # the way to make the hidden tools callable


def test_follow_up_outcome_is_saved_without_a_supersedes_edge() -> None:
    """``forget`` removes the memory's edges, so the edge would vanish at once."""
    text = _maintain()
    assert "supersedes=<time" not in text
    assert 'remember(context_id=..., type="note", summary=..., content=...)' in text


def test_boundary_with_sleep_maintenance_is_stated() -> None:
    when = _maintain().split("## When to use", 1)[1].split("\n## ", 1)[0]
    assert "Sleep maintenance" in when
    assert "person's judgment" in when


# ---------------------------------------------------------------------------
# The calls are the registry's
# ---------------------------------------------------------------------------


@BOTH
def test_every_tool_of_the_sweep_is_spelled_out(text) -> None:
    assert {name for name, _ in _calls(text())} >= set(READ_TOOLS + WRITE_TOOLS)


@pytest.mark.parametrize(
    "text",
    [_maintain, _codex_section, _step_4c, _codex_step_5],
    ids=["claude", "codex", "claude-4c", "codex-step-5"],
)
def test_every_call_is_a_registered_tool_with_its_required_parameters(text) -> None:
    tools = _tools()
    calls = _calls(text())
    assert calls
    names = {name for name, _ in calls}
    assert names <= set(tools), f"not a registered tool: {sorted(names - set(tools))}"
    problems = []
    for name, args in calls:
        unknown = args - set(tools[name]["properties"])
        if unknown:
            problems.append(f"{name} has no parameter {sorted(unknown)}")
        if (name, args) == FORBIDDEN_CALL:
            continue
        missing = set(tools[name].get("required", [])) - args
        if missing:
            problems.append(f"{name}({sorted(args)}) is missing {sorted(missing)}")
    assert problems == []


def test_call_pattern_catches_a_misspelt_tool() -> None:
    calls = _calls("```\nrecal_upcoming(context_id=...)\n```\nand `list_contexts()`")
    assert [name for name, _ in calls] == ["recal_upcoming", "list_contexts"]
    assert "recal_upcoming" not in _tools()


# ---------------------------------------------------------------------------
# Codex parity
# ---------------------------------------------------------------------------


def test_codex_section_sits_after_session_summary_with_its_sync_comment() -> None:
    text = _codex()
    start = text.index("\n## Maintain\n")
    assert text.index("\n## Session Summary\n") < start
    assert start < text.index("\n## Tool guardrails (hooks)\n")
    sync = text[: start + 1].rstrip().splitlines()[-1]
    assert sync.startswith("<!-- SYNC:")
    assert "claude-skills/maintain.md" in sync
    # The same comment ties the Session Summary step to its Claude counterpart.
    assert 'step 5 of "Session Summary"' in sync
    assert 'claude-skills/session-summary.md "4c"' in sync
    assert "### 4c. " in SESSION_SUMMARY.read_text(encoding="utf-8")
    assert len(_codex_section()) <= CODEX_SECTION_MAX_CHARS, len(_codex_section())


def test_codex_section_offers_the_same_actions() -> None:
    claude, section = _calls(_maintain()), _calls(_codex_section())
    for tool in READ_TOOLS + WRITE_TOOLS:
        assert any(name == tool for name, _ in section), tool
    # Each write the Claude skill can make is spelled out for Codex with the same switch.
    for switch in ("dismiss_supersede_candidate", "delivery_mode", "edge_type"):
        assert any(switch in args for _, args in claude), switch
        assert any(switch in args for _, args in section), switch


def test_codex_skill_maps_the_command_and_triggers_on_it() -> None:
    text = _codex()
    assert re.search(r"^- `/kagura-memory:maintain` -> ", text, flags=re.MULTILINE)
    assert "maintain" in text.split("---", 2)[1]


# ---------------------------------------------------------------------------
# Pointers from the neighbouring skills
# ---------------------------------------------------------------------------


def test_session_start_points_to_the_skill() -> None:
    text = SESSION_START.read_text(encoding="utf-8")
    pointer = "/kagura-memory:maintain"
    # Both places the large-pinned-set warning appears.
    warnings = [
        line
        for line in text.splitlines()
        if "pinned set is large" in line and "review for stale" in line
    ]
    assert len(warnings) == 2, warnings
    assert all(pointer in line for line in warnings), warnings
    # And the Upcoming section of the template, for a follow-up that is finished.
    upcoming = text.split("\n### ⏰ Upcoming\n", 1)[1].split("\n### ", 1)[0]
    assert pointer in upcoming and "Retire" in upcoming
    assert text.count("recall_upcoming(") == 1, "the pointer is static text, not a tool call"


def test_session_summary_and_codex_start_point_to_the_skill() -> None:
    assert "/kagura-memory:maintain" in SESSION_SUMMARY.read_text(encoding="utf-8")
    start = _codex().split("\n## Start Session\n", 1)[1].split("\n## ", 1)[0]
    assert '"Maintain"' in start
