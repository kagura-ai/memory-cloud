"""``observed_at`` on recall rows and list items (#2330): present when set, absent otherwise.

#2689: ``observed_weekday`` (the stored day's weekday) follows it, under the
same rule; a row without ``observed_at`` keeps its keys exactly.
"""

from __future__ import annotations

import json
from contextlib import ExitStack
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from mcp_server.tools.listing import _memory_item
from mcp_server.tools.memory import handle_reference
from models.schemas import MemoryResponse, ReferenceResponse
from utils.recall_projection import recall_result_item


def _response(observed_at: str | None) -> MemoryResponse:
    return MemoryResponse(
        memory_id=uuid4(),
        summary="the price was 100",
        context_summary=None,
        type="fact",
        importance=0.5,
        scope="persistent",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=None,
        observed_at=observed_at,
        client="test",
        tags=[],
        context=None,
        score=0.9,
    )


@pytest.mark.parametrize("verbosity", ["compact", "full"])
def test_recall_row_carries_it_after_updated_at(verbosity):
    item = recall_result_item(_response("2024-03-07"), verbosity)
    assert item["observed_at"] == "2024-03-07"
    keys = list(item)
    assert keys.index("observed_at") == keys.index("updated_at") + 1


@pytest.mark.parametrize("verbosity", ["compact", "full"])
def test_recall_row_without_it_has_no_key(verbosity):
    assert "observed_at" not in recall_result_item(_response(None), verbosity)


@pytest.mark.parametrize("verbosity", ["compact", "full"])
@pytest.mark.parametrize(
    ("observed_at", "weekday"),
    [("2024-03-11", "Mon"), ("2024-03-14T23:30:00Z", "Thu")],
)
def test_recall_row_carries_the_weekday_after_it(verbosity, observed_at, weekday):
    item = recall_result_item(_response(observed_at), verbosity)
    assert item["observed_weekday"] == weekday
    keys = list(item)
    assert keys.index("observed_weekday") == keys.index("observed_at") + 1


# The keys of a row without observed_at, as before #2689: nothing added.
_COMPACT_KEYS = ["memory_id", "summary", "type", "importance", "score", "created_at", "updated_at"]
_FULL_KEYS = [
    "memory_id",
    "summary",
    "type",
    "importance",
    "scope",
    "score",
    "created_at",
    "updated_at",
]


@pytest.mark.parametrize(("verbosity", "keys"), [("compact", _COMPACT_KEYS), ("full", _FULL_KEYS)])
def test_recall_row_without_it_is_unchanged(verbosity, keys):
    assert list(recall_result_item(_response(None), verbosity)) == keys


def _row(details):
    return SimpleNamespace(
        id=uuid4(),
        summary="s",
        type="fact",
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


def test_list_item_carries_the_details_form():
    item = _memory_item(_row({"observed_at": "2024-03-07T10:00:00Z"}), include_details=False)
    assert item["observed_at"] == "2024-03-07T10:00:00Z"
    assert "details" not in item


@pytest.mark.parametrize("details", [None, {}, {"observed_at": "March 7"}])
def test_list_item_without_one_has_no_key(details):
    assert "observed_at" not in _memory_item(_row(details), include_details=False)


@pytest.mark.parametrize(
    ("observed_at", "weekday"),
    [("2024-03-11", "Mon"), ("2024-03-14T10:00:00Z", "Thu")],
)
def test_list_item_carries_the_weekday_after_it(observed_at, weekday):
    item = _memory_item(_row({"observed_at": observed_at}), include_details=True)
    assert item["observed_weekday"] == weekday
    keys = list(item)
    assert keys.index("observed_weekday") == keys.index("observed_at") + 1
    assert keys[-1] == "details"


@pytest.mark.parametrize("details", [None, {}, {"observed_at": "March 7"}])
def test_list_item_without_one_is_unchanged(details):
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


def _reference(observed_at: str | None) -> ReferenceResponse:
    return ReferenceResponse(
        memory_id=uuid4(),
        summary="went hiking",
        context_summary=None,
        content="last weekend (2024-03-09–10; material date Mon 2024-03-11)",
        details={"observed_at": observed_at} if observed_at else None,
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
    )


async def _reference_memory(response: ReferenceResponse) -> dict:
    service = MagicMock(reference=AsyncMock(return_value=response))

    async def gen():
        yield AsyncMock()

    with ExitStack() as stack:
        stack.enter_context(patch("db.base.get_db", new=gen))
        stack.enter_context(
            patch(
                "mcp_server.tools.memory._resolve_context_for_read",
                new=AsyncMock(return_value=MagicMock(last_used_at=None)),
            )
        )
        stack.enter_context(patch("mcp_server.tools.memory._log_tool_usage", new=AsyncMock()))
        stack.enter_context(patch("services.memory_service.MemoryService", return_value=service))
        result = await handle_reference(
            args={"memory_id": str(response.memory_id), "context_id": str(uuid4())},
            user_id="u",
            workspace_id=uuid4(),
        )
    return json.loads(result[0].text)["memory"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("observed_at", "weekday"),
    [("2024-03-11", "Mon"), ("2024-03-14T23:30:00Z", "Thu")],
)
async def test_reference_carries_the_weekday_after_it(observed_at, weekday):
    memory = await _reference_memory(_reference(observed_at))
    assert memory["observed_at"] == observed_at
    assert memory["observed_weekday"] == weekday
    keys = list(memory)
    assert keys.index("observed_weekday") == keys.index("observed_at") + 1


@pytest.mark.asyncio
async def test_reference_without_it_is_unchanged():
    without = await _reference_memory(_reference(None))
    with_it = await _reference_memory(_reference("2024-03-11"))
    assert "observed_at" not in without and "observed_weekday" not in without
    added = ("observed_at", "observed_weekday")
    assert [key for key in with_it if key not in added] == list(without)
