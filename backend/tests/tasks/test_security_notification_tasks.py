"""Every-minute security notice digest job (Issue #1752)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from apscheduler.triggers.cron import CronTrigger

from tasks import schedule_security_notification_tasks, security_notification_tasks


class TestSchedulerRegistration:
    def test_registers_every_minute_job(self) -> None:
        scheduler = MagicMock()
        schedule_security_notification_tasks(scheduler)

        assert scheduler.add_job.call_count == 1
        args, kwargs = scheduler.add_job.call_args
        assert args[0] is security_notification_tasks.flush_security_notifications_task
        assert kwargs["id"] == "flush_security_notifications"
        assert kwargs["replace_existing"] is True
        trigger = kwargs["trigger"]
        assert isinstance(trigger, CronTrigger)
        assert str(trigger.fields[trigger.FIELD_NAMES.index("minute")]) == "*"

    def test_registered_at_app_startup(self) -> None:
        main_src = (Path(__file__).resolve().parents[2] / "src" / "api" / "main.py").read_text()
        assert "schedule_security_notification_tasks(scheduler)" in main_src


class TestTask:
    @pytest.mark.asyncio
    async def test_task_runs_the_flush(self, monkeypatch) -> None:
        flush = AsyncMock(return_value=2)
        monkeypatch.setattr(security_notification_tasks, "flush_due_security_notifications", flush)
        await security_notification_tasks.flush_security_notifications_task()
        flush.assert_awaited_once_with()
