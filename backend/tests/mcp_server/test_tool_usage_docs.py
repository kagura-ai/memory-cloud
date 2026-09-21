"""Call examples that moved out of the tool descriptions must stay callable (#1602).

The trim relocated the ``supersede_candidate`` accept / reject workflow from the
``recall`` / ``reference`` / ``update_memory`` descriptions to the usage notes
and the skills. An agent reading those copies a call verbatim, so an example
that leaves out a required argument turns into an invalid-params error. The
registry is the source of truth for what is required.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from mcp_server.tools import get_tool_definitions

# backend/tests/mcp_server/<this file> -> mcp_server -> tests -> backend -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[3]

# Every agent-facing file that spells out the accept / reject calls.
SUPERSEDE_WORKFLOW_DOCS = [
    "docs/mcp-tools.md",
    "claude-skills/guide.md",
    "claude-skills/recall.md",
    "plugins/kagura-memory/skills/kagura-memory/SKILL.md",
]

# A backticked `create_edge(...)` / `update_memory(...)` call.
_CALL = re.compile(r"`(create_edge|update_memory)\(([^`]*)\)`")


def _required(tool_name: str) -> list[str]:
    tool = next(tool for tool in get_tool_definitions() if tool["name"] == tool_name)
    return tool["inputSchema"]["required"]


def _supersede_calls(text: str) -> list[tuple[str, str]]:
    return [(name, args) for name, args in _CALL.findall(text) if "supersede" in args]


@pytest.mark.parametrize("relative_path", SUPERSEDE_WORKFLOW_DOCS)
def test_supersede_call_examples_name_every_required_parameter(relative_path):
    calls = _supersede_calls((_REPO_ROOT / relative_path).read_text(encoding="utf-8"))
    assert calls, f"{relative_path} no longer shows the accept / reject calls"
    incomplete = [
        f"{name}({args}) is missing {param}"
        for name, args in calls
        for param in _required(name)
        if f"{param}=" not in args
    ]
    assert incomplete == [], f"{relative_path}: {incomplete}"
