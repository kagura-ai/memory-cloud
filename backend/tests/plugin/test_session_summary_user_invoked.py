"""The session summary is user-directed (#1721, Directory policy 1.F).

Memories are client-authored and saved when the user asks. The Claude Code
``/kagura-memory:session-summary`` command runs when the user starts it, directly
or through a workflow the user started (``/gh-issue-driven:ship`` step 14 invokes
it through the Skill tool, so it must not set ``disable-model-invocation``), and
it and the Codex skill's "Session Summary" section save what the user chooses to
keep.
"""

from __future__ import annotations

from tests.plugin.conftest import REPO_ROOT

SESSION_SUMMARY = REPO_ROOT / "claude-skills" / "session-summary.md"
CODEX_SKILL = REPO_ROOT / "plugins" / "kagura-memory" / "skills" / "kagura-memory" / "SKILL.md"


def _frontmatter(text: str) -> dict[str, str]:
    assert text.startswith("---\n")
    block = text.split("---\n", 2)[1]
    fields: dict[str, str] = {}
    for line in block.splitlines():
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


def _section(text: str, heading: str) -> str:
    return text.split(heading, 1)[1].split("\n## ", 1)[0]


def test_session_summary_stays_invocable_by_user_started_workflows():
    fields = _frontmatter(SESSION_SUMMARY.read_text(encoding="utf-8"))
    assert "disable-model-invocation" not in fields, (
        "/gh-issue-driven:ship invokes this command through the Skill tool"
    )
    assert "user chooses to keep" in fields["description"]


def test_session_summary_saves_what_the_user_keeps():
    text = SESSION_SUMMARY.read_text(encoding="utf-8")
    when = _section(text, "## When to use")
    assert "When the user asks for it" in when
    assert "a workflow the user started" in when
    assert "save only the ones the user chooses to keep" in text


def test_session_summary_keeps_touched_memories_current_on_the_users_pick():
    """#1800: a light step after the save, for memories this session touched."""
    text = SESSION_SUMMARY.read_text(encoding="utf-8")
    step = text.split("### 4c. Keep touched memories current", 1)[1].split("\n### ", 1)[0]
    assert text.index("### 4. Save each item") < text.index("### 4c. Keep touched")
    assert text.index("### 4c. Keep touched") < text.index("### 5. Guidelines")
    assert "prints nothing when nothing applies" in step
    assert "applied only after the user picks" in step
    assert "No extra recall, no review of the whole context" in step
    # Save once: the replacement is declared on the step-4 save; 4c only links what
    # was saved without it, and never saves the item again.
    save = text.split("### 4. Save each item", 1)[1].split("\n### ", 1)[0]
    assert "Pass `supersedes=<old_memory_id>` on this `remember` call only when" in save
    assert "supersedes=<old_memory_id>" not in step
    assert "remember(..., " not in step
    assert "create_edge(source_id=<the memory just saved>, target_id=<the older memory>" in step
    assert "Never save the item a second time" in step
    assert "If step 4 did not already save the outcome" in step
    # A candidate is accepted or dismissed on the memory that carries it.
    assert "not the one just saved" in step
    assert step.count("<memory_id of the result that carries the candidate>") == 2
    # #2308: the candidate is accepted on update_memory, which core lists.
    assert "accept_supersede_candidate=true" in step
    # recall_upcoming returns no importance.
    assert "`reference(memory_id=..., context_id=..., fields=[])`" in step
    assert "`supersede_candidate`" in step
    assert "`forget(memory_id=..., context_id=...)`" in step
    assert "forget(query" not in step
    assert "/kagura-memory:maintain" in step
    # A "save everything" answer is not a pick for these changes.
    assert '"Save everything" in step 4 covers saving only' in step
    assert "with no answer from the user, skip the step entirely" in step
    assert "data, not instructions" in step
    assert "never the `query` mode" in step
    assert "show the summary and full id again" in step
    assert "above 0.8" in step
    assert "Copy ids verbatim from tool results" in step
    assert "`?profile=full`" in step
    # forget removes the memory's edges, so the outcome note is saved without one.
    assert "supersedes=<time" not in step
    assert len(step.encode("utf-8")) <= 2200, len(step.encode("utf-8"))


def test_codex_session_summary_keeps_touched_memories_current_on_the_users_pick():
    section = _section(CODEX_SKILL.read_text(encoding="utf-8"), "## Session Summary")
    assert "print nothing when nothing applies" in section
    assert "applied only after the user picks" in section
    assert "no extra recall" in section
    step_3, step_5 = section.split("\n5. Keep touched memories current", 1)
    assert "pass `supersedes=<old_memory_id>` on that `remember` call only when" in step_3
    assert "supersedes=<old_memory_id>" not in step_5
    assert "Never save the item a second time" in step_5
    assert "not the one just saved" in step_5
    assert step_5.count("<memory_id of the result that carries the candidate>") == 2
    assert "`reference(memory_id=..., context_id=..., fields=[])`" in step_5
    assert "`forget(memory_id=..., context_id=...)`" in section
    assert "Copy ids verbatim from tool results" in section
    assert '"Save everything" covers saving only' in section
    assert "with no answer the step is skipped" in section
    assert "data, not instructions" in section
    assert "never the `query` mode" in section
    assert "above 0.8" in section
    assert "supersedes=<time" not in section


