"""The ``guide`` tool and the trimmed core descriptions (#1850).

The core descriptions keep three to five lines each; the manual they used to
carry lives in ``mcp_server.tools.guide``. These tests pin both halves: the
descriptions stay short and point at topics that exist, and the sections that
left the descriptions are still reachable through ``guide``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from mcp_server.tools import _RATE_LIMIT_EXEMPT_TOOLS, _TOOLS_WITHOUT_CONTEXT_ID
from mcp_server.tools._annotations import TOOL_ANNOTATIONS
from mcp_server.tools._definitions import get_tool_definitions
from mcp_server.tools._profiles import CORE_TOOLS
from mcp_server.tools.guide import (
    GUIDE_INDEX,
    GUIDE_TOPICS,
    MAX_TOPICS,
    SHARED_TOPICS,
    handle_guide,
    resolve_topics,
)

DOCS = Path(__file__).resolve().parents[3] / "docs" / "mcp-tools.md"
MAX_DESCRIPTION_LINES = 5
MAX_PARAM_DESCRIPTION_CHARS = 400


def _by_name() -> dict[str, dict]:
    return {tool["name"]: tool for tool in get_tool_definitions()}


# ------------------------------------------------------------------ the tool


def test_guide_is_a_core_read_tool_that_needs_no_context():
    tool = _by_name()["guide"]
    assert "guide" in CORE_TOOLS
    assert TOOL_ANNOTATIONS["guide"]["readOnlyHint"] is True
    assert "guide" in _TOOLS_WITHOUT_CONTEXT_ID
    assert "guide" in _RATE_LIMIT_EXEMPT_TOOLS
    assert "required" not in tool["inputSchema"]  # no topics → index
    assert tool["inputSchema"]["properties"]["topics"]["maxItems"] == MAX_TOPICS


def test_index_covers_every_topic_and_every_topic_exists():
    for tool, topics in GUIDE_INDEX.items():
        for topic in topics:
            assert topic in GUIDE_TOPICS, f"{tool} lists unknown topic {topic!r}"
    indexed = {t for topics in GUIDE_INDEX.values() for t in topics} | set(SHARED_TOPICS)
    assert set(GUIDE_TOPICS) == indexed, "a topic is not reachable from the index"
    (index,), unknown = resolve_topics(["index"])
    assert unknown == []
    for tool in GUIDE_INDEX:
        assert f"- {tool}: " in index["text"]


def test_a_tool_name_expands_to_its_sections_once():
    found, unknown = resolve_topics(["recall", "recall.filters", "ids"])
    assert unknown == []
    topics = [f["topic"] for f in found]
    assert topics == list(GUIDE_INDEX["recall"])  # recall.filters and ids already included
    assert all(f["text"] == GUIDE_TOPICS[f["topic"]] for f in found)


def test_unknown_topics_are_reported_not_raised():
    found, unknown = resolve_topics(["recall.nope", " security "])
    assert [f["topic"] for f in found] == ["security"]
    assert unknown == ["recall.nope"]


@pytest.mark.asyncio
async def test_handle_guide_returns_topics_and_hints_on_unknown():
    (block,) = await handle_guide({"topics": ["remember", "bogus"]}, "u", None)
    payload = json.loads(block.text)
    assert payload["status"] == "success"
    assert [t["topic"] for t in payload["topics"]] == list(GUIDE_INDEX["remember"])
    assert payload["unknown"] == ["bogus"]
    assert "index" in payload["hint"]


@pytest.mark.asyncio
async def test_handle_guide_defaults_to_the_index_and_validates_the_list():
    (block,) = await handle_guide({}, "u", None)
    assert json.loads(block.text)["topics"][0]["topic"] == "index"
    (err,) = await handle_guide({"topics": "recall"}, "u", None)
    assert json.loads(err.text)["error"] == "validation_error"
    (err,) = await handle_guide({"topics": ["x"] * (MAX_TOPICS + 1)}, "u", None)
    assert json.loads(err.text)["error"] == "validation_error"


# ---------------------------------------------------- the trimmed descriptions


@pytest.mark.parametrize("name", sorted(CORE_TOOLS))
def test_core_descriptions_are_short(name):
    tool = _by_name()[name]
    lines = [line for line in tool["description"].splitlines() if line.strip()]
    assert len(lines) <= MAX_DESCRIPTION_LINES, f"{name}: {len(lines)} lines"
    for param, spec in tool["inputSchema"].get("properties", {}).items():
        text = spec.get("description", "")
        assert "\n" not in text, f"{name}.{param} spans lines"
        assert len(text) <= MAX_PARAM_DESCRIPTION_CHARS, f"{name}.{param}: {len(text)} chars"


def test_every_manual_pointer_names_an_existing_topic():
    pointers = set()
    for tool in get_tool_definitions():
        texts = [
            tool["description"],
            *(p.get("description", "") for p in tool["inputSchema"].get("properties", {}).values()),
        ]
        for text in texts:
            pointers.update(re.findall(r"""guide\(\[['"]([^'"]+)['"]\]\)""", text))
    assert pointers, "no description points at the manual"
    for topic in pointers:
        assert topic == "index" or topic in GUIDE_INDEX or topic in GUIDE_TOPICS, topic


@pytest.mark.parametrize(
    "phrase",
    [
        # recall
        "hypothetical answer",
        "Never decide relevance from relative_margin",
        "'search impaired', not 'nothing stored'",
        "tags_normalize=true",
        "absent, never null",
        # remember / update_memory
        "Three layers",
        "never 'part 1/3'",
        "consolidation_archive_min_age_days is a floor",
        "upsert for sync workflows",
        "details is replaced wholesale",
        # reference / forget / explore
        "content_next_offset",
        "memory_id wins if both are given",
        "typical edge weights are 0.02-0.05",
        # contexts / tags / pinned / time
        "by_type keeps the 20 largest types",
        "count = contexts in the workspace",
        "Soft-deleted memories are not counted",
        "no search, no ranking",
        "omit month/day for fuzzy timing",
        # review follow-ups: text that left a description must stay reachable
        "BYOK Voyage/Cohere",
        "['鯖', 'サバ', 'さば']",
        "also when no live suggestion existed",
        "guardrails?",
    ],
)
def test_guidance_removed_from_the_descriptions_lives_in_the_guide(phrase):
    assert any(phrase in text for text in GUIDE_TOPICS.values()), phrase


@pytest.mark.skipif(not DOCS.exists(), reason="docs/ is not shipped in the image")
def test_every_usage_note_in_the_docs_has_a_guide_topic():
    notes = DOCS.read_text(encoding="utf-8").split("## Usage notes", 1)[1]
    tools = re.findall(r"^### `([a-z_]+)`", notes, re.M)
    assert tools, "no Usage notes headings found"
    # Only the core tools moved their manual into the guide; a Usage note for a
    # non-core tool (update_search_config) still lives in the docs alone.
    missing = [t for t in tools if t in CORE_TOOLS and t not in GUIDE_INDEX]
    assert missing == [], f"Usage notes without a guide topic: {missing}"


# ------------------------------------------------------- hints on errors


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler_name", "args", "topic"),
    [
        (
            "handle_update_memory",
            {"context_id": "00000000-0000-4000-8000-000000000001"},
            "update_memory",
        ),
        ("handle_recall", {"context_id": "00000000-0000-4000-8000-000000000001"}, "recall"),
    ],
)
async def test_validation_errors_name_their_guide_topic(handler_name, args, topic):
    """A caller that got the arguments wrong is told where the manual is (#1850).

    These paths fail before any database access, so the handlers run bare.
    """
    from mcp_server.tools import memory

    (block,) = await getattr(memory, handler_name)(args, "user-1", None)
    payload = json.loads(block.text)
    assert payload["status"] == "error"
    assert payload["error"] in {"validation_error", "missing_fields"}
    assert payload["hint"] == f'guide(["{topic}"])'
    assert topic in GUIDE_INDEX
