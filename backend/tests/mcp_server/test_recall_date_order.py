"""``recall(order="date")``: the same results, oldest first, each with its day (#3004).

The search is unchanged; only the presentation moves. Each row gets ``date``
(``YYYY-MM-DD``) and ``date_source``: the event's own day (``event_start``),
else the day the fact was stated (``observed_at``), else ``created_at``'s day,
all in UTC as the rows already render them. Ties keep relevance order. ``full``
and the ``max_chars`` trimming still decide by relevance rank, and a default
recall (no ``order``, or ``order="relevance"``) is byte-identical to before.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from mcp_server.tools import memory as mcp_memory
from mcp_server.tools._helpers import _dumps, set_mcp_tool_view_query
from mcp_server.tools.memory import handle_recall
from models.schemas import MemoryResponse, RecallConfidence, RecallRequest, RecallResponse
from utils.recall_order import date_ordered, row_date


def _memory(created: datetime, **overrides: Any) -> MemoryResponse:
    fields: dict[str, Any] = {
        "memory_id": uuid4(),
        "summary": "Falcon launch date",
        "context_summary": "Recall for the launch schedule",
        "type": "fact",
        "importance": 0.6,
        "scope": "persistent",
        "created_at": created,
        "updated_at": None,
        "client": "mcp",
        "tags": ["falcon"],
        "context": None,
        "score": 0.5,
    }
    fields.update(overrides)
    return MemoryResponse(**fields)


def _day(month: int, day: int, hour: int = 9) -> datetime:
    return datetime(2026, month, day, hour, 0, tzinfo=UTC)


def _context():
    context = MagicMock()
    context.id = UUID("00000000-0000-4000-8000-000000000001")
    context.name = "dev"
    context.display_name = "Development"
    context.is_private = False
    context.is_locked = False
    return context


# ============================================================================
# The day of one row
# ============================================================================


class TestRowDate:
    def test_event_start_wins(self):
        row = _memory(
            _day(9, 1), observed_at="2026-08-15", event_start="2026-07-04", event_end="2026-07-05"
        )
        assert row_date(row) == ("2026-07-04", "event")

    def test_observed_at_date_without_an_event(self):
        assert row_date(_memory(_day(9, 1), observed_at="2026-08-15")) == ("2026-08-15", "observed")

    def test_observed_at_datetime_gives_its_utc_day(self):
        row = _memory(_day(9, 1), observed_at="2026-08-15T23:30:00Z")
        assert row_date(row) == ("2026-08-15", "observed")

    def test_created_at_last(self):
        assert row_date(_memory(_day(9, 1, 23))) == ("2026-09-01", "created")

    def test_created_at_is_the_day_it_renders(self):
        # The day of the rendered UTC value: a naive stored time is UTC.
        assert row_date(_memory(datetime(2026, 9, 1, 23, 59))) == ("2026-09-01", "created")

    def test_a_legacy_observed_value_falls_through_to_created(self):
        row = _memory(_day(9, 1))
        row.observed_at = "last spring"
        assert row_date(row) == ("2026-09-01", "created")


# ============================================================================
# The order of the rows (the REST response model)
# ============================================================================


class TestDateOrdered:
    def test_oldest_first_by_each_source(self):
        created = _memory(_day(9, 1), summary="created")  # 2026-09-01
        observed = _memory(_day(10, 1), summary="observed", observed_at="2026-08-01")
        event = _memory(_day(11, 1), summary="event", event_start="2026-07-01")
        result = date_ordered(RecallResponse(results=[created, observed, event]))

        assert [r.summary for r in result.results] == ["event", "observed", "created"]
        assert [(r.date, r.date_source) for r in result.results] == [
            ("2026-07-01", "event"),
            ("2026-08-01", "observed"),
            ("2026-09-01", "created"),
        ]
        assert result.order == "date"

    def test_ties_keep_relevance_order(self):
        rows = [_memory(_day(9, 1, hour), summary=f"r{hour}") for hour in (20, 8, 14)]
        result = date_ordered(RecallResponse(results=rows))
        assert [r.summary for r in result.results] == ["r20", "r8", "r14"]

    def test_confidence_and_other_keys_are_kept(self):
        confidence = RecallConfidence(level="high", top_score=0.9, result_count=2, rationale="r")
        rows = [_memory(_day(9, 2)), _memory(_day(9, 1))]
        result = date_ordered(RecallResponse(results=rows, confidence=confidence))
        assert result.confidence == confidence
        assert result.results[0].memory_id == rows[1].memory_id
        # The input is not mutated.
        assert rows[0].date is None

    def test_a_default_response_serializes_without_the_new_keys(self):
        dumped = RecallResponse(results=[_memory(_day(9, 1))]).model_dump(mode="json")
        assert "order" not in dumped
        assert "date" not in dumped["results"][0]
        assert "date_source" not in dumped["results"][0]


# ============================================================================
# The MCP reply
# ============================================================================


def _reply(result: RecallResponse, order: str = "relevance", **kw: Any) -> dict:
    kw.setdefault("verbosity", "compact")
    kw.setdefault("max_chars", 20_000)
    return mcp_memory._recall_reply(result, _context(), order=order, **kw)


class TestReply:
    def test_rows_are_oldest_first_with_date_after_memory_id(self):
        newer = _memory(_day(9, 2), summary="newer")
        older = _memory(_day(9, 1), summary="older", observed_at="2026-06-01")
        reply = _reply(RecallResponse(results=[newer, older]), order="date")

        assert [item["summary"] for item in reply["results"]] == ["older", "newer"]
        first = reply["results"][0]
        assert list(first)[:3] == ["memory_id", "date", "date_source"]
        assert (first["date"], first["date_source"]) == ("2026-06-01", "observed")
        # Every key a row has today stays.
        assert first["observed_at"] == "2026-06-01"
        assert reply["order"] == "date"
        assert list(reply).index("order") == list(reply).index("count") + 1

    @pytest.mark.parametrize("verbosity", ["compact", "full"])
    def test_relevance_reply_is_unchanged(self, verbosity):
        rows = [_memory(_day(9, 2)), _memory(_day(9, 1), event_start="2026-01-01")]
        result = RecallResponse(results=rows)
        before = mcp_memory._recall_reply(result, _context(), verbosity, 20_000)
        assert _reply(result, verbosity=verbosity) == before
        text = _dumps(before)
        assert '"order"' not in text
        assert '"date"' not in text
        assert '"date_source"' not in text

    def test_full_content_stays_on_the_most_relevant_rows(self):
        # The service attached content to the top two by relevance; the date
        # order moves them, it does not move the content.
        top = _memory(_day(9, 3), summary="top", content="top content")
        second = _memory(_day(9, 2), summary="second", content="second content")
        third = _memory(_day(9, 1), summary="third")
        reply = _reply(RecallResponse(results=[top, second, third]), order="date")
        assert [i["summary"] for i in reply["results"]] == ["third", "second", "top"]
        assert [("content" in i) for i in reply["results"]] == [False, True, True]

    def test_content_leaves_the_least_relevant_row_first_under_max_chars(self):
        # Longer than the service ever attaches, so a 10,000-character budget
        # holds two of the three.
        body = "x" * 4_500
        rows = [
            _memory(_day(9, 1), summary="top-oldest", content=body),
            _memory(_day(9, 3), summary="second-newest", content=body),
            _memory(_day(9, 2), summary="third-middle", content=body),
        ]
        result = RecallResponse(results=rows)
        plain = mcp_memory._recall_reply(result, _context(), "compact", 100_000)
        budget = len(_dumps(plain)) - 2_000
        assert budget >= 10_000
        reply = _reply(result, order="date", max_chars=budget)
        carrying = {i["summary"] for i in reply["results"] if "content" in i}
        assert "third-middle" not in carrying
        assert "top-oldest" in carrying
        assert [i["summary"] for i in reply["results"]] == [
            "top-oldest",
            "third-middle",
            "second-newest",
        ]

    def test_rows_cut_for_the_budget_are_the_least_relevant(self):
        rows = [
            _memory(_day(9, 1 + i), summary=f"r{i} " + "y" * 900, context_summary=None)
            for i in range(30)
        ]
        # Relevance order is newest first: r29 is the top hit.
        rows.reverse()
        reply = _reply(RecallResponse(results=rows), order="date", max_chars=10_000)
        assert reply["truncated"] is True
        kept = [item["summary"].split(" ")[0] for item in reply["results"]]
        expected = [f"r{i}" for i in range(30 - len(kept), 30)]
        assert kept == expected
        dates = [item["date"] for item in reply["results"]]
        assert dates == sorted(dates)
        assert len(_dumps(reply)) <= 10_000


# ============================================================================
# The handler: the argument, include_superseded, context_ids
# ============================================================================


@pytest.fixture(autouse=True)
def _reset_view():
    set_mcp_tool_view_query(None)
    yield
    set_mcp_tool_view_query(None)


async def _recall(args: dict[str, Any], rows: list[MemoryResponse] | None = None):
    db = AsyncMock()

    async def fake_get_db():
        yield db

    seen: dict[str, Any] = {}

    async def fake_recall(request: RecallRequest, **kwargs):
        seen["request"] = request
        seen["kwargs"] = kwargs
        return RecallResponse(
            results=list(rows or []),
            confidence=RecallConfidence(
                level="moderate", top_score=0.6, result_count=3, rationale="r"
            ),
        )

    service = MagicMock()
    service.recall = fake_recall
    config_repo = MagicMock()
    config_repo.create_or_get = AsyncMock(
        return_value=MagicMock(embedding_model="text-embedding-3-small")
    )
    context = MagicMock(trust_tier="trusted", workspace_id=uuid4(), is_private=False)

    with (
        patch("db.base.get_db", new=fake_get_db),
        patch(
            "mcp_server.tools.memory._resolve_context_for_read",
            new=AsyncMock(return_value=context),
        ),
        patch("mcp_server.tools.memory._context_response_fields", return_value={}),
        patch("mcp_server.tools.memory._touch_context_last_used", new=AsyncMock()),
        patch("mcp_server.tools.memory._log_tool_usage", new=AsyncMock()),
        patch("services.memory_service.MemoryService", new=MagicMock(return_value=service)),
        patch(
            "repositories.config_repository.ContextSearchConfigRepository",
            new=MagicMock(return_value=config_repo),
        ),
    ):
        base = {"query": "q"}
        if "context_ids" not in args:
            base["context_id"] = str(uuid4())
        result = await handle_recall({**base, **args}, user_id="u1", workspace_id=None)
    return result[0].text, seen


def _rows() -> list[MemoryResponse]:
    return [
        _memory(_day(9, 3), summary="new", memory_id=UUID(int=3)),
        _memory(_day(9, 1), summary="old", memory_id=UUID(int=1), superseded_by=UUID(int=3)),
        _memory(_day(9, 2), summary="mid", memory_id=UUID(int=2), event_start="2026-09-02"),
    ]


class TestHandler:
    @pytest.mark.asyncio
    async def test_default_reply_is_byte_identical_to_relevance(self):
        omitted, seen_omitted = await _recall({}, _rows())
        relevance, seen_relevance = await _recall({"order": "relevance"}, _rows())
        nulled, _ = await _recall({"order": None}, _rows())
        assert omitted == relevance == nulled
        assert '"order"' not in omitted
        assert '"date' not in omitted
        assert seen_omitted["request"].include_superseded is False
        assert seen_relevance["request"].include_superseded is False

    @pytest.mark.asyncio
    async def test_date_order(self):
        text, seen = await _recall({"order": "date"}, _rows())
        reply = json.loads(text)
        assert reply["order"] == "date"
        assert [i["summary"] for i in reply["results"]] == ["old", "mid", "new"]
        assert [i["date_source"] for i in reply["results"]] == ["created", "event", "created"]
        # The superseded row stays labelled.
        assert reply["results"][0]["superseded_by"] == str(UUID(int=3))
        # confidence is the service's, from the relevance ranking.
        assert reply["confidence"]["level"] == "moderate"
        assert seen["request"].order == "date"

    @pytest.mark.asyncio
    async def test_date_order_includes_superseded_by_default(self):
        _text, seen = await _recall({"order": "date"})
        assert seen["request"].include_superseded is True

    @pytest.mark.asyncio
    async def test_explicit_include_superseded_false_wins(self):
        _text, seen = await _recall({"order": "date", "include_superseded": False})
        assert seen["request"].include_superseded is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", ["newest", "DATE", "", 1, True, ["date"]])
    async def test_an_unknown_order_is_invalid_argument(self, value):
        text, seen = await _recall({"order": value})
        reply = json.loads(text)
        assert reply["status"] == "error"
        assert reply["error"] == "invalid_argument"
        assert "request" not in seen

    @pytest.mark.asyncio
    async def test_context_ids(self):
        text, seen = await _recall(
            {"order": "date", "context_ids": [str(uuid4()), str(uuid4())]}, _rows()
        )
        reply = json.loads(text)
        assert [i["summary"] for i in reply["results"]] == ["old", "mid", "new"]
        assert len(seen["kwargs"]["context_ids"]) == 2

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["hybrid", "semantic", "keyword"])
    async def test_every_search_mode_and_date_filters(self, mode):
        filters = {"observed_after": "2026-01-01", "event_before": "2027-01-01"}
        text, seen = await _recall(
            {"order": "date", "search_mode": mode, "filters": filters}, _rows()
        )
        assert [i["summary"] for i in json.loads(text)["results"]] == ["old", "mid", "new"]
        assert seen["request"].search_mode == mode
        assert seen["request"].filters == filters

    @pytest.mark.asyncio
    async def test_verbosity_full(self):
        text, _ = await _recall({"order": "date", "verbosity": "full"}, _rows())
        item = json.loads(text)["results"][0]
        assert item["date"] == "2026-09-01"
        assert item["tags"] == ["falcon"]
