"""The recall ``hint`` key on the MCP envelope and in the docs (#2754).

``_recall_envelope`` is hand-built (#1515), so ``RecallResponse.hint`` reaches
MCP clients only because it is copied: right after ``related_tags``, absent
when there is none. With the setting off the reply is byte-identical to the
reply before the key existed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock
from uuid import UUID

from mcp_server.tools import get_tool_definitions
from mcp_server.tools import memory as mcp_memory
from mcp_server.tools._helpers import _dumps
from mcp_server.tools.guide import GUIDE_TOPICS
from models.schemas import MemoryResponse, RecallConfidence, RecallResponse, RelatedTagItem

REPO_ROOT = Path(__file__).resolve().parents[3]
HINT = (
    'At least 3 memories matching this search carry tag "trip", 1 shown; '
    'list(filters={"tags": ["trip"]}) returns every memory with it.'
)


def _context():
    context = MagicMock()
    context.id = UUID("00000000-0000-4000-8000-000000000001")
    context.name = "dev"
    context.display_name = "Development"
    context.is_private = False
    context.is_locked = False
    return context


def _result(**overrides) -> RecallResponse:
    row = MemoryResponse(
        memory_id=UUID("00000000-0000-4000-8000-0000000000aa"),
        summary="went hiking",
        context_summary=None,
        type="event",
        importance=0.5,
        scope="persistent",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=None,
        client="test",
        tags=["trip"],
        context=None,
        score=0.9,
    )
    fields = {
        "results": [row],
        "related_tags": [RelatedTagItem(tag="trip", count=1)],
        "confidence": RecallConfidence(
            level="high", top_score=0.9, result_count=1, rationale="test"
        ),
    }
    fields.update(overrides)
    return RecallResponse(**fields)


class TestEnvelope:
    def test_the_hint_follows_related_tags(self):
        envelope = mcp_memory._recall_envelope(_result(hint=HINT), _context(), "compact")
        assert envelope["hint"] == HINT
        keys = list(envelope)
        assert keys.index("hint") == keys.index("related_tags") + 1

    def test_without_related_tags_it_still_follows_the_context_fields(self):
        envelope = mcp_memory._recall_envelope(
            _result(hint=HINT, related_tags=[]), _context(), "compact"
        )
        keys = list(envelope)
        assert "related_tags" not in keys
        assert keys.index("hint") == keys.index("context_is_locked") + 1

    def test_absent_never_null(self):
        envelope = mcp_memory._recall_envelope(_result(), _context(), "compact")
        assert "hint" not in envelope

    def test_the_reply_without_a_hint_is_byte_identical(self):
        """The key is the only difference: drop it and the bytes match."""
        with_hint = mcp_memory._recall_envelope(_result(hint=HINT), _context(), "full")
        without = mcp_memory._recall_envelope(_result(), _context(), "full")
        with_hint.pop("hint")
        assert _dumps(with_hint) == _dumps(without)

    def test_a_loose_stand_in_result_invents_no_hint(self):
        """A bare mock result (attribute access returns a mock) adds nothing."""
        loose = MagicMock()
        loose.results = []
        loose.confidence = None
        loose.related_tags = []
        loose.explore_hints = None
        loose.tag_suggestions = None
        loose.degraded = None
        loose.degraded_reason = None
        envelope = mcp_memory._recall_envelope(loose, _context(), "compact")
        assert "hint" not in envelope

    def test_rest_never_serialises_it(self):
        """MCP recall tool only: a REST or share-key body never carries it, even set."""
        assert "hint" not in _result(hint=HINT).model_dump(mode="json")
        assert "hint" not in _result(hint=HINT).model_dump(mode="json", exclude_none=True)


class TestDocs:
    def test_the_returns_line_names_it(self):
        description = next(t for t in get_tool_definitions() if t["name"] == "recall")[
            "description"
        ]
        assert "related_tags?: [{tag, count}], hint?," in description

    def test_the_guide_says_what_it_is_and_that_a_deployment_turns_it_on(self):
        text = " ".join(GUIDE_TOPICS["recall.returns"].split())
        assert "hint?" in text
        assert "RECALL_LIST_HINT_ENABLED" in text
        assert "list(filters=" in text

    def test_the_tool_docs_say_the_same(self):
        text = " ".join((REPO_ROOT / "docs/mcp-tools.md").read_text(encoding="utf-8").split())
        assert "`hint`" in text and "RECALL_LIST_HINT_ENABLED" in text

    def test_both_skills_follow_it(self):
        sentence = "when a recall reply carries `hint`, follow it for every-item questions"
        for relative in (
            "claude-skills/recall.md",
            "plugins/kagura-memory/skills/kagura-memory/SKILL.md",
        ):
            text = " ".join((REPO_ROOT / relative).read_text(encoding="utf-8").split())
            assert sentence in text.lower(), relative

    def test_the_deployment_docs_and_env_example_list_the_setting(self):
        for relative in ("docs/deployment.md", ".env.example"):
            text = (REPO_ROOT / relative).read_text(encoding="utf-8")
            assert "RECALL_LIST_HINT_ENABLED" in text, relative


class TestTheBound:
    """A reply whose rows the ``max_chars`` bound cut carries no hint (its "N shown"
    counted the uncut page); content fitted away without a row cut keeps it."""

    @staticmethod
    def _rows(n: int, content: str | None) -> list[MemoryResponse]:
        return [
            MemoryResponse(
                memory_id=UUID(f"00000000-0000-4000-8000-{i:012d}"),
                summary=f"went hiking, trip number {i}",
                context_summary=None,
                type="event",
                importance=0.5,
                scope="persistent",
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
                updated_at=None,
                client="test",
                tags=["trip"],
                context=None,
                score=0.9,
                content=content,
            )
            for i in range(n)
        ]

    def _reply(self, rows: list[MemoryResponse], max_chars: int) -> dict:
        return mcp_memory._recall_reply(
            _result(results=rows, hint=HINT), _context(), "compact", max_chars
        )

    def test_rows_cut_drops_the_hint(self):
        rows = self._rows(40, None)
        uncut = self._reply(rows, 100_000)
        assert "truncated" not in uncut and uncut["hint"] == HINT
        reply = self._reply(rows, len(_dumps(uncut)) // 2)
        assert reply["truncated"] is True
        assert "hint" not in reply

    def test_content_fitted_without_a_row_cut_keeps_the_hint(self):
        rows = self._rows(3, "x" * 1400)
        bare = self._reply(self._rows(3, None), 100_000)
        reply = self._reply(rows, len(_dumps(bare)) + 1_000)
        assert "full_applied" in reply
        assert "truncated" not in reply
        assert reply["hint"] == HINT
