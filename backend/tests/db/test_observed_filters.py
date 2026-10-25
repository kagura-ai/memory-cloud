"""``filters.observed_after / observed_before`` on the vector engines (#2330).

The same rows as tests/services/test_memory_listing_observed_at.py (SQL): a
date, a datetime on the same day, a row without an event time, and a later
date. after is inclusive, before exclusive, a date bound is the start of its
day (UTC); a row without the payload never matches.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from qdrant_client.models import DatetimeRange

from db.lance_store import (
    LanceVectorStore,
    extract_observed_bounds,
    filter_results_by_observed,
)
from db.qdrant import _build_date_filter_conditions
from tests.db.test_lance_store_filter import CTX, USER, WS, _StubTable
from utils.observed_at import ObservedAtValidationError

ROWS = {
    "date": "2024-03-07T00:00:00Z",
    "datetime": "2024-03-07T10:00:00Z",
    "none": None,
    "later": "2024-03-09T00:00:00Z",
}

WINDOWS = [
    ({"observed_after": "2024-03-07"}, {"date", "datetime", "later"}),
    ({"observed_after": "2024-03-07T00:00:01Z"}, {"datetime", "later"}),
    ({"observed_before": "2024-03-08"}, {"date", "datetime"}),
    ({"observed_before": "2024-03-07T10:00:00Z"}, {"date"}),
    ({"observed_after": "2024-03-07T19:00:00+09:00"}, {"datetime", "later"}),
    ({"observed_after": "2024-03-08", "observed_before": "2024-03-10"}, {"later"}),
]


def _payload_rows() -> list[dict]:
    return [
        {"id": name, "payload": {"observed_at": value} if value else {}}
        for name, value in ROWS.items()
    ]


class TestQdrant:
    def test_after_is_gte_and_before_is_lt(self):
        conditions = _build_date_filter_conditions(
            {"observed_after": "2024-03-07", "observed_before": "2024-03-08T09:00:00+09:00"}
        )
        assert [c.key for c in conditions] == ["observed_at", "observed_at"]
        after, before = (c.range for c in conditions)
        assert isinstance(after, DatetimeRange) and isinstance(before, DatetimeRange)
        assert after.gte == datetime(2024, 3, 7, tzinfo=UTC) and after.lt is None
        assert before.lt == datetime(2024, 3, 8, tzinfo=UTC) and before.lte is None

    @pytest.mark.parametrize(("filters", "expected"), WINDOWS)
    def test_the_conditions_select_the_same_rows(self, filters, expected):
        # Evaluate the built ranges the way Qdrant does over the payload.
        kept = set()
        for name, value in ROWS.items():
            if value is None:
                continue
            moment = datetime.fromisoformat(value)
            ok = True
            for condition in _build_date_filter_conditions(filters):
                r = condition.range
                if r.gte is not None and not moment >= r.gte:
                    ok = False
                if r.lt is not None and not moment < r.lt:
                    ok = False
            if ok:
                kept.add(name)
        assert kept == expected

    @pytest.mark.parametrize("value", ["soon", 20240307, True])
    def test_a_bad_bound_is_refused(self, value):
        with pytest.raises(ObservedAtValidationError, match="observed_before"):
            _build_date_filter_conditions({"observed_before": value})


class TestLance:
    def test_no_bounds(self):
        assert extract_observed_bounds(None) is None
        assert extract_observed_bounds({"type": "note"}) is None

    def test_bounds_are_the_column_form(self):
        assert extract_observed_bounds(
            {"observed_after": "2024-03-07", "observed_before": "2024-03-08T09:00:00+09:00"}
        ) == ("2024-03-07T00:00:00Z", "2024-03-08T00:00:00Z")

    @pytest.mark.parametrize(("filters", "expected"), WINDOWS)
    def test_the_post_filter_selects_the_same_rows(self, filters, expected):
        bounds = extract_observed_bounds(filters)
        assert bounds is not None
        kept = filter_results_by_observed(_payload_rows(), bounds)
        assert {row["id"] for row in kept} == expected

    @pytest.mark.asyncio
    async def test_search_semantic_applies_it(self):
        store = LanceVectorStore.__new__(LanceVectorStore)
        rows = [
            {
                "id": name,
                "_distance": 0.05 + i / 100,
                "payload_json": json.dumps({"observed_at": value} if value else {}),
            }
            for i, (name, value) in enumerate(ROWS.items())
        ]
        store._open = lambda _name, dim=0: _StubTable(rows)
        out = await store.search_semantic(
            USER, [0.1], WS, CTX, filters={"observed_before": "2024-03-08"}
        )
        assert [r["id"] for r in out] == ["date", "datetime"]

    @pytest.mark.asyncio
    async def test_a_bad_bound_fails_before_the_table(self):
        store = LanceVectorStore.__new__(LanceVectorStore)
        store._open = lambda _name, dim=0: _StubTable([])
        with pytest.raises(ValueError, match="observed_after"):
            await store.search_semantic(USER, [0.1], WS, CTX, filters={"observed_after": "x"})
