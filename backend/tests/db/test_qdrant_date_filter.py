"""Tests for date range filtering in Qdrant search.

Issue #78: Verify created_after, created_before, updated_after, updated_before filters.
"""

from datetime import UTC, datetime, timezone

import pytest
from qdrant_client.models import DatetimeRange, FieldCondition

from db.qdrant import _build_date_filter_conditions


class TestBuildDateFilterConditions:
    """Test _build_date_filter_conditions helper."""

    def test_created_after(self):
        """created_after should produce gte condition on created_at."""
        filters = {"created_after": "2026-03-01T00:00:00Z"}
        conditions = _build_date_filter_conditions(filters)

        assert len(conditions) == 1
        assert isinstance(conditions[0], FieldCondition)
        assert conditions[0].key == "created_at"
        assert isinstance(conditions[0].range, DatetimeRange)
        assert conditions[0].range.gte == datetime(2026, 3, 1, tzinfo=UTC)

    def test_created_before(self):
        """created_before should produce lte condition on created_at."""
        filters = {"created_before": "2026-03-31T23:59:59Z"}
        conditions = _build_date_filter_conditions(filters)

        assert len(conditions) == 1
        assert conditions[0].key == "created_at"
        assert conditions[0].range.lte == datetime(2026, 3, 31, 23, 59, 59, tzinfo=UTC)

    def test_updated_after(self):
        """updated_after should produce gte condition on updated_at."""
        filters = {"updated_after": "2026-03-15T00:00:00Z"}
        conditions = _build_date_filter_conditions(filters)

        assert len(conditions) == 1
        assert conditions[0].key == "updated_at"
        assert conditions[0].range.gte == datetime(2026, 3, 15, tzinfo=UTC)

    def test_updated_before(self):
        """updated_before should produce lte condition on updated_at."""
        filters = {"updated_before": "2026-04-01T00:00:00Z"}
        conditions = _build_date_filter_conditions(filters)

        assert len(conditions) == 1
        assert conditions[0].key == "updated_at"
        assert conditions[0].range.lte == datetime(2026, 4, 1, tzinfo=UTC)

    def test_combined_date_range(self):
        """Multiple date filters should produce multiple conditions."""
        filters = {
            "created_after": "2026-03-01T00:00:00Z",
            "created_before": "2026-03-31T23:59:59Z",
        }
        conditions = _build_date_filter_conditions(filters)

        assert len(conditions) == 2
        keys = [c.key for c in conditions]
        assert keys == ["created_at", "created_at"]

    def test_no_date_filters(self):
        """No date filters should return empty list."""
        assert _build_date_filter_conditions({"type": "code"}) == []
        assert _build_date_filter_conditions({}) == []

    def test_iso_offset_format(self):
        """Should handle ISO 8601 with timezone offset."""
        filters = {"created_after": "2026-03-01T09:00:00+09:00"}
        conditions = _build_date_filter_conditions(filters)

        assert len(conditions) == 1
        # +09:00 = midnight UTC
        assert conditions[0].range.gte == datetime(
            2026, 3, 1, 9, 0, 0, tzinfo=timezone(offset=__import__("datetime").timedelta(hours=9))
        )

    def test_naive_datetime_gets_utc(self):
        """Naive datetime string (no timezone) should be treated as UTC."""
        filters = {"created_after": "2026-03-01T00:00:00"}
        conditions = _build_date_filter_conditions(filters)

        assert len(conditions) == 1
        assert conditions[0].range.gte.tzinfo == UTC

    def test_invalid_datetime_raises(self):
        """Invalid datetime string should raise ValueError."""
        with pytest.raises(ValueError, match="Invalid datetime"):
            _build_date_filter_conditions({"created_after": "not-a-date"})

    def test_non_string_value_raises(self):
        """Non-string value should raise ValueError."""
        with pytest.raises(ValueError, match="ISO 8601 datetime string"):
            _build_date_filter_conditions({"created_after": 12345})
