"""``list`` × the event's own days against real Postgres.

The rows used on every engine (SQL here; Qdrant and LanceDB in
tests/db/test_event_filters.py): a single-day event, a week-long one, a row
without an event date, a row with an unresolved phrase only, and a later
event. A row matches ``event_after`` / ``event_before`` when its
``[event_start, event_end]`` overlaps the window — after inclusive, before
exclusive, a datetime bound reduced to its UTC day; rows without an event
date never match and ``order_by="event_start"`` puts them last in both
directions.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from services.memory_listing import compile_memory_filters, list_memories
from tests.services.test_memory_listing import _row, _scope


@pytest.fixture
async def events(db_session):
    owner = f"evt-{uuid4().hex[:8]}"
    ws, ctx = await _scope(db_session, owner, private=False)
    await _row(
        db_session,
        owner,
        ws,
        ctx,
        summary="single",
        details={"event_start": "2024-03-09", "event_end": "2024-03-09"},
    )
    await _row(
        db_session,
        owner,
        ws,
        ctx,
        summary="range",
        details={
            "observed_at": "2024-03-11",
            "event_phrase": "last week",
            "event_start": "2024-03-04",
            "event_end": "2024-03-10",
        },
    )
    await _row(db_session, owner, ws, ctx, summary="none", details={"observed_at": "2024-03-09"})
    await _row(db_session, owner, ws, ctx, summary="phrase", details={"event_phrase": "soon"})
    await _row(
        db_session,
        owner,
        ws,
        ctx,
        summary="later",
        details={"event_start": "2024-03-20", "event_end": "2024-03-21"},
    )
    return ctx


async def _summaries(db, ctx, **kwargs) -> list[str]:
    page = await list_memories(db, context_id=ctx, owner_user_id=None, **kwargs)
    return [m.summary for m in page.rows]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        ({"event_after": "2024-03-09"}, {"single", "range", "later"}),
        ({"event_before": "2024-03-09"}, {"range"}),
        ({"event_before": "2024-03-10"}, {"single", "range"}),
        ({"event_after": "2024-03-10", "event_before": "2024-03-11"}, {"range"}),
        ({"event_after": "2024-03-01", "event_before": "2024-03-09"}, {"range"}),
        (
            {"event_after": "2024-03-09T12:00:00Z", "event_before": "2024-03-09T18:00:00Z"},
            {"single", "range"},
        ),
        ({"event_after": "2024-03-11"}, {"later"}),
        ({"event_before": "2024-03-04"}, set()),
        # The rows without an event date never match, whatever their observed_at.
        ({"event_after": "2000-01-01"}, {"single", "range", "later"}),
        # An event filter composes with the observed one.
        ({"event_after": "2024-03-01", "observed_after": "2024-03-11"}, {"range"}),
    ],
)
async def test_event_window(db_session, events, filters, expected):
    assert set(await _summaries(db_session, events, filters=filters)) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["asc", "desc"])
async def test_order_by_event_start_puts_the_rows_without_one_last(db_session, events, direction):
    rows = await _summaries(db_session, events, order_by="event_start", direction=direction)
    dated = ["range", "single", "later"]
    assert rows[:3] == (dated if direction == "asc" else dated[::-1])
    assert set(rows[3:]) == {"none", "phrase"}


@pytest.mark.asyncio
async def test_the_generated_columns_hold_the_days(db_session, events):
    page = await list_memories(
        db_session, context_id=events, owner_user_id=None, filters={"event_before": "2024-03-09"}
    )
    (row,) = page.rows
    assert (row.event_start, row.event_end) == ("2024-03-04", "2024-03-10")


@pytest.mark.parametrize(
    "filters",
    [{"event_after": "soon"}, {"event_before": 20240309}, {"event_after": True}],
)
def test_a_bad_bound_is_refused(filters):
    with pytest.raises(ValueError, match="event_"):
        compile_memory_filters(filters)
