"""#1969: the daily capacity purge job — single-flight and scheduling."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.capacity_purge import NoticeCounts, PurgeCounts, StampCounts, SweepCounts
from tasks import capacity_purge_tasks


def _single_flight(acquired: bool):
    @asynccontextmanager
    async def _cm(_key):
        yield acquired

    return _cm


@pytest.mark.asyncio
async def test_sweep_skips_when_another_process_holds_the_lock() -> None:
    with (
        patch.object(capacity_purge_tasks, "single_flight", _single_flight(False)),
        patch.object(capacity_purge_tasks, "run_capacity_purge_sweep", new=AsyncMock()) as run,
    ):
        counts = await capacity_purge_tasks.capacity_purge_sweep()
    assert counts == {"skipped": 1}
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_sweep_runs_once_with_a_session() -> None:
    db = MagicMock()

    async def _get_db():
        yield db

    sweep = SweepCounts(
        stamp=StampCounts(evaluated=3, stamped=1, cleared=0, kept=2),
        notices=NoticeCounts(considered=1, sent=1, already_sent=0, skipped=0, failed=0),
        purge=PurgeCounts(due=7, purged=5, skipped_within=0, failed=0, aborted=0),
    )
    with (
        patch.object(capacity_purge_tasks, "single_flight", _single_flight(True)),
        patch.object(capacity_purge_tasks, "get_db", _get_db),
        patch.object(
            capacity_purge_tasks, "run_capacity_purge_sweep", new=AsyncMock(return_value=sweep)
        ) as run,
    ):
        counts = await capacity_purge_tasks.capacity_purge_sweep()
    run.assert_awaited_once()
    assert run.call_args.args == (db,)
    assert counts["stamped"] == 1 and counts["evaluated"] == 3
    assert counts["notice_sent"] == 1
    assert (counts["purge_due"], counts["purge_purged"]) == (7, 5)


@pytest.mark.asyncio
async def test_sweep_omits_stages_that_are_flagged_off() -> None:
    async def _get_db():
        yield MagicMock()

    sweep = SweepCounts(
        stamp=StampCounts(evaluated=0, stamped=0, cleared=0, kept=0), notices=None, purge=None
    )
    with (
        patch.object(capacity_purge_tasks, "single_flight", _single_flight(True)),
        patch.object(capacity_purge_tasks, "get_db", _get_db),
        patch.object(
            capacity_purge_tasks, "run_capacity_purge_sweep", new=AsyncMock(return_value=sweep)
        ),
    ):
        counts = await capacity_purge_tasks.capacity_purge_sweep()
    assert not any(k.startswith(("notice_", "purge_")) for k in counts)


def test_schedule_registers_daily_cron_at_0400_utc() -> None:
    scheduler = MagicMock()
    capacity_purge_tasks.schedule_capacity_purge_tasks(scheduler)
    kwargs = scheduler.add_job.call_args.kwargs
    assert scheduler.add_job.call_args.args[0] is capacity_purge_tasks.capacity_purge_sweep
    assert kwargs["id"] == "capacity_purge_sweep"
    assert kwargs["replace_existing"] is True
    trigger = kwargs["trigger"]
    fields = {f.name: str(f) for f in trigger.fields}
    assert (fields["hour"], fields["minute"]) == ("4", "0")
    assert str(trigger.timezone) == "UTC"


def test_job_is_registered_with_the_app_scheduler() -> None:
    import tasks

    assert tasks.schedule_capacity_purge_tasks is capacity_purge_tasks.schedule_capacity_purge_tasks
    assert "schedule_capacity_purge_tasks" in tasks.__all__
