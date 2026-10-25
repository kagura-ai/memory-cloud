"""``list`` × observed_at against real Postgres (#2330).

The three rows used on every engine (SQL here; Qdrant and LanceDB in
tests/db/test_observed_filters.py): a date, a datetime on the same day, and a
row without an event time. after is inclusive, before exclusive, a date bound
is the start of its day; order_by="observed_at" puts the row without one last
in both directions.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from services.memory_listing import compile_memory_filters, list_memories
from tests.services.test_memory_listing import _row, _scope


@pytest.fixture
async def three(db_session):
    owner = f"obs-{uuid4().hex[:8]}"
    ws, ctx = await _scope(db_session, owner, private=False)
    await _row(db_session, owner, ws, ctx, summary="date", details={"observed_at": "2024-03-07"})
    await _row(
        db_session,
        owner,
        ws,
        ctx,
        summary="datetime",
        details={"observed_at": "2024-03-07T10:00:00Z"},
    )
    await _row(db_session, owner, ws, ctx, summary="none", details={"k": 1})
    await _row(db_session, owner, ws, ctx, summary="later", details={"observed_at": "2024-03-09"})
    return ctx


async def _summaries(db, ctx, **kwargs) -> list[str]:
    page = await list_memories(db, context_id=ctx, owner_user_id=None, **kwargs)
    return [m.summary for m in page.rows]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        ({"observed_after": "2024-03-07"}, {"date", "datetime", "later"}),
        ({"observed_after": "2024-03-07T00:00:01Z"}, {"datetime", "later"}),
        ({"observed_before": "2024-03-08"}, {"date", "datetime"}),
        ({"observed_before": "2024-03-07T10:00:00Z"}, {"date"}),  # exclusive
        ({"observed_after": "2024-03-07T19:00:00+09:00"}, {"datetime", "later"}),  # = 10:00Z
        ({"observed_after": "2024-03-08", "observed_before": "2024-03-10"}, {"later"}),
    ],
)
async def test_observed_window(db_session, three, filters, expected):
    assert set(await _summaries(db_session, three, filters=filters)) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["asc", "desc"])
async def test_order_by_observed_at_puts_the_row_without_one_last(db_session, three, direction):
    rows = await _summaries(db_session, three, order_by="observed_at", direction=direction)
    dated = ["date", "datetime", "later"]
    assert rows == (dated if direction == "asc" else dated[::-1]) + ["none"]


@pytest.mark.asyncio
async def test_order_by_observed_at_pages_are_stable(db_session, three):
    first = await list_memories(
        db_session,
        context_id=three,
        owner_user_id=None,
        order_by="observed_at",
        direction="asc",
        limit=2,
    )
    rest = await list_memories(
        db_session,
        context_id=three,
        owner_user_id=None,
        order_by="observed_at",
        direction="asc",
        offset=2,
        limit=2,
    )
    assert [m.summary for m in first.rows + rest.rows] == ["date", "datetime", "later", "none"]
    assert first.has_more is True and rest.has_more is False


@pytest.mark.parametrize(
    "filters",
    [{"observed_after": "soon"}, {"observed_before": 20240307}, {"observed_after": True}],
)
def test_a_bad_bound_is_refused(filters):
    with pytest.raises(ValueError, match="observed_"):
        compile_memory_filters(filters)
