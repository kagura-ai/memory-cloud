"""Recall and remember skills: default context, stated facts, time questions.

#2332: a connection with a default context (``?context=<uuid>``, #2125) skips
``list_contexts()`` in recall and remember, as session-start already does.
#2331: remember stores what the material states (no fact-check notes), searches
before writing an updated fact and passes ``supersedes``, and stamps the
material's own date in ``details.observed_at``.
#2333: recall answers count / when / current-value questions from the whole
set, sorted by the date the fact held, newest value current.
#2690: the skills also accept the SessionStart hook's default-context line,
load their tools with one ``ToolSearch`` call and write with
``tags_normalize=true`` instead of calling ``list_tags`` first.

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
SESSION_START = "claude-skills/session-start.md"
CODEX = "plugins/kagura-memory/skills/kagura-memory/SKILL.md"
DEFAULT_CONTEXT = "Default context: <name>; omit context_id"


def _flat(relative: str) -> str:
    return " ".join((REPO_ROOT / relative).read_text(encoding="utf-8").split())


def _section(text: str, start: str, end: str) -> str:
    head = text.index(start)
    return text[head : text.index(end, head)]


CONTEXT_STEPS = {
    RECALL: ("### 1. Resolve the target context", "### 2."),
    REMEMBER: ("### 1. Resolve the target context", "### 2."),
    SESSION_SUMMARY: ("### 3. Get the target context", "### 4."),
}


@pytest.mark.parametrize("relative", [RECALL, REMEMBER, SESSION_SUMMARY])
def test_default_context_skips_list_contexts_before_the_listing(relative, hook_module):
    text = _flat(relative)
    step = _section(text, *CONTEXT_STEPS[relative])
    assert DEFAULT_CONTEXT in step
    # #2690: the SessionStart hook line is a source too (its prefix, quoted).
    assert f'"{hook_module.DEFAULT_CONTEXT_PREFIX.strip()}"' in step
    assert "skip this step" in step and "without `context_id`" in step
    # The skip comes first; list_contexts() is the "Otherwise" path.
    assert step.index(DEFAULT_CONTEXT) < step.index("Otherwise:") < step.index("list_contexts()")


def test_session_start_skip_quotes_the_hook_prefix(hook_module):
    """#2690: session-start's default-context skip accepts the hook line too."""
    text = _flat(SESSION_START)
    head = text.index("**When the connection has a default context**")
    skip = text[head : text.index("Otherwise:", head)]
    assert DEFAULT_CONTEXT in skip and "skip `list_contexts()`" in skip
    assert f'"{hook_module.DEFAULT_CONTEXT_PREFIX.strip()}"' in skip


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


def test_codex_recall_and_remember_skip_list_contexts_on_a_default_context(hook_module):
    for name in ("Recall", "Remember"):
        section = _codex(name)
        assert DEFAULT_CONTEXT in section and "skip `list_contexts()`" in section
        assert f'"{hook_module.DEFAULT_CONTEXT_PREFIX.strip()}"' in section  # #2690


def _write_rules(relative: str) -> str:
    """Where a skill decides its tags and makes the write (#2690)."""
    if relative == CODEX:
        return _codex("Remember")
    if relative == SESSION_SUMMARY:
        return _section(_flat(relative), "### 4. Save each item", "### 4a.")
    return _section(_flat(relative), "### 2. Parse the input", "### 4. Confirm")


@pytest.mark.parametrize("relative", [REMEMBER, SESSION_SUMMARY, CODEX])
def test_remember_writes_with_tags_normalize_and_no_list_tags(relative):
    """#2690: the server folds tag spelling variants, so no list_tags round trip."""
    text = _write_rules(relative)
    assert "tags_normalize=true" in text
    assert "`tag_normalized`" in text
    assert "no spelling variants" in text
    assert "list_tags(context_id=...)` first" not in text
    assert "Call `list_tags`" not in text
    assert "spellings `list_tags(" not in text


def test_remember_default_context_skip_no_longer_calls_list_tags():
    step = _section(_flat(REMEMBER), "### 1. Resolve the target context", "### 2.")
    assert "list_tags" not in step
    assert "list_tags" not in _codex("Remember").split("Otherwise ask before writing")[0]


TOOL_LOADS = {
    REMEMBER: ("remember", "remember_batch", "recall", "update_memory"),
    RECALL: ("recall", "list", "reference"),
    SESSION_SUMMARY: (
        "remember_batch",
        "remember",
        "update_memory",
        "create_edge",
        "forget",
        "reference",
    ),
}


