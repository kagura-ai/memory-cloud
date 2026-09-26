"""The session summary is user-directed (#1721, Directory policy 1.F).

Memories are client-authored and saved when the user asks. The Claude Code
``/kagura-memory:session-summary`` command must not be started by the model
(``disable-model-invocation: true``), and it and the Codex skill's "Session
Summary" section save what the user chooses to keep.
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


def test_session_summary_cannot_be_started_by_the_model():
    fields = _frontmatter(SESSION_SUMMARY.read_text(encoding="utf-8"))
    assert fields["disable-model-invocation"] == "true"
    assert "user chooses to keep" in fields["description"]


def test_session_summary_saves_what_the_user_keeps():
    text = SESSION_SUMMARY.read_text(encoding="utf-8")
    assert "Only when the user runs this command" in _section(text, "## When to use")
    assert "save only the ones the user chooses to keep" in text


def test_codex_session_summary_is_user_directed():
    section = _section(CODEX_SKILL.read_text(encoding="utf-8"), "## Session Summary")
    assert "Only when the user asks for a session summary" in section
    assert "never on your own" in section
    assert "save what the user chooses to keep" in section
