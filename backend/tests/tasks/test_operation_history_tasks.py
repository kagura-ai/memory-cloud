"""Scheduling of the expired operation history purge (#2739)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from tasks import operation_history_tasks
from tasks.operation_history_tasks import schedule_operation_history_tasks
from tests.tasks.conftest import mock_get_db_factory


def test_scheduled_daily_at_0510_utc():
    scheduler = MagicMock()
    schedule_operation_history_tasks(scheduler)
    kwargs = scheduler.add_job.call_args.kwargs
    assert kwargs["id"] == "purge_operation_history"
    assert kwargs["replace_existing"] is True
    trigger = kwargs["trigger"]
    fields = {f.name: str(f) for f in trigger.fields}
    assert (fields["hour"], fields["minute"]) == ("5", "10")
    assert str(trigger.timezone) == "UTC"


def test_registered_with_the_leader_jobs():
    import tasks

    scheduler = MagicMock()
    tasks.register_scheduled_jobs(scheduler)
    ids = {c.kwargs.get("id") for c in scheduler.add_job.call_args_list}
    assert "purge_operation_history" in ids
    assert "schedule_operation_history_tasks" in tasks.__all__


def _single_flight(acquired: bool):
    @asynccontextmanager
    async def guard(_key: str):
        yield acquired

    return guard


@pytest.mark.asyncio
async def test_task_rolls_back_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    db = MagicMock()
    db.execute = AsyncMock(side_effect=RuntimeError("db down"))
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    monkeypatch.setattr(operation_history_tasks, "get_db", mock_get_db_factory(db))
    monkeypatch.setattr(operation_history_tasks, "single_flight", _single_flight(True))
    log = MagicMock()
    monkeypatch.setattr(operation_history_tasks, "logger", log)

    await operation_history_tasks.purge_operation_history_task()

    db.rollback.assert_awaited_once()
    log.exception.assert_called_once_with("operation_history_purge_failed")


@pytest.mark.asyncio
async def test_a_contended_tick_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    db = MagicMock()
    db.execute = AsyncMock()
    monkeypatch.setattr(operation_history_tasks, "get_db", mock_get_db_factory(db))
    monkeypatch.setattr(operation_history_tasks, "single_flight", _single_flight(False))

    await operation_history_tasks.purge_operation_history_task()

    db.execute.assert_not_awaited()
