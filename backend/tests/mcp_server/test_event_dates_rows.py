"""The event's own days on recall rows, list items and the reference.

``event_start`` / ``event_end`` / ``event_weekday`` (the weekday of
``event_start``) follow ``observed_weekday`` when the memory has them, under
the "absent, never null" rule: a row without an event date keeps its keys
exactly as before.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from mcp_server.tools.listing import _memory_item
from models.schemas import MemoryResponse, ReferenceResponse
from tests.mcp_server.test_observed_at_rows import _reference_memory
from utils.recall_projection import recall_result_item

EVENT = {"event_start": "2024-03-09", "event_end": "2024-03-10"}


def _response(observed_at: str | None = None, **event) -> MemoryResponse:
    return MemoryResponse(
        memory_id=uuid4(),
        summary="went hiking",
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


class TestRecallRows:
    @pytest.mark.parametrize("verbosity", ["compact", "full"])
    def test_the_days_follow_the_observed_weekday(self, verbosity):
        item = recall_result_item(_response("2024-03-11", **EVENT), verbosity)
        assert (item["event_start"], item["event_end"], item["event_weekday"]) == (
            "2024-03-09",
            "2024-03-10",
            "Sat",
        )
        keys = list(item)
        assert keys.index("event_start") == keys.index("observed_weekday") + 1
        assert keys[keys.index("event_start") :][:3] == [
            "event_start",
            "event_end",
            "event_weekday",
        ]

    @pytest.mark.parametrize("verbosity", ["compact", "full"])
    def test_without_an_observed_at_they_follow_updated_at(self, verbosity):
        item = recall_result_item(_response(None, **EVENT), verbosity)
        assert "observed_at" not in item
        keys = list(item)
        assert keys.index("event_start") == keys.index("updated_at") + 1

    @pytest.mark.parametrize("verbosity", ["compact", "full"])
    def test_a_row_without_them_is_unchanged(self, verbosity):
        with_observed = recall_result_item(_response("2024-03-11"), verbosity)
        bare = recall_result_item(_response(None), verbosity)
        for item in (with_observed, bare):
            assert not {"event_start", "event_end", "event_weekday"} & set(item)
        assert list(bare) == [
            k for k in with_observed if k not in ("observed_at", "observed_weekday")
        ]

    def test_the_rest_response_leaves_them_out(self):
        """REST recall serialises MemoryResponse: its rows stay as they were."""
        dumped = _response("2024-03-11", **EVENT).model_dump(mode="json")
        assert dumped["observed_at"] == "2024-03-11"
        assert "event_start" not in dumped and "event_end" not in dumped
        schema = MemoryResponse.model_json_schema(mode="serialization")
        assert "event_start" not in schema["properties"]


def _row(details):
    return SimpleNamespace(
        id=uuid4(),
        summary="s",
        type="event",
        importance=0.5,
        scope="persistent",
        tags=[],
        delivery_mode="on_recall",
        source_type="manual",
        created_at=datetime(2026, 1, 1),
        updated_at=None,
        version=1,
        details=details,
    )


class TestListItems:
    def test_the_days_follow_the_observed_weekday_before_details(self):
        item = _memory_item(
            _row({"observed_at": "2024-03-11", "event_phrase": "last weekend", **EVENT}),
            include_details=True,
        )
        assert (item["event_start"], item["event_end"], item["event_weekday"]) == (
            "2024-03-09",
            "2024-03-10",
            "Sat",
        )
        keys = list(item)
        assert keys.index("event_start") == keys.index("observed_weekday") + 1
        assert keys[-1] == "details"

    def test_a_start_alone_is_a_single_day(self):
        item = _memory_item(_row({"event_start": "2024-03-09"}), include_details=False)
        assert (item["event_start"], item["event_end"]) == ("2024-03-09", "2024-03-09")

    @pytest.mark.parametrize(
        "details",
        [
            None,
            {},
            {"event_phrase": "soon"},
            {"event_start": "March 9"},
            {"event_end": "2024-03-10"},
        ],
    )
    def test_without_them_the_item_is_unchanged(self, details):
        assert list(_memory_item(_row(details), include_details=False)) == [
            "memory_id",
            "summary",
            "type",
            "importance",
            "scope",
            "tags",
            "delivery_mode",
            "source_type",
            "created_at",
            "updated_at",
            "version",
        ]


def _reference(observed_at: str | None, **event) -> ReferenceResponse:
    return ReferenceResponse(
        memory_id=uuid4(),
        summary="went hiking",
        context_summary=None,
        content="last weekend (2024-03-09–10)",
        details={"observed_at": observed_at, "event_phrase": "last weekend", **event}
        if observed_at
        else None,
        type="event",
        scope="persistent",
        importance=0.5,
        tags=[],
        context=None,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 2, tzinfo=UTC),
        observed_at=observed_at,
        version=1,
        client="mcp",
        outgoing_links=[],
        incoming_links=[],
        **event,
    )


class TestReference:
    @pytest.mark.asyncio
    async def test_the_days_follow_the_observed_weekday(self):
        memory = await _reference_memory(_reference("2024-03-11", **EVENT))
        assert (memory["event_start"], memory["event_end"], memory["event_weekday"]) == (
            "2024-03-09",
            "2024-03-10",
            "Sat",
        )
        keys = list(memory)
        assert keys.index("event_start") == keys.index("observed_weekday") + 1

    @pytest.mark.asyncio
    async def test_without_them_the_reference_is_unchanged(self):
        without = await _reference_memory(_reference("2024-03-11"))
        with_them = await _reference_memory(_reference("2024-03-11", **EVENT))
        assert not {"event_start", "event_end", "event_weekday"} & set(without)
        added = ("event_start", "event_end", "event_weekday")
        assert [key for key in with_them if key not in added] == list(without)

    def test_the_rest_response_leaves_them_out(self):
        schema = ReferenceResponse.model_json_schema(mode="serialization")
        assert "event_start" not in schema["properties"]
        assert "observed_at" in schema["properties"]
