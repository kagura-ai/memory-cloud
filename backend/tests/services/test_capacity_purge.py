"""#1969: pure pieces of the capacity purge — the clock arithmetic."""

from datetime import datetime, timedelta

from services.capacity_purge import NOTICE_DAYS, PURGE_AFTER, purge_at


def test_purge_at_is_thirty_days_after_lock_start() -> None:
    since = datetime(2026, 10, 1, 4, 0)
    assert purge_at(since) == datetime(2026, 10, 31, 4, 0)
    assert PURGE_AFTER == timedelta(days=30)


def test_purge_at_none_when_not_locked() -> None:
    assert purge_at(None) is None


def test_notice_days_are_the_policy_series() -> None:
    assert NOTICE_DAYS == (0, 7, 14, 23, 27, 29)
