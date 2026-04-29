"""Tests for utils.datetime — to_utc_iso, utcnow, parse_iso8601_to_aware.

Issue #489: serialization helpers must produce JS-parseable Z-suffixed ISO 8601
strings regardless of input shape (None, naive, tz-aware, double-Z avoidance).
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from utils.datetime import parse_iso8601_to_aware, to_utc_iso, utcnow


class TestToUtcIso:
    def test_none_returns_none(self):
        assert to_utc_iso(None) is None

    def test_naive_datetime_appends_z(self):
        dt = datetime(2026, 4, 28, 17, 50, 22)
        assert to_utc_iso(dt) == "2026-04-28T17:50:22Z"

    def test_tz_aware_utc_replaces_offset_with_z(self):
        dt = datetime(2026, 4, 28, 17, 50, 22, tzinfo=UTC)
        # +00:00 must be replaced by Z, NOT both ("+00:00Z" double-suffix bug)
        assert to_utc_iso(dt) == "2026-04-28T17:50:22Z"

    def test_tz_aware_non_utc_keeps_offset(self):
        # JST (+09:00) is preserved, not Z. Only +00:00 is normalized to Z.
        dt = datetime(2026, 4, 28, 17, 50, 22, tzinfo=ZoneInfo("Asia/Tokyo"))
        result = to_utc_iso(dt)
        assert result == "2026-04-28T17:50:22+09:00"

    def test_microseconds_preserved(self):
        dt = datetime(2026, 4, 28, 17, 50, 22, 123456)
        assert to_utc_iso(dt) == "2026-04-28T17:50:22.123456Z"

    def test_already_z_suffixed_via_tz_object(self):
        # timezone.utc and datetime.UTC produce "+00:00" in isoformat — both
        # paths must hit the same Z-replacement.
        dt = datetime(2026, 4, 28, 17, 50, 22, tzinfo=UTC)
        assert to_utc_iso(dt) == "2026-04-28T17:50:22Z"


class TestUtcnow:
    def test_returns_naive_datetime(self):
        dt = utcnow()
        assert dt.tzinfo is None

    def test_close_to_now(self):
        before = datetime.now(UTC).replace(tzinfo=None)
        dt = utcnow()
        after = datetime.now(UTC).replace(tzinfo=None)
        assert before <= dt <= after


class TestParseIso8601ToAware:
    def test_z_suffix(self):
        dt = parse_iso8601_to_aware("2026-04-28T17:50:22Z", "field")
        assert dt.tzinfo is not None
        assert dt.utcoffset().total_seconds() == 0

    def test_offset_form(self):
        dt = parse_iso8601_to_aware("2026-04-28T17:50:22+09:00", "field")
        assert dt.utcoffset().total_seconds() == 9 * 3600

    def test_naive_input_assumed_utc(self):
        dt = parse_iso8601_to_aware("2026-04-28T17:50:22", "field")
        assert dt.tzinfo is not None
        assert dt.utcoffset().total_seconds() == 0

    def test_invalid_raises(self):
        with pytest.raises(ValueError, match="Invalid datetime"):
            parse_iso8601_to_aware("not-a-date", "field")