@pytest.mark.parametrize("relative", [REMEMBER, RECALL, SESSION_SUMMARY])
def test_claude_skills_load_the_tools_in_one_toolsearch_call(relative):
    """#2690: a client that defers MCP tools loads them in one round trip."""
    text = _flat(relative)
    assert text.count("select:mcp__kagura-memory__") == 1
    head = text.index("## Steps")
    load = text[head : text.index("### 1.", head)]
    query = load[load.index("select:") : load.index('")', load.index("select:"))]
    names = {n.removeprefix("mcp__kagura-memory__") for n in query[len("select:") :].split(",")}
    assert names == set(TOOL_LOADS[relative])
    assert "`mcp__kagura-memory__list_contexts`" in load
    # The no-default path may also call get_context_info before the first write.
    assert "`mcp__kagura-memory__get_context_info`" in load
    assert "Never call `ToolSearch` a second time" in load
    assert "has no `ToolSearch`: skip this paragraph" in load


def test_codex_skill_carries_no_toolsearch_text():
    assert "ToolSearch" not in _flat(CODEX)


def test_server_text_points_a_write_at_tags_normalize():
    """#2690: the tool text and guide topics no longer send a write to list_tags first."""
    from mcp_server.tools.guide import GUIDE_TOPICS

    tools = {tool["name"]: tool for tool in get_tool_definitions()}
    tags = tools["remember"]["inputSchema"]["properties"]["tags"]["description"]
    assert "tags_normalize" in tags and "list_tags" not in tags
    assert "remember" not in tools["list_tags"]["description"]
    for topic in ("remember.layers", "list_tags.usage"):
        text = GUIDE_TOPICS[topic]
        assert "tags_normalize=true" in text
        assert "BEFORE remember" not in text and "list_tags() first" not in text


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
    # The event's own days come first when the row has them.
    assert '"event_after": "2024-03-01", "event_before": "2024-04-01"' in text
    assert 'order_by="event_start", direction="asc")' in text
    assert "`event_start` / `event_end` with `event_weekday`" in text
    assert "Sort the rows by that date yourself" not in text
    assert "the later `observed_at`, else the later `created_at`, is current" in text
    assert "`include_superseded=true`" in text
    assert "the store is the authority; never drop a row" in text


@pytest.mark.parametrize("relative", [RECALL, CODEX])
def test_recall_searches_again_before_answering_not_recorded(relative):
    """The plain case next to the likely / would / might rule: a second search by
    the subject before "not recorded", and a general statement answers "how often"."""
    text = _recall_rules(relative)
    assert "**Not recorded**" in text
    assert "first recall again with the subject's name and the fact's key noun" in text
    assert "`list` with a tag naming the subject" in text
    assert 'beats single instances for "how often"' in text
    tools = {tool["name"]: tool for tool in get_tool_definitions()}
    assert "Not recorded: first recall again" in tools["recall"]["description"]


def test_recall_signals_point_at_the_time_section():
    text = _flat(RECALL)
    assert "### 2b. When, how many, which is current" in text
    assert "see step 2b" in text


@pytest.mark.parametrize("relative", [RECALL, CODEX])
@pytest.mark.parametrize(
    "rule",
    [
        "read every result, not only the top ones; recall once more per kind found, or "
        "`list` with a tag or type filter naming the set; answer with all of them.",
        '`filters={"event_after": ..., "event_before": ...}` (the event\'s own days, '
        "event_start/event_end); rows without one: `observed_after`/`observed_before` "
        "around it with a margin (`observed_at` = the date a fact was stated); nothing "
        "matches: recall again without the filter.",
        "recall again on concrete entities and category words (brand, sponsor, shop, friend), "
        '`search_mode="keyword"` for surfaced names; answer with the best-supported '
        "inference, name the memory, mark it as an inference; decline only at "
        "`confidence.level` `none`/`low`.",
        # Before answering "not recorded", search once more by the subject.
        "first recall again with the subject's name and the fact's key noun "
        '(`search_mode="keyword"`), or `list` with a tag naming the subject; a row that '
        'states it answers — name it. A general statement beats single instances for "how '
        'often".',
    ],
)
def test_answering_rules_match_the_tool_and_both_skills(relative, rule):
    assert rule in _recall_rules(relative)
    tools = {tool["name"]: tool for tool in get_tool_definitions()}
    assert rule in tools["recall"]["description"]
    assert rule in _section(_flat("docs/mcp-tools.md"), "### `recall`", "### `reference`")


