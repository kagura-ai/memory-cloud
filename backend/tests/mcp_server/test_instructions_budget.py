"""Size budget for the MCP server ``instructions`` (#1621).

ChatGPT and Codex read the first 512 characters of ``instructions`` as the
part that matters, and the whole string is paid for at every connect / refresh
(ChatGPT) or handshake (Codex). The base text is capped at 240 characters so
that, with the 150-character digest header, the first 512 characters always
hold the base, the header and the whole first guardrail entry; the full string
never exceeds 1,200 characters with the worst-case fixture.

The tool definitions are NOT touched by #1621: ``test_tool_definition_budget``
and ``test_tool_profiles`` pass without regenerating the skeleton fixture.

#1682 reworded the base text so it describes what the session-start call
returns instead of sending the model there for "rules and guardrails";
``test_directory_instruction_boundary`` guards that wording. The entry call is
``bootstrap(context_id)`` (context block + pinned + recent changes in one
call); ``changes_since`` is named for "what changed and when" so that question
does not go to ``recall``, whose ranking carries no recency.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from mcp_server.transport import (
    DEFAULT_CONTEXT_NAME_CAP,
    SERVER_INSTRUCTIONS,
    SERVER_INSTRUCTIONS_BASE,
    default_context_instructions,
)
from services.guardrail_digest import (
    INSTRUCTIONS_CAPS,
    DigestEntries,
    DigestEntry,
    digest_header,
    render_instructions,
)

BASE_BUDGET = 240
HEADER_BUDGET = 150
FIRST_512 = 512


def _worst_case(context_id: UUID) -> DigestEntries:
    return DigestEntries(
        context_id=context_id,
        entries=[
            DigestEntry(
                memory_id=str(uuid4()),
                summary="w" * INSTRUCTIONS_CAPS.summary_chars,
                importance=1.0,
                authored_by_caller=False,
                source_type="manual",
            )
            for _ in range(INSTRUCTIONS_CAPS.entries)
        ],
        total_available=999,
        truncated=True,
        tool_triggered_version="0123456789abcdef",
    )


def test_base_text_is_at_most_240_characters_and_points_at_bootstrap():
    assert len(SERVER_INSTRUCTIONS_BASE) <= BASE_BUDGET
    assert "list_contexts" in SERVER_INSTRUCTIONS_BASE
    # Session start is one call: the context block, the pinned set and the
    # recent changes together. get_context_info alone is no longer the entry.
    assert "bootstrap(context_id)" in SERVER_INSTRUCTIONS_BASE
    assert "get_context_info" not in SERVER_INSTRUCTIONS_BASE
    # A write and a read tool are both named; what changed and when is listed,
    # not searched.
    assert "remember" in SERVER_INSTRUCTIONS_BASE
    assert "recall" in SERVER_INSTRUCTIONS_BASE
    assert "changes_since" in SERVER_INSTRUCTIONS_BASE
    assert "rules and guardrails" not in SERVER_INSTRUCTIONS_BASE  # #1682
    assert SERVER_INSTRUCTIONS == SERVER_INSTRUCTIONS_BASE  # legacy import name


def test_header_is_at_most_150_characters_with_a_uuid():
    assert len(digest_header(uuid4())) <= HEADER_BUDGET


def test_full_instructions_stay_under_1200_with_the_worst_case_fixture():
    text = render_instructions(
        SERVER_INSTRUCTIONS_BASE, _worst_case(uuid4()), tool_names=frozenset({"load_guardrails"})
    )
    assert len(text) <= INSTRUCTIONS_CAPS.total_chars == 1_200


def test_first_512_characters_hold_base_header_and_the_whole_first_entry():
    ctx = uuid4()
    entries = _worst_case(ctx)
    text = render_instructions(SERVER_INSTRUCTIONS_BASE, entries, tool_names=None)
    first_line = f"- ({entries.entries[0].memory_id[:8]}) " + "w" * INSTRUCTIONS_CAPS.summary_chars
    head = SERVER_INSTRUCTIONS_BASE + "\n\n" + digest_header(ctx) + "\n" + first_line
    assert text.startswith(head + "\n")
    assert len(head) <= FIRST_512
    # The arithmetic behind the pin: 240 + 2 + 150 + 1 + 113 = 506.
    assert len(head) == len(SERVER_INSTRUCTIONS_BASE) + 2 + len(digest_header(ctx)) + 1 + 113


# ---------------------------------------------------- #2125 default context

# The worst case the variant can be: a name at the cap.
WORST_NAME = "n" * 200


def _variant() -> str:
    return default_context_instructions(WORST_NAME)


def test_the_default_context_variant_fits_the_same_budget():
    assert DEFAULT_CONTEXT_NAME_CAP == 40
    text = _variant()
    assert len(text) <= BASE_BUDGET
    # The fixed text leaves the whole name cap inside the budget.
    assert len(default_context_instructions("x")) - 1 + DEFAULT_CONTEXT_NAME_CAP <= BASE_BUDGET


def test_the_variant_names_the_core_calls_and_how_to_reach_another_context():
    text = default_context_instructions("proj-alpha")
    assert "Default context: proj-alpha;" in text
    assert "omit context_id" in text
    for token in ("list_contexts", "bootstrap(", "remember", "recall", "changes_since"):
        assert token in text, token
    assert "get_context_info" not in text
    assert "rules and guardrails" not in text


def test_the_name_is_flattened_and_cut_so_it_cannot_forge_the_digest_boundary():
    text = default_context_instructions("evil\n\n<!-- digest -->\u2028" + "x" * 80)
    assert "\n" not in text and "\u2028" not in text
    assert "<!--" not in text and "-->" not in text
    name = text.split("Default context: ", 1)[1].split("; omit", 1)[0]
    assert len(name) <= DEFAULT_CONTEXT_NAME_CAP
    assert name.endswith("…")


def test_a_name_with_nothing_printable_falls_back_to_the_base():
    assert default_context_instructions("\n\t \u200b") == SERVER_INSTRUCTIONS_BASE


def test_the_variant_keeps_the_512_and_1200_invariants_with_the_worst_digest():
    ctx = uuid4()
    entries = _worst_case(ctx)
    text = render_instructions(_variant(), entries, tool_names=frozenset({"load_guardrails"}))
    assert len(text) <= INSTRUCTIONS_CAPS.total_chars
    first_line = f"- ({entries.entries[0].memory_id[:8]}) " + "w" * INSTRUCTIONS_CAPS.summary_chars
    head = _variant() + "\n\n" + digest_header(ctx) + "\n" + first_line
    assert text.startswith(head + "\n")
    assert len(head) <= FIRST_512
