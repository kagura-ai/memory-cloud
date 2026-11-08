"""``filters.event_after / event_before`` on the vector engines.

The same rows as tests/services/test_memory_listing_event_dates.py (SQL): a
single-day event, a week-long one, a row without an event date and a later
one. A row matches when its ``[event_start, event_end]`` overlaps
``[after, before)``: after inclusive, before exclusive, a datetime bound
reduced to its UTC day; a row without the payload never matches.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from qdrant_client.models import DatetimeRange

from db.lance_store import (
    LanceVectorStore,
    extract_event_bounds,
    filter_results_by_event,
)
from db.qdrant import _build_date_filter_conditions
from tests.db.test_lance_store_filter import CTX, USER, WS, _StubTable
from utils.event_dates import EventDateValidationError

ROWS = {
    "single": ("2024-03-09", "2024-03-09"),
    "range": ("2024-03-04", "2024-03-10"),
    "none": None,
    "later": ("2024-03-20", "2024-03-21"),
}

WINDOWS = [
    ({"event_after": "2024-03-09"}, {"single", "range", "later"}),
    ({"event_before": "2024-03-09"}, {"range"}),
    ({"event_before": "2024-03-10"}, {"single", "range"}),
    ({"event_after": "2024-03-10", "event_before": "2024-03-11"}, {"range"}),
    ({"event_after": "2024-03-01", "event_before": "2024-03-09"}, {"range"}),
    # A window inside one day keeps the events of that day.
    (
        {"event_after": "2024-03-09T12:00:00Z", "event_before": "2024-03-09T18:00:00Z"},
        {"single", "range"},
    ),
    ({"event_after": "2024-03-11"}, {"later"}),
    ({"event_before": "2024-03-04"}, set()),
    ({"event_after": "2024-03-10T00:00:00Z"}, {"range", "later"}),
]


def _payload(value) -> dict:
    if value is None:
        return {}
    start, end = value
    return {"event_start": start + "T00:00:00Z", "event_end": end + "T00:00:00Z"}


def _payload_rows() -> list[dict]:
    return [{"id": name, "payload": _payload(value)} for name, value in ROWS.items()]


class TestQdrant:
    def test_after_reads_the_end_and_before_the_start(self):
        conditions = _build_date_filter_conditions(
            {"event_after": "2024-03-09", "event_before": "2024-03-10T09:00:00+09:00"}
        )
        assert [c.key for c in conditions] == ["event_end", "event_start"]
        after, before = (c.range for c in conditions)
        assert isinstance(after, DatetimeRange) and isinstance(before, DatetimeRange)
        assert after.gte == datetime(2024, 3, 9, tzinfo=UTC) and after.lt is None
        # 09:00+09:00 is UTC midnight of the 10th: the day itself.
        assert before.lt == datetime(2024, 3, 10, tzinfo=UTC) and before.lte is None

    def test_a_before_bound_during_the_day_includes_that_day(self):
        (before,) = _build_date_filter_conditions({"event_before": "2024-03-10T12:00:00Z"})
        assert before.range.lt == datetime(2024, 3, 11, tzinfo=UTC)

    @pytest.mark.parametrize(("filters", "expected"), WINDOWS)
    def test_the_conditions_select_the_same_rows(self, filters, expected):
        # Evaluate the built ranges the way Qdrant does over the payload.
        kept = set()
        for name, value in ROWS.items():
            payload = _payload(value)
            if not payload:
                continue
            ok = True
            for condition in _build_date_filter_conditions(filters):
                moment = datetime.fromisoformat(payload[condition.key])
                r = condition.range
                if r.gte is not None and not moment >= r.gte:
                    ok = False
                if r.lt is not None and not moment < r.lt:
                    ok = False
            if ok:
                kept.add(name)
        assert kept == expected

    def test_both_date_filters_compose(self):
        conditions = _build_date_filter_conditions(
            {"observed_after": "2024-03-07", "event_before": "2024-03-10"}
        )
        assert [c.key for c in conditions] == ["observed_at", "event_start"]

    @pytest.mark.parametrize("value", ["soon", 20240309, True])
    def test_a_bad_bound_is_refused(self, value):
        with pytest.raises(EventDateValidationError, match="event_before"):
            _build_date_filter_conditions({"event_before": value})


class TestLance:
    def test_no_bounds(self):
        assert extract_event_bounds(None) is None
        assert extract_event_bounds({"type": "note"}) is None
        assert extract_event_bounds({"observed_after": "2024-03-07"}) is None

    def test_bounds_are_the_payload_form(self):
        assert extract_event_bounds(
            {"event_after": "2024-03-09", "event_before": "2024-03-10T09:00:00+09:00"}
        ) == ("2024-03-09T00:00:00Z", "2024-03-10T00:00:00Z")
        assert extract_event_bounds({"event_before": "2024-03-10T12:00:00Z"}) == (
            None,
            "2024-03-11T00:00:00Z",
        )

    @pytest.mark.parametrize(("filters", "expected"), WINDOWS)
    def test_the_post_filter_selects_the_same_rows(self, filters, expected):
        bounds = extract_event_bounds(filters)
        assert bounds is not None
        kept = filter_results_by_event(_payload_rows(), bounds)
        assert {row["id"] for row in kept} == expected

    def test_a_payload_with_a_start_only_is_a_single_day(self):
        rows = [{"id": "s", "payload": {"event_start": "2024-03-09T00:00:00Z"}}]
        assert filter_results_by_event(rows, ("2024-03-09T00:00:00Z", None)) == rows
        assert filter_results_by_event(rows, ("2024-03-10T00:00:00Z", None)) == []

    @pytest.mark.asyncio
    async def test_search_semantic_applies_it(self):
        store = LanceVectorStore.__new__(LanceVectorStore)
        rows = [
            {"id": name, "_distance": 0.05 + i / 100, "payload_json": json.dumps(_payload(value))}
            for i, (name, value) in enumerate(ROWS.items())
        ]
        store._open = lambda _name, dim=0: _StubTable(rows)
        out = await store.search_semantic(
            USER,
            [0.1],
            WS,
            CTX,
            filters={"event_after": "2024-03-10", "event_before": "2024-03-11"},
        )
        assert [r["id"] for r in out] == ["range"]

    @pytest.mark.asyncio
    async def test_a_bad_bound_fails_before_the_table(self):
        store = LanceVectorStore.__new__(LanceVectorStore)
        store._open = lambda _name, dim=0: _StubTable([])
        with pytest.raises(ValueError, match="event_after"):
            await store.search_semantic(USER, [0.1], WS, CTX, filters={"event_after": "x"})
