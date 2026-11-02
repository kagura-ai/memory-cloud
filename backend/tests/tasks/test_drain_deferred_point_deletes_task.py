"""#2476: the drain job for forget's deferred point deletes — scheduled, bounded, switchable."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import structlog

from services.deferred_point_deletes import DrainResult
from tasks.neural_tasks import drain_deferred_point_deletes_task, schedule_neural_tasks
from tests.tasks.conftest import mock_get_db_factory

_INTERVAL = "DEFERRED_POINT_DRAIN_INTERVAL_SECONDS"
_WAIT = "DEFERRED_POINT_DRAIN_WAIT_SECONDS"


@pytest.fixture
def drain():
    with patch(
        "services.deferred_point_deletes.drain_deferred_point_deletes",
        AsyncMock(return_value=DrainResult(drained=2, kept=1, pending=0, oldest_age_seconds=40.0)),
    ) as mock:
        yield mock


def _jobs(scheduler):
    return {call.kwargs["id"]: call for call in scheduler.add_job.call_args_list}


class TestScheduling:
    def test_scheduled_every_five_minutes_by_default(self, monkeypatch):
        monkeypatch.delenv(_INTERVAL, raising=False)
        scheduler = MagicMock()

        schedule_neural_tasks(scheduler)

        job = _jobs(scheduler)["drain_deferred_point_deletes"]
        assert job.args[0] is drain_deferred_point_deletes_task
        assert job.kwargs["trigger"].interval.total_seconds() == 300

    def test_the_interval_is_read_at_schedule_time(self, monkeypatch):
        monkeypatch.setenv(_INTERVAL, "60")
        scheduler = MagicMock()

        schedule_neural_tasks(scheduler)

        job = _jobs(scheduler)["drain_deferred_point_deletes"]
        assert job.kwargs["trigger"].interval.total_seconds() == 60

    def test_zero_turns_the_job_off(self, monkeypatch):
        monkeypatch.setenv(_INTERVAL, "0")
        scheduler = MagicMock()

        schedule_neural_tasks(scheduler)

        assert "drain_deferred_point_deletes" not in _jobs(scheduler)

    @pytest.mark.parametrize("value", ["abc", "-5", "nan", "inf"])
    def test_a_bad_interval_falls_back_to_the_default(self, monkeypatch, value):
        monkeypatch.setenv(_INTERVAL, value)
        scheduler = MagicMock()

        schedule_neural_tasks(scheduler)

        job = _jobs(scheduler)["drain_deferred_point_deletes"]
        assert job.kwargs["trigger"].interval.total_seconds() == 300

    def test_scheduled_with_the_orphan_sweep_off(self, monkeypatch):
        monkeypatch.setenv("ORPHAN_VECTOR_SWEEP_ENABLED", "false")
        monkeypatch.delenv(_INTERVAL, raising=False)
        scheduler = MagicMock()

        schedule_neural_tasks(scheduler)

        assert "drain_deferred_point_deletes" in _jobs(scheduler)


class TestDrainTask:
    @pytest.mark.asyncio
    async def test_waits_thirty_seconds_by_default(self, drain, monkeypatch):
        monkeypatch.delenv(_WAIT, raising=False)
        db = MagicMock()

        with patch("tasks.neural_tasks.get_db", mock_get_db_factory(db)):
            await drain_deferred_point_deletes_task()

        drain.assert_awaited_once_with(db, wait_seconds=30.0)

    @pytest.mark.asyncio
    async def test_the_wait_is_read_at_call_time(self, drain, monkeypatch):
        monkeypatch.setenv(_WAIT, "5")
        db = MagicMock()

        with patch("tasks.neural_tasks.get_db", mock_get_db_factory(db)):
            await drain_deferred_point_deletes_task()

        drain.assert_awaited_once_with(db, wait_seconds=5.0)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", ["-1", "soon", "inf"])
    async def test_a_bad_wait_falls_back_to_the_bound(self, drain, monkeypatch, value):
        """Never an unbounded wait from a typo."""
        monkeypatch.setenv(_WAIT, value)

        with patch("tasks.neural_tasks.get_db", mock_get_db_factory(MagicMock())):
            await drain_deferred_point_deletes_task()

        assert drain.await_args.kwargs["wait_seconds"] == 30.0

    @pytest.mark.asyncio
    async def test_runs_with_the_orphan_sweep_off(self, drain, monkeypatch):
        monkeypatch.setenv("ORPHAN_VECTOR_SWEEP_ENABLED", "false")

        with patch("tasks.neural_tasks.get_db", mock_get_db_factory(MagicMock())):
            await drain_deferred_point_deletes_task()

        drain.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_logs_what_it_drained(self, drain):
        with (
            patch("tasks.neural_tasks.get_db", mock_get_db_factory(MagicMock())),
            structlog.testing.capture_logs() as logs,
        ):
            await drain_deferred_point_deletes_task()

        entry = next(e for e in logs if e["event"] == "deferred_point_deletes_drained")
        assert (entry["drained"], entry["kept"], entry["pending"]) == (2, 1, 0)

    @pytest.mark.asyncio
    async def test_logs_busy_writers(self, drain):
        drain.return_value = DrainResult(pending=3, oldest_age_seconds=10.0, busy=True)
        with (
            patch("tasks.neural_tasks.get_db", mock_get_db_factory(MagicMock())),
            structlog.testing.capture_logs() as logs,
        ):
            await drain_deferred_point_deletes_task()

        entry = next(e for e in logs if e["event"] == "deferred_point_deletes_writers_busy")
        assert entry["pending"] == 3

    @pytest.mark.asyncio
    async def test_warns_on_a_backlog_older_than_a_day(self, drain):
        drain.return_value = DrainResult(pending=1, oldest_age_seconds=90000.0, busy=True)
        with (
            patch("tasks.neural_tasks.get_db", mock_get_db_factory(MagicMock())),
            structlog.testing.capture_logs() as logs,
        ):
            await drain_deferred_point_deletes_task()

        entry = next(e for e in logs if e["event"] == "deferred_point_deletes_backlog_old")
        assert entry["log_level"] == "warning"

    @pytest.mark.asyncio
    async def test_an_empty_queue_logs_no_drain_line(self, drain):
        drain.return_value = DrainResult()
        with (
            patch("tasks.neural_tasks.get_db", mock_get_db_factory(MagicMock())),
            structlog.testing.capture_logs() as logs,
        ):
            await drain_deferred_point_deletes_task()

        assert not [e for e in logs if e["event"].startswith("deferred_point_deletes")]

    @pytest.mark.asyncio
    async def test_a_failure_does_not_escape_the_scheduler(self, drain):
        drain.side_effect = RuntimeError("db down")
        with (
            patch("tasks.neural_tasks.get_db", mock_get_db_factory(MagicMock())),
            structlog.testing.capture_logs() as logs,
        ):
            await drain_deferred_point_deletes_task()

        entry = next(e for e in logs if e["event"] == "deferred_point_deletes_task_failed")
        assert entry["error_class"] == "RuntimeError"