@pytest.mark.parametrize("relative", [REMEMBER, CODEX])
def test_named_speakers_keep_subject_attribution_and_direction(relative):
    text = _remember_rules(relative)
    for rule in (
        "Each summary starts with the name of the person it is about",
        'never "the user", "they", "he" or "she" alone',
        "Suggestions, advice, gifts, invitations and questions record both people and the direction",
        "Attribute each fact to the speaker who said it",
        "a fact about the other person keeps that person as its subject and names who said it",
        "Check each summary's subject against the speaker label of its source line",
    ):
        assert rule in text


@pytest.mark.parametrize("relative", [REMEMBER, CODEX])
def test_named_speaker_example_keeps_both_directions_and_the_reported_subject(relative):
    text = _remember_rules(relative)
    for line in (
        'Neri: "I started pottery. Tovan, I suggest you try swimming."',
        'Tovan: "I gave you a sketchbook, Neri. You won the local poetry prize."',
        "Neri started pottery.",
        "Neri suggested Tovan try swimming.",
        "Tovan gave Neri a sketchbook.",
        "Neri won the local poetry prize, according to Tovan.",
    ):
        assert line in text


def test_the_guidance_uses_real_tool_parameters():
    tools = {t["name"]: t for t in get_tool_definitions()}
    listing = tools["list"]["inputSchema"]["properties"]
    assert "created_at" in listing["order_by"]["enum"]
    assert "observed_at" in listing["order_by"]["enum"]  # #2330
    assert "event_start" in listing["order_by"]["enum"]
    assert "observed_after/before" in listing["filters"]["description"]
    assert "event_after/before" in listing["filters"]["description"]
    recall_filters = tools["recall"]["inputSchema"]["properties"]["filters"]["description"]
    assert "observed_after/before" in recall_filters
    assert "event_after/before" in recall_filters
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


RELATIVE_DATE_RULES = (
    "Relative dates.",
    "pass the phrase as stated in `details.event_phrase`",
    "do not compute the dates yourself",
    '"last weekend" is the Saturday–Sunday just before the material date',
    "on a Saturday or Sunday, the weekend before the current one",
    "returns `event_start`, `event_end` and `event_weekday`",
    'written "last weekend (2024-03-09–10)"',
    '`event_unresolved: "unknown_phrase"`',
    '`event_unresolved: "no_observed_at"`',
    "`observed_at` stays the material's date",
    "With no material date, leave the phrase as stated",
    "the server never counts from the time you write",
)


@pytest.mark.parametrize("relative", [REMEMBER, CODEX])
@pytest.mark.parametrize("rule", RELATIVE_DATE_RULES)
def test_remember_passes_relative_dates_as_event_phrase(relative, rule):
    """The server resolves ``details.event_phrase``; the writer no longer computes dates."""
    section = _section(
        _remember_rules(relative), "Relative dates.", "Store what the material states"
    )
    assert rule in section


@pytest.mark.parametrize("relative", [REMEMBER, CODEX])
def test_remember_no_longer_asks_the_writer_to_resolve_dates(relative):
    text = _remember_rules(relative)
    assert "Resolve relative dates." not in text
    assert "recompute the dates" not in text


def test_the_worked_example_matches_the_resolver():
    from datetime import date

    from utils.event_dates import resolve_event_phrase

    assert date(2024, 3, 11).weekday() == 0  # a Monday
    assert resolve_event_phrase("last weekend", date(2024, 3, 11)) == (
        date(2024, 3, 9),
        date(2024, 3, 10),
    )


@pytest.mark.parametrize("relative", [SESSION_SUMMARY, CODEX])
def test_session_summary_passes_relative_dates_as_event_phrase(relative):
    """session-summary writes dated memories too: the same rule, one line."""
    text = _codex("Session Summary") if relative == CODEX else _write_rules(relative)
    assert "`details.event_phrase` as stated" in text
    assert "do not compute it" in text
    assert "returns `event_start` / `event_end`" in text
    assert "resolve relative dates" not in text


@pytest.mark.parametrize("name", ["remember", "remember_batch", "update_memory"])
def test_write_tool_descriptions_carry_the_relative_date_rule(name):
    """Writers call the tools without the skills: the description holds the rule."""
    tools = {tool["name"]: tool for tool in get_tool_definitions()}
    description = tools[name]["description"]
    assert "details.event_phrase" in description
    assert "do not compute dates" in description


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
