"""Tests for the BM25 IDF drift cron registration + task body (issue #343)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from config.settings import Settings
from tasks.bm25_drift_tasks import (
    bm25_drift_maintenance_task,
    schedule_bm25_drift_tasks,
)


def _mock_settings(enabled: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace get_settings with a controlled instance."""
    monkeypatch.setattr(
        "tasks.bm25_drift_tasks.get_settings",
        lambda: Settings(bm25_drift_cron_enabled=enabled),
    )


class TestSchedulerRegistration:
    """Sleeping-code discipline: cron must NOT register when env flag is unset."""

    def test_disabled_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _mock_settings(False, monkeypatch)
        scheduler = MagicMock()
        schedule_bm25_drift_tasks(scheduler)
        scheduler.add_job.assert_not_called()

    def test_explicit_false_does_not_register(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _mock_settings(False, monkeypatch)
        scheduler = MagicMock()
        schedule_bm25_drift_tasks(scheduler)
        scheduler.add_job.assert_not_called()

    def test_enabled_registers_cron_job(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _mock_settings(True, monkeypatch)
        monkeypatch.setenv("BM25_DRIFT_CRON_HOUR", "5")
        monkeypatch.setenv("BM25_DRIFT_CRON_MINUTE", "30")
        scheduler = MagicMock()
        schedule_bm25_drift_tasks(scheduler)
        assert scheduler.add_job.call_count == 1
        kwargs = scheduler.add_job.call_args.kwargs
        assert kwargs["id"] == "bm25_drift_maintenance"
        assert kwargs["replace_existing"] is True


class TestTaskBodyGate:
    """Defense in depth: even if registered, the task must self-skip when off."""

    @pytest.mark.asyncio
    async def test_task_skips_when_env_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _mock_settings(False, monkeypatch)
        await bm25_drift_maintenance_task()

    @pytest.mark.asyncio
    async def test_task_skips_when_env_explicit_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _mock_settings(False, monkeypatch)
        await bm25_drift_maintenance_task()
