"""Recall and remember skills: default context, stated facts, time questions.

#2332: a connection with a default context (``?context=<uuid>``, #2125) skips
``list_contexts()`` in recall and remember, as session-start already does.
#2331: remember stores what the material states (no fact-check notes), searches
before writing an updated fact and passes ``supersedes``, and stamps the
material's own date in ``details.observed_at``.
#2333: recall answers count / when / current-value questions from the whole
set, sorted by the date the fact held, newest value current.

Each rule is pinned in the Claude command and in the Codex skill (SYNC pair).
"""

from __future__ import annotations

import pytest

from mcp_server.tools import get_tool_definitions
from tests.plugin.conftest import REPO_ROOT

RECALL = "claude-skills/recall.md"
REMEMBER = "claude-skills/remember.md"
CODEX = "plugins/kagura-memory/skills/kagura-memory/SKILL.md"
DEFAULT_CONTEXT = "Default context: <name>; omit context_id"


def _flat(relative: str) -> str:
    return " ".join((REPO_ROOT / relative).read_text(encoding="utf-8").split())


def _section(text: str, start: str, end: str) -> str:
    head = text.index(start)
    return text[head : text.index(end, head)]


@pytest.mark.parametrize("relative", [RECALL, REMEMBER])
def test_default_context_skips_list_contexts_before_the_listing(relative):
    text = _flat(relative)
    step = _section(text, "### 1. Resolve the target context", "### 2.")
    assert DEFAULT_CONTEXT in step
    assert "skip this step" in step and "without `context_id`" in step
    # The skip comes first; list_contexts() is the "Otherwise" path.
    assert step.index(DEFAULT_CONTEXT) < step.index("Otherwise:") < step.index("list_contexts()")


def _codex(name: str) -> str:
    """One top-level section of the Codex skill (``## Recall``, ``## Remember``)."""
    text = _flat(CODEX)
    head = text.index(f"## {name} ")
    nxt = text.find(" ## ", head + 3)
    return text[head : nxt if nxt != -1 else len(text)]


def _remember_rules(relative: str) -> str:
    if relative == CODEX:
        return _codex("Remember")
    return _section(_flat(relative), "### 2. Parse the input", "### 3. Save")


def _recall_rules(relative: str) -> str:
    if relative == CODEX:
        return _codex("Recall")
    return _section(_flat(relative), "### 2b. When, how many", "### 3. Read the response signals")


def test_codex_recall_and_remember_skip_list_contexts_on_a_default_context():
    for name in ("Recall", "Remember"):
        section = _codex(name)
        assert DEFAULT_CONTEXT in section and "skip `list_contexts()`" in section


@pytest.mark.parametrize("relative", [REMEMBER, CODEX])
def test_remember_stores_facts_as_stated(relative):
    text = _remember_rules(relative)
    assert "Store what the material states, as it states it." in text
    assert "Do not fact-check it" in text
    assert "end years or qualifiers the material does not contain" in text


@pytest.mark.parametrize("relative", [REMEMBER, CODEX])
def test_remember_searches_before_an_update_and_supersedes(relative):
    text = _remember_rules(relative)
    assert 'recall(context_id=..., query="<the subject>", k=3, full=0)' in text
    assert "(no `context_id` on a default context)" in text
    assert "`supersedes=<that memory_id>`" in text
    assert "Never store the new value next to the old one without either." in text


@pytest.mark.parametrize("relative", [REMEMBER, CODEX])
def test_remember_stamps_the_materials_own_date_as_a_plain_date(relative):
    text = _remember_rules(relative)
    assert "`details.observed_at`" in text
    assert "`YYYY-MM-DD`" in text


@pytest.mark.parametrize("relative", [RECALL, CODEX])
def test_recall_time_and_count_questions(relative):
    text = _recall_rules(relative)
    assert "never count from the top five" in text
    assert 'order_by="created_at", include_details=true, limit=50)' in text
    assert "`total`" in text and "`next_cursor`" in text and "`has_more`" in text
    assert "k=20, full=0)" in text
    assert '{"details.observed_at": "2024-03-07"}' in text
    # #2330: the server filters and orders by the event time.
    assert '"observed_after": "2024-03-01", "observed_before": "2024-04-01"' in text
    assert 'order_by="observed_at", direction="asc")' in text
    assert "Sort the rows by that date yourself" not in text
    assert "the later `observed_at`, else the later `created_at`, is current" in text
    assert "`include_superseded=true`" in text
    assert "the store is the authority; never drop a row" in text


def test_recall_signals_point_at_the_time_section():
    text = _flat(RECALL)
    assert "### 2b. When, how many, which is current" in text
    assert "see step 2b" in text


def test_the_guidance_uses_real_tool_parameters():
    tools = {t["name"]: t for t in get_tool_definitions()}
    listing = tools["list"]["inputSchema"]["properties"]
    assert "created_at" in listing["order_by"]["enum"]
    assert "observed_at" in listing["order_by"]["enum"]  # #2330
    assert "observed_after/before" in listing["filters"]["description"]
    assert (
        "observed_after/before"
        in tools["recall"]["inputSchema"]["properties"]["filters"]["description"]
    )
    assert "observed_at" in tools["remember"]["inputSchema"]["properties"]
    assert "include_details" in listing
    assert "details.<key>" in listing["filters"]["description"]
    for key in ("total", "has_more", "next_cursor"):
        assert key in tools["list"]["description"]
    recall = tools["recall"]["inputSchema"]["properties"]
    assert {"k", "full", "include_superseded"} <= set(recall)
    assert "supersedes" in tools["remember"]["inputSchema"]["properties"]
