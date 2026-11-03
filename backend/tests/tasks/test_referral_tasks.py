"""#2563: the sweep that applies pending referral grants — scheduled, bounded, switchable."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import structlog

from tasks import referral_tasks
from tasks.referral_tasks import (
    PendingGrantSweep,
    apply_pending_referral_grants_task,
    schedule_referral_tasks,
    sweep_pending_referral_grants,
)
from tests.tasks.conftest import mock_get_db_factory
from utils.exceptions import ConflictError

_INTERVAL = "REFERRAL_PENDING_GRANT_SWEEP_INTERVAL_SECONDS"
_JOB_ID = "apply_pending_referral_grants"


def _jobs(scheduler):
    return {call.kwargs["id"]: call for call in scheduler.add_job.call_args_list}


class TestScheduling:
    def test_scheduled_every_five_minutes_by_default(self, monkeypatch):
        monkeypatch.delenv(_INTERVAL, raising=False)
        scheduler = MagicMock()

        schedule_referral_tasks(scheduler)

        job = _jobs(scheduler)[_JOB_ID]
        assert job.args[0] is apply_pending_referral_grants_task
        assert job.kwargs["trigger"].interval.total_seconds() == 300
        assert job.kwargs["replace_existing"] is True

    def test_the_interval_is_read_at_schedule_time(self, monkeypatch):
        monkeypatch.setenv(_INTERVAL, "60")
        scheduler = MagicMock()

        schedule_referral_tasks(scheduler)

        assert _jobs(scheduler)[_JOB_ID].kwargs["trigger"].interval.total_seconds() == 60

    def test_a_fraction_of_a_second_still_schedules_the_job(self, monkeypatch):
        monkeypatch.setenv(_INTERVAL, "0.5")
        scheduler = MagicMock()

        schedule_referral_tasks(scheduler)

        assert _jobs(scheduler)[_JOB_ID].kwargs["trigger"].interval.total_seconds() == 1

    def test_zero_turns_the_job_off(self, monkeypatch):
        monkeypatch.setenv(_INTERVAL, "0")
        scheduler = MagicMock()

        schedule_referral_tasks(scheduler)

        assert _JOB_ID not in _jobs(scheduler)

    @pytest.mark.parametrize("value", ["abc", "-5", "nan", "inf"])
    def test_a_bad_interval_falls_back_to_the_default(self, monkeypatch, value):
        monkeypatch.setenv(_INTERVAL, value)
        scheduler = MagicMock()

        schedule_referral_tasks(scheduler)

        assert _jobs(scheduler)[_JOB_ID].kwargs["trigger"].interval.total_seconds() == 300

    def test_registered_at_app_startup(self) -> None:
        # The lifespan's scheduler leader (#1980) registers jobs through
        # ``register_scheduled_jobs``; it must include this family.
        import tasks

        scheduler = MagicMock()
        with patch.object(tasks, "schedule_referral_tasks") as schedule:
            tasks.register_scheduled_jobs(scheduler)
        schedule.assert_called_once_with(scheduler)


def _db() -> MagicMock:
    db = MagicMock()
    db.rollback = AsyncMock()
    return db


@pytest.fixture
def service():
    """``ReferralService`` as the sweep sees it: four candidates, applied as
    2 grants, a busy workspace, an unexpected error, and nothing (another
    process applied them first)."""
    svc = MagicMock()
    svc.users_with_pending_grants = AsyncMock(return_value=["u_a", "u_b", "u_c", "u_d"])
    svc.apply_pending_grants = AsyncMock(
        side_effect=[2, ConflictError("busy"), RuntimeError("boom"), 0]
    )
    with patch.object(referral_tasks, "ReferralService", return_value=svc):
        yield svc


class TestSweep:
    @pytest.mark.asyncio
    async def test_applies_each_selected_user_and_counts_the_outcomes(self, service):
        result = await sweep_pending_referral_grants(_db(), limit=4)

        service.users_with_pending_grants.assert_awaited_once_with(limit=4)
        assert [c.args for c in service.apply_pending_grants.await_args_list] == [
            ("u_a",),
            ("u_b",),
            ("u_c",),
            ("u_d",),
        ]
        assert result == PendingGrantSweep(
            candidates=4, users_applied=1, grants_applied=2, busy=1, failed=1
        )

    @pytest.mark.asyncio
    async def test_selects_a_hundred_users_by_default(self, service):
        await sweep_pending_referral_grants(_db())

        service.users_with_pending_grants.assert_awaited_once_with(limit=100)

    @pytest.mark.asyncio
    async def test_every_user_runs_in_a_transaction_of_its_own(self, service):
        """The selection's transaction ends before the first apply, and each
        apply's transaction ends before the next — a busy or failed one is
        rolled back, so the next user starts with nothing locked."""
        db = _db()
        order: list[str] = []
        db.rollback.side_effect = lambda: order.append("rollback")
        service.users_with_pending_grants.side_effect = lambda **_: (
            order.append("select") or ["u_a", "u_b", "u_c", "u_d"]
        )
        outcomes = iter([2, ConflictError("busy"), RuntimeError("boom"), 0])

        async def apply(user_id: str) -> int:
            order.append(user_id)
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        service.apply_pending_grants.side_effect = apply

        await sweep_pending_referral_grants(db)

        assert order == [
            "select",
            "rollback",
            "u_a",
            "rollback",
            "u_b",
            "rollback",
            "u_c",
            "rollback",
            "u_d",
            "rollback",
        ]

    @pytest.mark.asyncio
    async def test_a_busy_user_is_logged_at_info_and_a_failed_one_as_a_warning(self, service):
        with structlog.testing.capture_logs() as logs:
            await sweep_pending_referral_grants(_db())

        busy = next(e for e in logs if e["event"] == "referral_pending_grant_sweep_busy")
        assert (busy["user_id"], busy["log_level"]) == ("u_b", "info")
        failed = next(e for e in logs if e["event"] == "referral_pending_grant_sweep_user_failed")
        assert (failed["user_id"], failed["log_level"]) == ("u_c", "warning")
        assert failed["error_class"] == "RuntimeError"

    @pytest.mark.asyncio
    async def test_no_candidates_applies_nothing(self, service):
        service.users_with_pending_grants.return_value = []

        result = await sweep_pending_referral_grants(_db())

        service.apply_pending_grants.assert_not_awaited()
        assert result == PendingGrantSweep()


class TestTask:
    @pytest.mark.asyncio
    async def test_logs_what_the_run_did(self):
        sweep = AsyncMock(
            return_value=PendingGrantSweep(
                candidates=3, users_applied=1, grants_applied=2, busy=1, failed=1
            )
        )
        db = _db()
        with (
            patch.object(referral_tasks, "get_db", mock_get_db_factory(db)),
            patch.object(referral_tasks, "sweep_pending_referral_grants", sweep),
            structlog.testing.capture_logs() as logs,
        ):
            await apply_pending_referral_grants_task()

        sweep.assert_awaited_once_with(db)
        entry = next(e for e in logs if e["event"] == "referral_pending_grant_sweep_done")
        assert (
            entry["candidates"],
            entry["users_applied"],
            entry["grants_applied"],
            entry["busy"],
            entry["failed"],
        ) == (3, 1, 2, 1, 1)

    @pytest.mark.asyncio
    async def test_an_empty_run_logs_nothing(self):
        with (
            patch.object(referral_tasks, "get_db", mock_get_db_factory(_db())),
            patch.object(
                referral_tasks,
                "sweep_pending_referral_grants",
                AsyncMock(return_value=PendingGrantSweep()),
            ),
            structlog.testing.capture_logs() as logs,
        ):
            await apply_pending_referral_grants_task()

        assert not [e for e in logs if e["event"].startswith("referral_pending_grant_sweep")]

    @pytest.mark.asyncio
    async def test_a_failure_does_not_escape_the_scheduler(self):
        with (
            patch.object(referral_tasks, "get_db", mock_get_db_factory(_db())),
            patch.object(
                referral_tasks,
                "sweep_pending_referral_grants",
                AsyncMock(side_effect=RuntimeError("db down")),
            ),
            structlog.testing.capture_logs() as logs,
        ):
            await apply_pending_referral_grants_task()

        entry = next(e for e in logs if e["event"] == "referral_pending_grant_sweep_failed")
        assert (entry["error_class"], entry["log_level"]) == ("RuntimeError", "error")
