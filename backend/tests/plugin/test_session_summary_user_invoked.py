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
    assert "target_id=<supersede_candidate.memory_id>" in step
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
    assert "describe_tools" in step
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
