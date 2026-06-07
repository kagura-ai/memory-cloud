"""Unit tests for resource-events service pure helpers (Issue #316).

DB-free — covers the limit clamp and the ``since`` aware→naive-UTC
normalization. The DB-backed keyset/filter semantics live in
``tests/integration/test_resource_events_query.py``.
"""

from datetime import UTC, datetime, timedelta, timezone

from services.resource_events import (
    DEFAULT_EVENTS_PAGE_SIZE,
    MAX_EVENTS_PAGE_SIZE,
    _clamp_limit,
    _normalize_since,
)


class TestClampLimit:
    def test_none_uses_default(self):
        assert _clamp_limit(None) == DEFAULT_EVENTS_PAGE_SIZE

    def test_zero_and_negative_use_default(self):
        assert _clamp_limit(0) == DEFAULT_EVENTS_PAGE_SIZE
        assert _clamp_limit(-10) == DEFAULT_EVENTS_PAGE_SIZE

    def test_in_range_passes_through(self):
        assert _clamp_limit(5) == 5
        assert _clamp_limit(MAX_EVENTS_PAGE_SIZE) == MAX_EVENTS_PAGE_SIZE

    def test_over_max_clamped(self):
        assert _clamp_limit(MAX_EVENTS_PAGE_SIZE + 1) == MAX_EVENTS_PAGE_SIZE
        assert _clamp_limit(10_000) == MAX_EVENTS_PAGE_SIZE


class TestNormalizeSince:
    def test_none_passes_through(self):
        assert _normalize_since(None) is None

    def test_naive_passes_through_unchanged(self):
        dt = datetime(2026, 6, 7, 12, 0, 0)
        assert _normalize_since(dt) == dt
        assert _normalize_since(dt).tzinfo is None

    def test_aware_utc_becomes_naive(self):
        aware = datetime(2026, 6, 7, 12, 0, 0, tzinfo=UTC)
        result = _normalize_since(aware)
        assert result == datetime(2026, 6, 7, 12, 0, 0)
        assert result.tzinfo is None

    def test_aware_offset_converted_to_utc(self):
        # 12:00 at +09:00 (JST) is 03:00 UTC.
        jst = timezone(timedelta(hours=9))
        aware = datetime(2026, 6, 7, 12, 0, 0, tzinfo=jst)
        result = _normalize_since(aware)
        assert result == datetime(2026, 6, 7, 3, 0, 0)
        assert result.tzinfo is None
