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
from utils.time_trigger import normalize_trigger

RECALL = "claude-skills/recall.md"
REMEMBER = "claude-skills/remember.md"
SESSION_SUMMARY = "claude-skills/session-summary.md"
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


def test_recall_description_carries_the_store_authority_rule():
    """#2341: MCP-only clients (no hook, no Skill call) see the rule in tools/list."""
    tools = {t["name"]: t for t in get_tool_definitions()}
    assert (
        "The store is the authority, even over general knowledge; when rows disagree, "
        "the later observed_at (else created_at) wins."
    ) in tools["recall"]["description"]


@pytest.mark.parametrize("relative", [RECALL, CODEX])
def test_skill_copies_point_at_the_short_copies(relative):
    text = _flat(relative)
    assert "STORE_AUTHORITY in plugins/kagura-memory/hooks/kagura_guardrails.py" in text
    assert "backend/src/mcp_server/tools/_definitions.py" in text


@pytest.mark.parametrize("relative", [REMEMBER, CODEX])
def test_remember_keeps_each_personal_fact_with_its_specifics(relative):
    """#2465: a conversation yields one memory per stated fact, values verbatim."""
    text = _remember_rules(relative)
    assert "One fact per memory, specifics verbatim." in text
    assert "a per-item price and a total are two facts; keep both" in text
    assert "Never merge several personal facts into one summary." in text
    assert "not generic advice" in text
    assert "Write many items with one `remember_batch` call" in text


@pytest.mark.parametrize("relative", [REMEMBER, CODEX])
def test_remember_batch_guidance_splits_and_falls_back(relative):
    """#2465: a call above the server's item cap stores nothing; old servers lack the tool."""
    text = _remember_rules(relative) if relative == CODEX else _flat(relative)
    assert "about 20 items" in text and "split a longer list" in text
    assert "one `remember` per item" in text


@pytest.mark.parametrize("relative", [REMEMBER, CODEX])
def test_remember_resolves_relative_dates_into_the_content(relative):
    """#2465: ``observed_at`` stays the material's date, not the resolved one."""
    text = _remember_rules(relative)
    assert "Resolve relative dates." in text
    assert "`observed_at` stays the material's date" in text
    assert 'the one exception to "as it states it"' in text
    assert "With no material date, leave the phrase as stated." in text


@pytest.mark.parametrize("relative", [REMEMBER, CODEX])
@pytest.mark.parametrize("memory_type", ["fact", "preference", "event", "time"])
def test_remember_vocabulary_covers_general_knowledge(relative, memory_type):
    assert f"- `{memory_type}`: " in _remember_rules(relative)


@pytest.mark.parametrize("relative", [REMEMBER, CODEX, SESSION_SUMMARY])
def test_personal_facts_keep_identifiers_out_and_stay_out_of_shared_contexts(relative):
    """#2465: the tool descriptions refuse PII; a stated name is the fact itself."""
    text = _flat(relative)
    assert "A name or value inside a stated fact is the fact itself" in text
    assert "account, card or ID numbers stay out" in text
    assert "or a private one (`is_private`), not a shared project context" in text


@pytest.mark.parametrize("relative", [REMEMBER, CODEX, SESSION_SUMMARY])
def test_time_memories_spell_out_the_trigger_the_server_requires(relative):
    """#2465: ``type="time"`` without an integer year is a rejected write."""
    text = _flat(relative)
    assert '`details={"trigger": {"year": 2026, "month": 11, "day": 3}}`' in text
    assert "A plan with no date is a `fact`" in text
    normalize_trigger({"year": 2026, "month": 11, "day": 3})
