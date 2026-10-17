"""#2020: the startup rerank calibration task — gated start, cancel on shutdown."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from tasks import rerank_calibration as task_module
from tasks.rerank_calibration import start_rerank_calibration, stop_rerank_calibration


def _settings(base_url="http://gpu:8002", enabled=True):
    return SimpleNamespace(rerank_base_url=base_url, enable_reranking=enabled)


async def test_not_started_when_the_check_does_not_apply(monkeypatch):
    called = []

    async def fake_check(settings=None):
        called.append(settings)

    monkeypatch.setattr(task_module, "run_rerank_calibration_check", fake_check)
    assert start_rerank_calibration(_settings(enabled=False)) is None
    assert start_rerank_calibration(_settings(base_url="")) is None
    await asyncio.sleep(0)
    assert called == []


async def test_started_and_runs_the_check(monkeypatch):
    settings = _settings()
    called = []

    async def fake_check(s=None):
        called.append(s)

    monkeypatch.setattr(task_module, "run_rerank_calibration_check", fake_check)
    task = start_rerank_calibration(settings)
    assert isinstance(task, asyncio.Task)
    await task
    assert called == [settings]


async def test_stop_cancels_a_running_task(monkeypatch):
    started = asyncio.Event()

    async def slow_check(s=None):
        started.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(task_module, "run_rerank_calibration_check", slow_check)
    task = start_rerank_calibration(_settings())
    assert task is not None
    await started.wait()
    await stop_rerank_calibration(task)
    assert task.cancelled()


async def test_stop_tolerates_none_and_finished_tasks(monkeypatch):
    async def quick(s=None):
        return None

    monkeypatch.setattr(task_module, "run_rerank_calibration_check", quick)
    await stop_rerank_calibration(None)
    task = start_rerank_calibration(_settings())
    assert task is not None
    await task
    await stop_rerank_calibration(task)  # no-op, no raise
