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


def _sweep_over(owners: list[str], limit: int, calls: list[tuple[str, str | None]]):
    """A fake sweep over sorted ``owners`` honouring ``after`` / ``until`` / ``limit``."""

    async def sweep(db, *, after: str = "", until: str | None = None, limit: int = limit):
        calls.append((after, until))
        page = [o for o in owners if o > after and (until is None or o <= until)][:limit]
        return SweepOutcome(
            owners=len(page),
            resumed=0,
            pruned=0,
            busy=0,
            failed=0,
            next_after=page[-1] if len(page) == limit else "",
        )

    return sweep


@pytest.mark.asyncio
async def test_one_run_makes_a_full_pass_in_batches(monkeypatch) -> None:
    owners = [f"owner-{i}" for i in range(5)]
    calls: list[tuple[str, str | None]] = []
    monkeypatch.setitem(workspace_slot_tasks._cursor, "after", "")
    with (
        _mode("shadow"),
        patch.object(workspace_slot_tasks, "single_flight", _single_flight(True)),
        patch.object(workspace_slot_tasks, "get_db", mock_get_db_factory(MagicMock())),
        patch.object(workspace_slot_tasks, "sweep_workspace_slots", _sweep_over(owners, 2, calls)),
    ):
        counts = await workspace_slot_tasks.workspace_slot_sweep()
    assert counts["owners"] == 5
    assert calls == [("", None), ("owner-1", None), ("owner-3", None)]
    assert workspace_slot_tasks._cursor["after"] == ""


@pytest.mark.asyncio
async def test_a_run_out_of_time_continues_and_the_next_one_wraps_once(monkeypatch) -> None:
    owners = [f"owner-{i}" for i in range(5)]
    calls: list[tuple[str, str | None]] = []
    monkeypatch.setitem(workspace_slot_tasks._cursor, "after", "")
    clock = iter([0.0, 0.0, 61.0])  # started, first check, second check: out of time
    with (
        _mode("enforce"),
        patch.object(workspace_slot_tasks, "single_flight", _single_flight(True)),
        patch.object(workspace_slot_tasks, "get_db", mock_get_db_factory(MagicMock())),
        patch.object(workspace_slot_tasks, "sweep_workspace_slots", _sweep_over(owners, 2, calls)),
        patch.object(workspace_slot_tasks, "monotonic", lambda: next(clock)),
    ):
        first = await workspace_slot_tasks.workspace_slot_sweep()
    assert (first["owners"], workspace_slot_tasks._cursor["after"]) == (2, "owner-1")
    calls.clear()
    with (
        _mode("enforce"),
        patch.object(workspace_slot_tasks, "single_flight", _single_flight(True)),
        patch.object(workspace_slot_tasks, "get_db", mock_get_db_factory(MagicMock())),
        patch.object(workspace_slot_tasks, "sweep_workspace_slots", _sweep_over(owners, 2, calls)),
    ):
        second = await workspace_slot_tasks.workspace_slot_sweep()
    # owner-2..4 after the cursor, then owner-0..1 up to it: every owner once.
    assert second["owners"] == 5
    assert calls == [("owner-1", None), ("owner-3", None), ("", "owner-1")]
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
