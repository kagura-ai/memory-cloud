"""Which date answers "when did it happen" inside one recall row (#2755).

A recall row can show three dates for one memory: ``observed_at`` (when the
fact was stated, with ``observed_weekday``), ``event_start`` / ``event_end``
(when the event happened, with ``event_weekday``) and a date the writer put in
the summary text. The tool description names the event fields in its Returns
line and says which date wins inside a row; the guide, docs/mcp-tools.md and
both skill copies say the same.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from mcp_server.tools import get_tool_definitions
from mcp_server.tools.guide import GUIDE_TOPICS
from models.schemas import MemoryResponse
from utils.recall_projection import recall_result_item

# backend/tests/mcp_server/<this file> -> repo root
REPO_ROOT = Path(__file__).resolve().parents[3]

# The within-row rule, word for word, wherever the answering rules are kept.
WITHIN_ROW_RULE = (
    "In one row: `event_start`..`event_end` = when it happened, `observed_at` = "
    "when it was said; a date in the summary loses to the event fields."
)
EVENT_KEYS = ("event_start", "event_end", "event_weekday")
SUMMARY_WITH_ANOTHER_DATE = "Went hiking on 2024-03-02 (the summary's own date)"


def _row(observed_at: str | None, **event: str) -> MemoryResponse:
    return MemoryResponse(
        memory_id=uuid4(),
        summary=SUMMARY_WITH_ANOTHER_DATE,
        context_summary=None,
        type="event",
        importance=0.5,
        scope="persistent",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=None,
        observed_at=observed_at,
        client="test",
        tags=[],
        context=None,
        score=0.9,
        **event,
    )


def _recall_description() -> str:
    return next(t for t in get_tool_definitions() if t["name"] == "recall")["description"]


def _flat(relative: str) -> str:
    return " ".join((REPO_ROOT / relative).read_text(encoding="utf-8").split())


class TestRowsCarryEveryDate:
    @pytest.mark.parametrize("verbosity", ["compact", "full"])
    def test_a_row_with_all_three_dates_carries_each_one(self, verbosity):
        row = _row("2024-03-11", event_start="2024-03-09", event_end="2024-03-10")
        item = recall_result_item(row, verbosity)
        assert item["observed_at"] == "2024-03-11"
        assert item["observed_weekday"] == "Mon"
        assert (item["event_start"], item["event_end"], item["event_weekday"]) == (
            "2024-03-09",
            "2024-03-10",
            "Sat",
        )
        # The summary's own date is shown as written; the server never rewrites it.
        assert item["summary"] == SUMMARY_WITH_ANOTHER_DATE

    @pytest.mark.parametrize("verbosity", ["compact", "full"])
    def test_a_row_with_observed_at_only_has_no_event_key(self, verbosity):
        item = recall_result_item(_row("2024-03-11"), verbosity)
        assert (item["observed_at"], item["observed_weekday"]) == ("2024-03-11", "Mon")
        assert not set(EVENT_KEYS) & set(item)


class TestTheRuleIsWrittenDown:
    def test_the_returns_line_names_the_event_fields(self):
        returns = _recall_description().split("\nReturns:", 1)[1]
        assert "observed_at?, observed_weekday?, event_start?, event_end?, event_weekday?" in (
            returns
        )

    def test_the_description_says_which_date_wins_inside_a_row(self):
        line = next(
            line
            for line in _recall_description().splitlines()
            if line.startswith("When did / in a period:")
        )
        assert WITHIN_ROW_RULE in line

    def test_the_guide_says_the_same(self):
        assert WITHIN_ROW_RULE in " ".join(GUIDE_TOPICS["recall.reading-results"].split())

    @pytest.mark.parametrize(
        "relative",
        [
            "docs/mcp-tools.md",
            "claude-skills/recall.md",
            "plugins/kagura-memory/skills/kagura-memory/SKILL.md",
        ],
    )
    def test_the_docs_and_both_skills_say_the_same(self, relative):
        text = _flat(relative)
        rule = text[text.index("**When did / in a period**") :]
        assert WITHIN_ROW_RULE in rule[: rule.index(" - **")]

    @pytest.mark.parametrize(
        "relative",
        ["claude-skills/recall.md", "plugins/kagura-memory/skills/kagura-memory/SKILL.md"],
    )
    def test_the_skills_keep_the_order_and_add_that_the_summary_loses(self, relative):
        text = _flat(relative)
        rule = text[text.index("**When did / in what order**") :]
        rule = rule[: rule.index(" - **")]
        assert "use the event's own days when the row has them" in rule
        assert "else `created_at`" in rule
        assert "a date written in the summary loses to the row's event fields" in rule
