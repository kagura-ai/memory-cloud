"""Pure parts of ``services.operations.job_status`` (Issue #2768): overdue, running, merge."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from services.operations.job_status import JobRunUpdate, is_overdue, is_running

T0 = datetime(2026, 11, 13, 0, 0, tzinfo=UTC)
GRACE = 900


@dataclass
class Row:
    trigger_kind: str = "cron"
    expected_interval_seconds: int | None = 86400
    last_registered_at: datetime = T0
    registered_since: datetime = T0
    last_started_at: datetime | None = None
    last_outcome_at: datetime | None = None


def _overdue(row: Row, now: datetime, latest: datetime | None = None) -> bool:
    return is_overdue(
        row,
        now=now,
        grace_seconds=GRACE,
        latest_registration=row.last_registered_at if latest is None else latest,
    )


class TestOverdue:
    def test_never_ran_within_interval_plus_grace_is_not_overdue(self) -> None:
        row = Row()
        assert not _overdue(row, T0 + timedelta(seconds=86400 + GRACE))
        assert _overdue(row, T0 + timedelta(seconds=86400 + GRACE + 1))

    def test_ran_recently_is_not_overdue(self) -> None:
        row = Row(last_started_at=T0 + timedelta(days=3), last_outcome_at=T0 + timedelta(days=3))
        assert not _overdue(row, T0 + timedelta(days=3, hours=20))

    def test_cron_run_skipped_by_downtime_is_overdue(self) -> None:
        started = T0 + timedelta(days=1)
        # A deploy re-registered the job in the same generation chain: the
        # cron base stays the last start.
        row = Row(
            last_started_at=started,
            last_outcome_at=started,
            last_registered_at=T0 + timedelta(days=2, hours=1),
        )
        assert _overdue(row, started + timedelta(seconds=86400 + GRACE + 1))

    def test_unregistered_row_is_never_overdue(self) -> None:
        row = Row(last_registered_at=T0)
        assert not _overdue(row, T0 + timedelta(days=30), latest=T0 + timedelta(days=1))

    def test_no_interval_is_never_overdue(self) -> None:
        assert not _overdue(Row(expected_interval_seconds=None), T0 + timedelta(days=365))

    def test_no_registration_at_all_is_not_overdue(self) -> None:
        row = Row()
        assert not is_overdue(
            row, now=T0 + timedelta(days=9), grace_seconds=GRACE, latest_registration=None
        )

    def test_interval_job_after_re_registration_is_not_overdue(self) -> None:
        """An interval trigger restarts its period at every registration."""
        last_run = T0
        deploy = T0 + timedelta(minutes=55)
        row = Row(
            trigger_kind="interval",
            expected_interval_seconds=3600,
            last_started_at=last_run,
            last_outcome_at=last_run,
            last_registered_at=deploy,
        )
        # Next fire is deploy + 1 h; until then (plus grace) the job is fine.
        assert not _overdue(row, last_run + timedelta(minutes=110))
        assert not _overdue(row, deploy + timedelta(seconds=3600 + GRACE))
        assert _overdue(row, deploy + timedelta(seconds=3600 + GRACE + 1))

    def test_long_running_cron_job_is_not_overdue_until_start_plus_interval(self) -> None:
        start = T0 + timedelta(days=1)
        row = Row(
            last_started_at=start,
            last_outcome_at=T0,  # the previous run's outcome
        )
        assert is_running(row)
        assert not _overdue(row, start + timedelta(hours=5))
        # A run that hangs is flagged at start + interval + grace.
        assert _overdue(row, start + timedelta(seconds=86400 + GRACE + 1))

    def test_re_enabled_job_is_not_overdue_at_once(self) -> None:
        """A job registered again after an absence starts from its new registration."""
        re_enabled = T0 + timedelta(days=40)
        row = Row(
            last_started_at=T0,
            last_outcome_at=T0,
            registered_since=re_enabled,
            last_registered_at=re_enabled,
        )
        assert not _overdue(row, re_enabled + timedelta(hours=1))
        assert _overdue(row, re_enabled + timedelta(seconds=86400 + GRACE + 1))


class TestRunning:
    @pytest.mark.parametrize(
        ("started", "outcome", "expected"),
        [
            (None, None, False),
            (T0, None, True),
            (T0 + timedelta(seconds=1), T0, True),
            (T0, T0 + timedelta(seconds=1), False),
            (T0, T0, False),
        ],
    )
    def test_running(self, started, outcome, expected) -> None:
        assert is_running(Row(last_started_at=started, last_outcome_at=outcome)) is expected


def _ok(at: datetime, **kw) -> JobRunUpdate:
    return JobRunUpdate(
        job_id="j",
        outcome="ok",
        outcome_at=at,
        finished_at=at,
        success_at=at,
        reset_failures=True,
        **kw,
    )


def _failed(at: datetime, cls: str = "OSError") -> JobRunUpdate:
    return JobRunUpdate(
        job_id="j",
        outcome="failed",
        outcome_at=at,
        finished_at=at,
        error_class=cls,
        failures_added=1,
    )


def _missed(at: datetime) -> JobRunUpdate:
    return JobRunUpdate(job_id="j", outcome="missed", outcome_at=at)


class TestMerge:
    def test_failures_add_up(self) -> None:
        merged = _failed(T0).then(_failed(T0 + timedelta(seconds=1), "ValueError"))
        assert (merged.outcome, merged.error_class, merged.failures_added) == (
            "failed",
            "ValueError",
            2,
        )
        assert merged.reset_failures is False

    def test_ok_after_failures_resets(self) -> None:
        merged = _failed(T0).then(_ok(T0 + timedelta(seconds=1), counts={"n": 1}))
        assert merged.reset_failures is True and merged.failures_added == 0
        assert merged.counts == {"n": 1} and merged.error_class is None

    def test_failure_after_ok_keeps_the_success(self) -> None:
        ok_at = T0 + timedelta(seconds=1)
        merged = _ok(ok_at).then(_failed(T0 + timedelta(seconds=2)))
        assert merged.reset_failures is True and merged.failures_added == 1
        assert merged.success_at == ok_at
        assert merged.outcome == "failed"

    def test_missed_keeps_the_previous_finish(self) -> None:
        ok_at = T0 + timedelta(seconds=1)
        merged = _ok(ok_at, counts={"n": 3}).then(_missed(T0 + timedelta(seconds=5)))
        assert merged.outcome == "missed"
        assert merged.outcome_at == T0 + timedelta(seconds=5)
        assert merged.finished_at == ok_at and merged.counts == {"n": 3}
        assert merged.success_at == ok_at and merged.reset_failures is True
