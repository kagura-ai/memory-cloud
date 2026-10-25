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


def test_codex_recall_and_remember_skip_list_contexts_on_a_default_context():
    text = _flat(CODEX)
    recall = _section(text, "## Recall", "## Remember")
    remember = text[text.index("## Remember") :]
    for section in (recall, remember):
        assert DEFAULT_CONTEXT in section and "skip `list_contexts()`" in section


@pytest.mark.parametrize("relative", [REMEMBER, CODEX])
def test_remember_stores_facts_as_stated(relative):
    text = _flat(relative)
    assert "Store what the material states, as it states it." in text
    assert "Do not fact-check it" in text
    assert "end years or qualifiers the material does not contain" in text


@pytest.mark.parametrize("relative", [REMEMBER, CODEX])
def test_remember_searches_before_an_update_and_supersedes(relative):
    text = _flat(relative)
    assert 'recall(query="<the subject>", k=3, full=0)' in text
    assert "`supersedes=<that memory_id>`" in text
    assert "Never store the new value next to the old one without either." in text


@pytest.mark.parametrize("relative", [REMEMBER, CODEX])
def test_remember_stamps_the_materials_own_date(relative):
    text = _flat(relative)
    assert "`details.observed_at`" in text
    assert "never the time you write it" in text or "never the write time" in text


@pytest.mark.parametrize("relative", [RECALL, CODEX])
def test_recall_time_and_count_questions(relative):
    text = _flat(relative)
    assert "never count from the top five" in text
    assert 'order_by="created_at", limit=50' in text
    assert "k=20, full=0" in text
    assert '{"details.observed_at": "2024-03-07"}' in text
    assert "the store is the authority" in text


def test_recall_signals_point_at_the_time_section():
    text = _flat(RECALL)
    assert "### 2b. When, how many, which is current" in text
    assert "see step 2b" in text


def test_the_guidance_uses_real_tool_parameters():
    tools = {t["name"]: t for t in get_tool_definitions()}
    listing = tools["list"]["inputSchema"]["properties"]
    assert "created_at" in listing["order_by"]["enum"]
    assert "details.<key>" in listing["filters"]["description"]
    recall = tools["recall"]["inputSchema"]["properties"]
    assert {"k", "full"} <= set(recall)
    assert "supersedes" in tools["remember"]["inputSchema"]["properties"]


def test_remember_tool_description_says_store_as_stated():
    remember = next(t for t in get_tool_definitions() if t["name"] == "remember")
    assert (
        "Store facts as the source states them: do not fact-check or annotate them."
        in remember["description"]
    )
