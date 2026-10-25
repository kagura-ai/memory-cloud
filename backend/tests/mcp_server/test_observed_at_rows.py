"""``observed_at`` on recall rows and list items (#2330): present when set, absent otherwise."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from mcp_server.tools.listing import _memory_item
from models.schemas import MemoryResponse
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
