"""The core profile is self-describing: text a ``?profile=core`` client is sent
to — every core tool's definition and the ``guide`` topics its manual expands
to — may name a tool that view does not list only when the same sentence says
so and names where its schema is (``outside the core profile`` +
``describe_tools(``). A bare ``create_edge(...)`` or ``use recall_nearby`` hint
sends a core client to a tool its ``tools/list`` never showed.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest

from mcp_server.tools._definitions import get_tool_definitions
from mcp_server.tools._profiles import CORE_TOOLS
from mcp_server.tools.guide import GUIDE_INDEX, GUIDE_TOPICS

# A sentence ends at a period followed by whitespace or a closing quote, or at
# a line break or the end of the text — so ``e.g.``, ``2.0`` and
# ``describe_tools(["x"])`` do not split one.
_SENTENCE_END = re.compile(r'\.(?=\s|"|$)|\n')
_QUALIFIER = ("outside the core profile", "describe_tools(")


def _definitions() -> dict[str, dict]:
    return {tool["name"]: tool for tool in get_tool_definitions()}


def _strings(value: object) -> Iterator[str]:
    """Every string inside a definition, raw (no JSON escaping)."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _strings(item)


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


def _offenders(texts: list[str], outside: set[str]) -> list[str]:
    return [
        f"{other}: {sentence.strip()[:160]!r}"
        for text in texts
        for other in sorted(outside)
        for sentence in mentions(text, other)
        if not all(marker in sentence for marker in _QUALIFIER)
    ]


@pytest.mark.parametrize("name", CORE_TOOLS)
def test_core_tool_text_names_only_core_tools_or_says_where_the_other_lives(name):
    definitions = _definitions()
    outside = set(definitions) - set(CORE_TOOLS)
    offenders = _offenders(list(_strings(definitions[name])), outside)
    assert offenders == [], f"{name} points a core client at a tool it cannot see:\n" + "\n".join(
        offenders
    )


@pytest.mark.parametrize("name", [tool for tool in CORE_TOOLS if tool in GUIDE_INDEX])
def test_core_tool_manual_names_only_core_tools_or_says_where_the_other_lives(name):
    """``guide([<core tool>])`` is one hop from every core description."""
    outside = set(_definitions()) - set(CORE_TOOLS)
    texts = [GUIDE_TOPICS[topic] for topic in GUIDE_INDEX[name]]
    offenders = _offenders(texts, outside)
    assert offenders == [], (
        f"guide([{name!r}]) points a core client at a tool it cannot see:\n" + "\n".join(offenders)
    )


def test_the_scan_sees_the_known_neighbours():
    """Sanity: the scan finds call forms and bare names (a no-op scan cannot pass)."""
    recall = list(_strings(_definitions()["recall"]))
    assert any(mentions(text, "reference") for text in recall)
    assert any(mentions(text, "load_pinned") for text in recall)
    assert mentions("Filters: near/within: use recall_nearby. Next sentence.", "recall_nearby") == [
        "Filters: near/within: use recall_nearby."
    ]


def test_a_period_inside_an_example_does_not_split_but_a_line_break_does():
    text = (
        'Numbers as written: 2 is not 2.0. Accept with x(edge_type="a"), outside the core '
        'profile; describe_tools(["x"]) has its schema. Then more.\nnear: use y.'
    )
    (sentence,) = mentions(text, "x")[:1]
    assert sentence.startswith(" Accept with x(")
    assert all(marker in sentence for marker in _QUALIFIER)
    # The qualifier on the first line never excuses a bare name on the next.
    (bare,) = mentions(text, "y")
    assert bare == "near: use y."
