"""The core profile is self-describing: a core tool's text may name a tool that
``?profile=core`` does not list only when the same sentence says so and names
where its schema is (``outside the core profile`` + ``guide([...])``). A bare
``create_edge(...)`` or ``use recall_nearby`` hint sends a core client to a
tool its ``tools/list`` never showed.
"""

from __future__ import annotations

import json
import re

import pytest

from mcp_server.tools._definitions import get_tool_definitions
from mcp_server.tools._profiles import CORE_TOOLS

# A sentence ends at a period followed by whitespace, a closing quote or the
# end of the text — so ``e.g.``, ``2.0`` and ``guide(["x"])`` do not split one.
_SENTENCE_END = re.compile(r'\.(?=\s|"|$)')
_QUALIFIER = ("outside the core profile", "guide(")


def _definitions() -> dict[str, dict]:
    return {tool["name"]: tool for tool in get_tool_definitions()}


def _sentence_around(text: str, start: int, end: int) -> str:
    """The sentence of ``text`` that holds ``text[start:end]``."""
    sentence_start = 0
    for match in _SENTENCE_END.finditer(text, 0, start):
        sentence_start = match.end()
    tail = _SENTENCE_END.search(text, end)
    return text[sentence_start : tail.end() if tail else len(text)]


def mentions(text: str, name: str) -> list[str]:
    """Every sentence in which ``name`` appears as a word (``name(`` included)."""
    return [
        _sentence_around(text, match.start(), match.end())
        for match in re.finditer(r"\b" + re.escape(name) + r"\b", text)
    ]


@pytest.mark.parametrize("name", CORE_TOOLS)
def test_core_tool_text_names_only_core_tools_or_says_where_the_other_lives(name):
    definitions = _definitions()
    outside = set(definitions) - set(CORE_TOOLS)
    text = json.dumps(definitions[name], ensure_ascii=False)
    offenders = [
        f"{other}: {sentence.strip()[:160]!r}"
        for other in sorted(outside)
        for sentence in mentions(text, other)
        if not all(marker in sentence for marker in _QUALIFIER)
    ]
    assert offenders == [], f"{name} points a core client at a tool it cannot see:\n" + "\n".join(
        offenders
    )


def test_the_scan_sees_the_known_neighbours():
    """Sanity: the scan finds call forms and bare names (a no-op scan cannot pass)."""
    recall = json.dumps(_definitions()["recall"], ensure_ascii=False)
    assert mentions(recall, "reference")
    assert mentions(recall, "load_pinned")
    assert mentions("Filters: near/within: use recall_nearby. Next sentence.", "recall_nearby") == [
        "Filters: near/within: use recall_nearby."
    ]


def test_a_period_inside_an_example_does_not_split_the_sentence():
    text = 'Numbers as written: 2 is not 2.0. Accept with x(edge_type="a"), outside the core profile; guide(["x"]) documents it. Then more.'
    (sentence,) = mentions(text, "x")[:1]
    assert sentence.startswith(" Accept with x(")
    assert all(marker in sentence for marker in _QUALIFIER)