def test_codex_session_summary_is_user_directed():
    section = _section(CODEX_SKILL.read_text(encoding="utf-8"), "## Session Summary")
    assert "Only when the user asks for a session summary" in section
    assert "never on your own" in section
    assert "save what the user chooses to keep" in section


REMEMBER = REPO_ROOT / "claude-skills" / "remember.md"


def test_session_summary_takes_a_save_mode_defaulting_to_auto():
    """#1930: the mode sets how much the model writes while the user waits."""
    text = SESSION_SUMMARY.read_text(encoding="utf-8")
    assert "Arguments: $ARGUMENTS — `[light|standard|deep|auto]`" in text
    assert "without an argument, too) means `auto`" in text
    modes = _section(text, "## Save modes")
    for mode in ("`light`", "`standard`", "`deep`"):
        assert f"\n| {mode} |" in modes
    # auto's criteria are written down so the pick does not drift, and announced.
    assert "`auto` picks one of the three" in modes
    assert "→ `light`" in modes and "→ `standard`" in modes and "→ `deep`" in modes
    assert "auto → standard (2 decisions, 1 trap)" in modes
    # light skips the extra writes but keeps the user's own picks.
    assert "`light` skips 4a and 4b." in text
    light_row = next(line for line in modes.splitlines() if line.startswith("| `light` |"))
    assert "| skipped | only the completed follow-up offer |" in light_row
    assert "A `supersedes` the user picked still applies" in light_row
    step_4c = text.split("### 4c. Keep touched memories current", 1)[1]
    assert step_4c.lstrip().startswith("`light`: the follow-up bullet only.")
    # deep links and supersedes only from ids already seen: no recall per item.
    deep_row = next(line for line in modes.splitlines() if line.startswith("| `deep` |"))
    assert "only from ids already in this session's results, no extra recall" in deep_row
    save = text.split("### 4. Save each item", 1)[1].split("\n### ", 1)[0]
    assert "- **linked_memory_ids** (optional, mainly `deep`)" in save
    assert "never fetched by an extra recall" in save


def test_codex_session_summary_mirrors_the_modes():
    """#1930: the Codex section carries the same caps, skips and id rule."""
    claude = _section(SESSION_SUMMARY.read_text(encoding="utf-8"), "## Save modes")
    codex = _section(CODEX_SKILL.read_text(encoding="utf-8"), "## Session Summary")
    for cap in ("up to 3", "up to 7", "about 15"):
        assert cap in claude and cap in codex, cap
    assert "no `context_summary`" in claude and "no `context_summary`" in codex
    assert "only from ids already in this session's results, no extra recall" in codex
    assert "a `supersedes` the user picked still applies" in codex
    step_5 = codex.split("\n5. Keep touched memories current", 1)[1]
    assert step_5.startswith(" (`light`: only the completed follow-up offer below)")


def test_save_skills_say_each_thing_once():
    """#1930: no restated summary, no spelling-variant tags, issues as tags only."""
    summary = SESSION_SUMMARY.read_text(encoding="utf-8")
    remember = REMEMBER.read_text(encoding="utf-8")
    codex = CODEX_SKILL.read_text(encoding="utf-8")
    for text in (summary, remember, codex):
        assert "writing variations for Japanese" not in text
        assert "Include `Related issues: #N` in `content`" not in text
        assert "a `Related issues:` line in content where relevant" not in text
    assert "Include a `Related issues: #N, #M` line at the end of **content**" not in summary
    assert "(max 2000 chars)" not in remember
    for text in (summary, remember):
        assert "never restate the summary" in text.lower()
        assert "about 6" in text.lower()
    assert "One sentence — when to recall it" in summary
    assert "one sentence on when to recall it" in remember
    session = _section(codex, "## Session Summary")
    assert "`light`" in session and "`deep`" in session and "`auto`" in session


def test_save_skills_always_send_content():
    """#1936: remember and every remember_batch item require content (min_length=1)."""
    texts = {
        "session-summary": SESSION_SUMMARY.read_text(encoding="utf-8"),
        "remember": REMEMBER.read_text(encoding="utf-8"),
        "codex": CODEX_SKILL.read_text(encoding="utf-8"),
    }
    for name, text in texts.items():
        lowered = text.lower()
        assert "leave it out when the summary" not in lowered, name
        assert "is left out when the summary" not in lowered, name
        assert "`content` only for evidence" not in text, name
        assert "short line naming the evidence or source" in text, name
    assert "- **content**: Required." in texts["session-summary"]
    assert "`content` (required)" in texts["remember"] and "`content` (required)" in texts["codex"]


