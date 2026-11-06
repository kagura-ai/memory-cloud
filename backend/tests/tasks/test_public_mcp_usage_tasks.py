"""Scheduling of the public MCP usage event purge (#2678)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tasks import public_mcp_usage_tasks
from tasks.public_mcp_usage_tasks import schedule_public_mcp_usage_tasks
from tests.tasks.conftest import mock_get_db_factory


def test_scheduled_daily_at_0450_utc():
    scheduler = MagicMock()
    schedule_public_mcp_usage_tasks(scheduler)
    kwargs = scheduler.add_job.call_args.kwargs
    assert kwargs["id"] == "purge_public_mcp_usage_events"
    assert kwargs["replace_existing"] is True
    fields = {f.name: str(f) for f in kwargs["trigger"].fields}
    assert fields["hour"] == "4"
    assert fields["minute"] == "50"


def test_registered_with_the_leader_jobs():
    import tasks

    scheduler = MagicMock()
    tasks.register_scheduled_jobs(scheduler)
    ids = {c.kwargs.get("id") for c in scheduler.add_job.call_args_list}
    assert "purge_public_mcp_usage_events" in ids
    assert "schedule_public_mcp_usage_tasks" in tasks.__all__


@pytest.mark.asyncio
async def test_task_rolls_back_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    db = MagicMock()
    db.execute = AsyncMock(side_effect=RuntimeError("db down"))
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    monkeypatch.setattr(public_mcp_usage_tasks, "get_db", mock_get_db_factory(db))
    log = MagicMock()
    monkeypatch.setattr(public_mcp_usage_tasks, "logger", log)

    await public_mcp_usage_tasks.purge_public_mcp_usage_events_task()

    db.rollback.assert_awaited_once()
    log.exception.assert_called_once_with("public_mcp_usage_purge_failed")
