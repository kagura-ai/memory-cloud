"""Size budget for the MCP server ``instructions`` (#1621).

ChatGPT and Codex read the first 512 characters of ``instructions`` as the
part that matters, and the whole string is paid for at every connect / refresh
(ChatGPT) or handshake (Codex). The base text is capped at 240 characters so
that, with the 150-character digest header, the first 512 characters always
hold the base, the header and the whole first guardrail entry; the full string
never exceeds 1,200 characters with the worst-case fixture.

The tool definitions are NOT touched by #1621: ``test_tool_definition_budget``
and ``test_tool_profiles`` pass without regenerating the skeleton fixture.

#1682 reworded the base text (233 characters) so it describes what
get_context_info returns instead of sending the model there for "rules and
guardrails"; ``test_directory_instruction_boundary`` guards that wording.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from mcp_server.transport import SERVER_INSTRUCTIONS, SERVER_INSTRUCTIONS_BASE
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


def test_base_text_is_at_most_240_characters_and_points_at_get_context_info():
    assert len(SERVER_INSTRUCTIONS_BASE) <= BASE_BUDGET
    assert "get_context_info(context_id) describes it" in SERVER_INSTRUCTIONS_BASE
    assert "list_contexts" in SERVER_INSTRUCTIONS_BASE
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