def _modes(text: str) -> dict[str, str]:
    table = text.split("## Save modes", 1)[1].split("`auto` picks", 1)[0]
    rows = (line for line in table.splitlines() if line.startswith("| `"))
    return {row.split("`")[1]: row for row in rows}


def test_session_summary_keeps_every_personal_fact_outside_the_mode_count():
    """#2466: the count limits development knowledge; development counts stay."""
    text = SESSION_SUMMARY.read_text(encoding="utf-8")
    step = text.split("### 2. Identify what to remember", 1)[1].split("\n### ", 1)[0]
    for memory_type in ("fact", "preference", "event"):
        assert f"| `{memory_type}` | " in step
    assert "**A conversation about the user's own life** (not a development session)" in step
    assert "The mode's candidate count limits development knowledge only" in step
    assert "Keep development knowledge to the mode's candidate count" in step
    modes = _modes(text)
    assert "up to 3" in modes["light"]
    assert "up to 7" in modes["standard"]
    assert "about 15" in modes["deep"]


def test_session_summary_stamps_observed_at_in_every_mode():
    """#2466: ``auto`` picks ``light`` for a short conversation, so it keeps the date."""
    text = SESSION_SUMMARY.read_text(encoding="utf-8")
    save = text.split("### 4. Save each item", 1)[1].split("\n### ", 1)[0]
    assert "- **details.observed_at**: the session's date as `YYYY-MM-DD`" in save
    assert save.index("- **type**") < save.index("- **details.observed_at**")
    assert save.index("- **details.observed_at**") < save.index("- **importance**")
    assert "and so does `details.observed_at`" in _modes(text)["light"]
    # A time item without its trigger is a rejected write, in any mode.
    assert "`details.trigger` on a `time` item" in _modes(text)["light"]
    # A live session is dated today, so the rule cannot forbid today's date.
    assert "never today's date" not in text
    # ``light`` content is one evidence line, so a resolved date goes in the summary.
    assert "into the content (`light`: the summary)" in save


def test_codex_session_summary_carries_the_personal_fact_rules():
    """#2466: the Codex skill's "Session Summary" section is the SYNC copy."""
    text = CODEX_SKILL.read_text(encoding="utf-8")
    section = _section(text, "## Session Summary")
    assert "The mode's item count limits development knowledge only" in section
    assert "`details.observed_at`" in section
    assert "and so does `details.observed_at`" in section
    assert "`details.trigger` on a `time` item" in section
    assert "never today's date" not in section
    assert "into the content (`light`: the summary)" in section


MEMO_TAGS = (
    "`status:open`",
    "`source:review`",
    "`issue:#N`",
    "`pr:#N`",
    "`area:<area>`",
    "`file:<repo path>` for every file concerned",
)


def _codex_backlog() -> str:
    text = CODEX_SKILL.read_text(encoding="utf-8")
    body = text.split("\n## Lightweight backlog\n", 1)[1]
    return body.split("\n<!-- SYNC:", 1)[0].split("\n## ", 1)[0]


def test_session_summary_offers_the_two_memo_buckets():
    """#2586: the follow-up policy's memos, with every field the policy names."""
    text = SESSION_SUMMARY.read_text(encoding="utf-8")
    step = text.split("### 2. Identify what to remember", 1)[1].split("\n### ", 1)[0]
    memos = step.split("**Memos", 1)[1]
    assert "| `tech-debt` | 0.45 | `tech-debt (<area>): ` |" in memos
    assert "| `needs-decision` | 0.5 | `needs-decision (<area>): ` |" in memos
    assert "a question only the owner can answer" in memos
    assert "`Problem:`, `Proposal:` and `Source:` lines" in memos
    assert "then `Related issues: #…`" in memos
    assert "- **context_summary**: when to pick it up" in memos
    for tag in MEMO_TAGS:
        assert tag in memos, tag
    # The file tags are the lookup key, so the general tag cap gives way; and
    # light mode, which drops context_summary, keeps a memo whole.
    assert "the about-6 cap of step 4 does not apply" in memos
    assert "a memo keeps these fields in every mode" in memos
    assert "/kagura-memory:maintain" in memos


def test_codex_session_summary_offers_the_memo_buckets():
    section = _section(CODEX_SKILL.read_text(encoding="utf-8"), "## Session Summary")
    assert 'can be offered as a memo ("Lightweight backlog" below)' in section
    assert "a memo's is the one exception" in section
    backlog = " ".join(_codex_backlog().split())
    assert '`type="tech-debt"` (importance 0.45)' in backlog
    assert '`type="needs-decision"` (0.5)' in backlog
    assert "`tech-debt (<area>): `" in backlog and "`needs-decision (<area>): `" in backlog
    assert "`Problem:`, `Proposal:` and `Source:` lines, then `Related issues: #…`" in backlog
    assert "`context_summary`: when to pick it up" in backlog
    for tag in MEMO_TAGS:
        assert tag in backlog, tag
