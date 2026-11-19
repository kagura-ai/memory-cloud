"""#2918: the workspace slot sweep job: mode, single-flight, cursor, scheduling."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.workspace_slots import SweepOutcome
from tasks import workspace_slot_tasks
from tasks.job_runs import JobFailed
from tests.tasks.conftest import mock_get_db_factory


def _mode(mode: str):
    return patch(
        "config.settings.get_settings",
        return_value=SimpleNamespace(workspace_slot_suspension=mode),
    )


def _single_flight(acquired: bool):
    @asynccontextmanager
    async def _cm(_key):
        yield acquired

    return _cm


@pytest.mark.asyncio
async def test_off_does_nothing() -> None:
    with (
        _mode("off"),
        patch.object(workspace_slot_tasks, "sweep_workspace_slots", new=AsyncMock()) as sweep,
    ):
        assert await workspace_slot_tasks.workspace_slot_sweep() == {"skipped": 1}
    sweep.assert_not_awaited()


@pytest.mark.asyncio
async def test_skips_when_another_process_holds_the_lock() -> None:
    with (
        _mode("enforce"),
        patch.object(workspace_slot_tasks, "single_flight", _single_flight(False)),
        patch.object(workspace_slot_tasks, "sweep_workspace_slots", new=AsyncMock()) as sweep,
    ):
        assert await workspace_slot_tasks.workspace_slot_sweep() == {"skipped": 1}
    sweep.assert_not_awaited()


@pytest.mark.asyncio
async def test_runs_one_batch_and_continues_after_the_cursor(monkeypatch) -> None:
    db = MagicMock()
    monkeypatch.setitem(workspace_slot_tasks._cursor, "after", "")
    outcomes = [
        SweepOutcome(owners=2, resumed=1, pruned=1, busy=0, failed=0, next_after="owner-b"),
        SweepOutcome(owners=1, resumed=0, pruned=0, busy=1, failed=0, next_after=""),
    ]
    sweep = AsyncMock(side_effect=outcomes)
    with (
        _mode("shadow"),
        patch.object(workspace_slot_tasks, "single_flight", _single_flight(True)),
        patch.object(workspace_slot_tasks, "get_db", mock_get_db_factory(db)),
        patch.object(workspace_slot_tasks, "sweep_workspace_slots", new=sweep),
    ):
        first = await workspace_slot_tasks.workspace_slot_sweep()
        second = await workspace_slot_tasks.workspace_slot_sweep()
    assert first == {"owners": 2, "resumed": 1, "pruned": 1, "busy": 0, "failed": 0}
    assert second["busy"] == 1
    assert [c.kwargs["after"] for c in sweep.await_args_list] == ["", "owner-b"]
    assert workspace_slot_tasks._cursor["after"] == ""


@pytest.mark.asyncio
async def test_a_failure_is_a_failed_run() -> None:
    with (
        _mode("enforce"),
        patch.object(workspace_slot_tasks, "single_flight", _single_flight(True)),
        patch.object(workspace_slot_tasks, "get_db", mock_get_db_factory(MagicMock())),
        patch.object(
            workspace_slot_tasks,
            "sweep_workspace_slots",
            new=AsyncMock(side_effect=ConnectionError("db down")),
        ),
    ):
        result = await workspace_slot_tasks.workspace_slot_sweep()
    assert isinstance(result, JobFailed)


@pytest.mark.parametrize(
    ("mode", "registered"), [("off", False), ("shadow", True), ("enforce", True)]
)
def test_registered_every_15_minutes_unless_off(mode: str, registered: bool) -> None:
    scheduler = MagicMock()
    with _mode(mode):
        workspace_slot_tasks.schedule_workspace_slot_tasks(scheduler)
    assert scheduler.add_job.called is registered
    if registered:
        kwargs = scheduler.add_job.call_args.kwargs
        assert kwargs["id"] == "workspace_slot_sweep"
        assert kwargs["trigger"].interval.total_seconds() == 15 * 60
