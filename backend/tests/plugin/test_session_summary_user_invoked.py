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


def test_codex_session_summary_is_user_directed():
    section = _section(CODEX_SKILL.read_text(encoding="utf-8"), "## Session Summary")
    assert "Only when the user asks for a session summary" in section
    assert "never on your own" in section
    assert "save what the user chooses to keep" in section
