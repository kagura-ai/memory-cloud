"""Operations status jobs (#2768): the lane snapshot and the opt-in probe."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from config.settings import Settings
from services.operations import llm_lanes, probe
from services.operations.llm_lanes import LaneSnapshot
from services.operations.probe import ProbeResult
from tasks import operations_probe_tasks as opt
from tasks.job_runs import JobFailed


def _settings(**values: Any) -> Settings:
    return Settings(_env_file=None, **values)


def _lane(lane: str, kind: str, model: str, present: bool = True) -> LaneSnapshot:
    return LaneSnapshot(
        lane=lane,  # type: ignore[arg-type]
        provider="openai",
        model=model,
        credential_present=present,
        probe_kind=kind,  # type: ignore[arg-type]
    )


LANES = [
    _lane("embedding", "embedding", "e1"),
    _lane("sleep_judge", "completion", "m1"),
    _lane("recall_selector", "completion", "m1"),
    _lane("analysis", "completion", "m2", present=False),
]


class TestSchedule:
    def test_probe_off_registers_only_the_snapshot(self, monkeypatch) -> None:
        monkeypatch.setattr(opt, "get_settings", lambda: _settings())
        scheduler = AsyncIOScheduler(timezone="UTC")
        before = datetime.now(UTC)
        opt.schedule_operations_probe_tasks(scheduler)
        jobs = {job.id: job for job in scheduler.get_jobs()}
        assert set(jobs) == {"operations_lane_snapshot"}
        snapshot = jobs["operations_lane_snapshot"]
        assert isinstance(snapshot.trigger, IntervalTrigger)
        assert snapshot.trigger.interval_length == 300
        # Fires right at scheduler start too.
        assert snapshot.next_run_time is not None and snapshot.next_run_time >= before

    def test_probe_on(self, monkeypatch) -> None:
        monkeypatch.setattr(
            opt, "get_settings", lambda: _settings(operations_probe_interval_seconds=900)
        )
        scheduler = AsyncIOScheduler(timezone="UTC")
        opt.schedule_operations_probe_tasks(scheduler)
        jobs = {job.id: job for job in scheduler.get_jobs()}
        assert set(jobs) == {"operations_lane_snapshot", "operations_probe"}
        assert jobs["operations_probe"].trigger.interval_length == 900

    def test_registered_by_register_scheduled_jobs(self, monkeypatch) -> None:
        import tasks

        monkeypatch.setattr(
            opt, "get_settings", lambda: _settings(operations_probe_interval_seconds=300)
        )
        scheduler = AsyncIOScheduler(timezone="UTC")
        tasks.register_scheduled_jobs(scheduler)
        ids = {job.id for job in scheduler.get_jobs()}
        assert {"operations_lane_snapshot", "operations_probe"} <= ids


class TestLaneSnapshotTask:
    async def test_writes_the_lanes_and_counts(self, monkeypatch) -> None:
        write = AsyncMock()
        monkeypatch.setattr(llm_lanes, "read_platform_lanes", AsyncMock(return_value=LANES))
        monkeypatch.setattr(llm_lanes, "write_lane_snapshot", write)
        assert await opt.operations_lane_snapshot_task() == {
            "lanes": 4,
            "credentials_missing": 1,
        }
        write.assert_awaited_once_with(LANES)

    async def test_failure_is_a_failed_run(self, monkeypatch) -> None:
        monkeypatch.setattr(
            llm_lanes, "read_platform_lanes", AsyncMock(side_effect=ConnectionError("db down"))
        )
        result = await opt.operations_lane_snapshot_task()
        assert isinstance(result, JobFailed)
        assert result.error_class == "ConnectionError"


class TestProbeTask:
    @pytest.fixture
    def wired(self, monkeypatch):
        monkeypatch.setattr(
            opt, "get_settings", lambda: _settings(operations_probe_interval_seconds=300)
        )
        monkeypatch.setattr(llm_lanes, "read_platform_lanes", AsyncMock(return_value=LANES))
        monkeypatch.setattr(llm_lanes, "write_lane_snapshot", AsyncMock())
        recorded: list[ProbeResult] = []

        async def _record(result: ProbeResult) -> None:
            recorded.append(result)

        monkeypatch.setattr(probe, "record_probe", _record)
        monkeypatch.setattr(probe, "ledger_probe", AsyncMock())
        return recorded

    async def test_every_target_once(self, monkeypatch, wired) -> None:
        async def _run(target, clients, settings, *, timeout):
            ok = target.model != "m2"
            return ProbeResult(
                target, ok, 3, None if ok else "AuthenticationError", datetime.now(UTC)
            )

        monkeypatch.setattr(probe, "run_probe", _run)
        counts = await opt.operations_probe_task()
        assert counts == {"targets": 3, "failed": 1, "budget_skipped": 0, "record_failed": 0}
        assert [r.target.model for r in wired] == ["e1", "m1", "m2"]
        assert wired[1].target.lanes == ("sleep_judge", "recall_selector")

    async def test_budget_skips_the_rest(self, monkeypatch, wired) -> None:
        clock = itertools.chain([0.0, 0.0], itertools.repeat(200.0))
        # Only the task's clock: the event loop keeps the real one.
        monkeypatch.setattr(opt, "time", SimpleNamespace(monotonic=lambda: next(clock)))

        async def _run(target, clients, settings, *, timeout):
            assert timeout == 20.0
            return ProbeResult(target, True, 3, None, datetime.now(UTC))

        monkeypatch.setattr(probe, "run_probe", _run)
        counts = await opt.operations_probe_task()
        assert counts["budget_skipped"] == 2
        assert len(wired) == 1

    async def test_record_failure_does_not_stop_the_run(self, monkeypatch, wired) -> None:
        monkeypatch.setattr(probe, "record_probe", AsyncMock(side_effect=OSError("db")))
        monkeypatch.setattr(
            probe,
            "run_probe",
            AsyncMock(
                side_effect=lambda t, c, s, *, timeout: ProbeResult(
                    t, True, 1, None, datetime.now(UTC)
                )
            ),
        )
        counts = await opt.operations_probe_task()
        assert counts["record_failed"] == 3

    async def test_top_level_failure_is_a_failed_run(self, monkeypatch) -> None:
        monkeypatch.setattr(llm_lanes, "read_platform_lanes", AsyncMock(side_effect=TimeoutError()))
        result = await opt.operations_probe_task()
        assert isinstance(result, JobFailed)
        assert result.error_class == "TimeoutError"
