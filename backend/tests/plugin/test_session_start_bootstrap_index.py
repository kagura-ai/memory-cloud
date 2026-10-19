"""Session start reads the bootstrap index (#2126).

Both skills of the SYNC pair ask for ``format="index"`` only when the server's
``bootstrap`` lists ``format``, retry once with the plain call on an older
server's refusal, and never page ``changes_since`` on their own after an index
reply (the saving would be spent on the next call).
"""

from __future__ import annotations

import pytest

from mcp_server.tools import get_tool_definitions
from tests.plugin.conftest import REPO_ROOT

SKILLS = [
    "claude-skills/session-start.md",
    "plugins/kagura-memory/skills/kagura-memory/SKILL.md",
]
INDEX_CALL = 'bootstrap(context_id=..., since="7d", format="index", max_chars=3000)'
PLAIN_CALL = 'bootstrap(context_id=..., since="7d")'


def _flat(relative: str) -> str:
    return " ".join((REPO_ROOT / relative).read_text(encoding="utf-8").split())


@pytest.mark.parametrize("relative", SKILLS)
def test_skill_asks_for_the_index_when_bootstrap_lists_format(relative):
    text = _flat(relative)
    assert INDEX_CALL in text
    assert PLAIN_CALL in text  # older servers and the one retry
    assert "parameters include `format`" in text
    assert "never a third time" in text or "never try a third time" in text


@pytest.mark.parametrize("relative", SKILLS)
def test_skill_reads_index_rows_without_paging_or_bulk_reference(relative):
    text = _flat(relative)
    assert 'format: "index"' in text
    assert "summary_cut: true" in text
    assert "at most 3" in text
    assert "changes_since(next_cursor=...)" in text
    assert "never page automatically" in text or "do not page on your own" in text


def test_codex_sync_comment_names_the_index_rules():
    text = _flat(SKILLS[1])
    sync = text[text.index('<!-- SYNC: keep "Start Session"') :]
    sync = sync[: sync.index("-->")]
    assert 'format="index"' in sync and "no automatic changes paging" in sync


def test_the_index_call_uses_real_bootstrap_parameters():
    tool = next(t for t in get_tool_definitions() if t["name"] == "bootstrap")
    properties = tool["inputSchema"]["properties"]
    for name in ("context_id", "since", "format", "max_chars"):
        assert name in properties
    assert "index" in properties["format"]["enum"]
