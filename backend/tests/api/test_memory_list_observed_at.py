"""``observed_at`` on GET /memory/list items (#2330): the details form, or None."""

from datetime import datetime
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from api.routes.memory import _list_memory_item


def _row(details):
    mem = MagicMock()
    mem.id = uuid4()
    mem.summary = "Test memory"
    mem.type = "note"
    mem.scope = "persistent"
    mem.importance = 0.8
    mem.created_at = datetime(2026, 4, 1)
    mem.updated_at = None
    mem.location_lat = None
    mem.location_lon = None
    mem.details = details
    return mem


@pytest.mark.parametrize(
    ("details", "expected"),
    [
        ({"observed_at": "2024-03-07"}, "2024-03-07"),
        ({"observed_at": "2024-03-07T10:00:00Z"}, "2024-03-07T10:00:00Z"),
        ({"observed_at": "March 7"}, None),
        ({"other": 1}, None),
        (None, None),
    ],
)
def test_list_item_carries_the_details_form(details, expected):
    assert _list_memory_item(_row(details)).observed_at == expected


@pytest.fixture(autouse=True)
def _reach_everything():
    """#2325: with no ``context_id`` the list also keeps to the contexts the
    caller reaches (one membership read). These mocked-DB tests cover the other
    filters, so that read is stubbed to reach everything; the real rule runs
    against PostgreSQL in test_2325_context_less_views_reach_db.py."""
    from unittest.mock import AsyncMock, patch

    from sqlalchemy import true

    with patch("api.routes.memory.reachable_memory_filter", AsyncMock(return_value=true())):
        yield
