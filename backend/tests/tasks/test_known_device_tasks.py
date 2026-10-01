"""Daily cleanup of stale known devices (#1769)."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from config.settings import Settings
from tasks import known_device_tasks
from tasks.known_device_tasks import cleanup_stale_known_devices, schedule_known_device_tasks
from tests.tasks.conftest import mock_get_db_factory
from utils.datetime import utcnow


def _settings(days: int, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "tasks.known_device_tasks.get_settings",
        lambda: Settings(_env_file=None, known_device_retention_days=days),
    )


class TestCleanupRun:
    @pytest.mark.asyncio
    async def test_cutoff_is_now_minus_retention(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _settings(180, monkeypatch)
        db = MagicMock()
        db.execute = AsyncMock(return_value=MagicMock(rowcount=4))
        now = utcnow()

        deleted = await cleanup_stale_known_devices(db, now=now)

        assert deleted == 4
        compiled = db.execute.await_args.args[0].compile()
        assert "DELETE FROM user_known_devices" in str(compiled)
        assert "last_seen <" in str(compiled)
        assert list(compiled.params.values()) == [now - timedelta(days=180)]

    @pytest.mark.asyncio
    async def test_scheduled_entrypoint_commits_and_logs_count(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _settings(180, monkeypatch)
        db = MagicMock()
        db.execute = AsyncMock(return_value=MagicMock(rowcount=7))
        db.commit = AsyncMock()
        db.rollback = AsyncMock()
        monkeypatch.setattr(known_device_tasks, "get_db", mock_get_db_factory(db))
        mock_logger = MagicMock()
        monkeypatch.setattr(known_device_tasks, "logger", mock_logger)

        await known_device_tasks.cleanup_stale_known_devices_task()

        db.commit.assert_awaited_once()
        db.rollback.assert_not_awaited()
        mock_logger.info.assert_called_once_with("known_device_cleanup_completed", deleted=7)

    @pytest.mark.asyncio
    async def test_scheduled_entrypoint_rolls_back_on_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _settings(180, monkeypatch)
        db = MagicMock()
        db.execute = AsyncMock(side_effect=RuntimeError("boom"))
        db.commit = AsyncMock()
        db.rollback = AsyncMock()
        monkeypatch.setattr(known_device_tasks, "get_db", mock_get_db_factory(db))
        monkeypatch.setattr(known_device_tasks, "logger", MagicMock())

        await known_device_tasks.cleanup_stale_known_devices_task()

        db.commit.assert_not_awaited()
        db.rollback.assert_awaited_once()


def test_schedule_registers_a_daily_job() -> None:
    scheduler = MagicMock()

    schedule_known_device_tasks(scheduler)

    scheduler.add_job.assert_called_once()
    kwargs = scheduler.add_job.call_args.kwargs
    assert kwargs["id"] == "cleanup_stale_known_devices"
    assert kwargs["replace_existing"] is True
    assert str(kwargs["trigger"]).startswith("cron[")
