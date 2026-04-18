"""Tests for the BM25 IDF drift cron registration + task body (issue #343)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tasks.bm25_drift_tasks import (
    bm25_drift_maintenance_task,
    schedule_bm25_drift_tasks,
)


class TestSchedulerRegistration:
    """Sleeping-code discipline: cron must NOT register when env flag is unset."""

    def test_disabled_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BM25_DRIFT_CRON_ENABLED", raising=False)
        scheduler = MagicMock()
        schedule_bm25_drift_tasks(scheduler)
        scheduler.add_job.assert_not_called()

    def test_explicit_false_does_not_register(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BM25_DRIFT_CRON_ENABLED", "false")
        scheduler = MagicMock()
        schedule_bm25_drift_tasks(scheduler)
        scheduler.add_job.assert_not_called()

    def test_enabled_registers_cron_job(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BM25_DRIFT_CRON_ENABLED", "true")
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
        monkeypatch.delenv("BM25_DRIFT_CRON_ENABLED", raising=False)
        # No DB / orchestrator imports should happen — if they did, the
        # absence of fixtures would raise. The fact that this returns
        # without error is the assertion.
        await bm25_drift_maintenance_task()

    @pytest.mark.asyncio
    async def test_task_skips_when_env_explicit_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BM25_DRIFT_CRON_ENABLED", "false")
        await bm25_drift_maintenance_task()
