"""#3034: background embedding tasks run under a process-wide limit.

Every write path (remember / remember_batch items, an in-place update, a
patch) starts its embedding through ``spawn_embedding_task``: at most
``EMBEDDING_TASK_CONCURRENCY`` of them run at once on an event loop, the rest
wait for a slot (their rows stay ``pending``, so the sweep can take them).
The sweep and the rollback re-embed call ``process_pending_embedding``
directly and never wait for a slot.
"""

from __future__ import annotations

import asyncio
import inspect
import warnings
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import structlog

from config.settings import Settings
from services import memory_service
from services.memory_service import spawn_embedding_task


@pytest.fixture
def limit(monkeypatch):
    """Set the limit for this test's event loop (read when its slots are made)."""
    memory_service._reset_embedding_tasks_for_tests()

    def _set(n: int) -> None:
        settings = Settings(_env_file=None, embedding_task_concurrency=n)
        monkeypatch.setattr("config.settings.get_settings", lambda: settings)

    yield _set
    memory_service._reset_embedding_tasks_for_tests()


class _Pipeline:
    """Stand-in for ``process_pending_embedding``: counts runs, blocks until released."""

    def __init__(self) -> None:
        self.running = 0
        self.peak = 0
        self.done: list[UUID] = []
        self.release = asyncio.Event()
        self.fail: set[UUID] = set()

    async def __call__(self, memory_id: UUID) -> None:
        self.running += 1
        self.peak = max(self.peak, self.running)
        try:
            await self.release.wait()
            if memory_id in self.fail:
                raise RuntimeError("provider down")
            self.done.append(memory_id)
        finally:
            self.running -= 1


async def _settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


async def test_at_most_n_run_while_the_rest_wait_and_all_complete(limit, monkeypatch):
    limit(2)
    pipeline = _Pipeline()
    monkeypatch.setattr(memory_service, "process_pending_embedding", pipeline)
    ids = [uuid4() for _ in range(7)]

    tasks = [spawn_embedding_task(mid) for mid in ids]
    await _settle()
    assert pipeline.running == 2
    assert len(memory_service._embedding_tasks) == 7

    pipeline.release.set()
    await asyncio.gather(*tasks)
    assert pipeline.peak == 2
    assert sorted(pipeline.done) == sorted(ids)
    assert not memory_service._embedding_tasks, "strong references are dropped when done"


async def test_a_failed_run_releases_its_slot_and_is_logged(limit, monkeypatch):
    limit(1)
    pipeline = _Pipeline()
    first, second = uuid4(), uuid4()
    pipeline.fail.add(first)
    monkeypatch.setattr(memory_service, "process_pending_embedding", pipeline)

    with structlog.testing.capture_logs() as logs:
        tasks = [spawn_embedding_task(first), spawn_embedding_task(second)]
        pipeline.release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await _settle()

    assert pipeline.done == [second]
    errors = [e for e in logs if e["event"] == "embedding_task_exception"]
    assert [e["memory_id"] for e in errors] == [str(first)]


async def test_a_task_cancelled_while_waiting_never_runs_and_leaves_no_warning(limit, monkeypatch):
    """A process that stops before a queued task gets a slot leaves its row
    pending: the pipeline never started for it."""
    limit(1)
    pipeline = _Pipeline()
    monkeypatch.setattr(memory_service, "process_pending_embedding", pipeline)
    running, waiting = uuid4(), uuid4()

    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        with structlog.testing.capture_logs() as logs:
            first = spawn_embedding_task(running)
            second = spawn_embedding_task(waiting)
            await _settle()
            second.cancel()
            await asyncio.gather(second, return_exceptions=True)
            pipeline.release.set()
            await first
            await _settle()

    assert pipeline.done == [running]
    assert pipeline.peak == 1
    assert [e["memory_id"] for e in logs if e["event"] == "embedding_task_cancelled"] == [
        str(waiting)
    ]
    assert not memory_service._embedding_tasks


async def test_the_pipeline_is_resolved_when_the_task_is_spawned(limit, monkeypatch):
    """Tests (and callers) patch the module global: the task uses the function
    in place when it was spawned, as ``create_task(process_pending_embedding(id))``
    did."""
    limit(1)
    calls: list[UUID] = []

    async def patched(memory_id: UUID) -> None:
        calls.append(memory_id)

    monkeypatch.setattr(memory_service, "process_pending_embedding", patched)
    mid = uuid4()
    task = spawn_embedding_task(mid)
    monkeypatch.undo()  # the real pipeline is back before the task runs
    await task
    assert calls == [mid]


def test_the_sweep_and_the_rollback_reembed_take_no_slot():
    """The slot is taken in the spawn wrapper only: the pipeline itself, which
    the sweep and the rollback re-embed call directly, never waits for one."""
    from services import session_rollback
    from tasks import embedding_tasks

    assert "_embedding_slots" not in inspect.getsource(memory_service.process_pending_embedding)
    assert "spawn_embedding_task" not in inspect.getsource(embedding_tasks.sweep_pending_embeddings)
    assert "spawn_embedding_task" not in inspect.getsource(session_rollback)


async def test_slots_are_per_event_loop(limit):
    limit(3)
    slots = memory_service._embedding_slots()
    assert slots is memory_service._embedding_slots()
    assert slots._value == 3  # noqa: SLF001 — asyncio.Semaphore has no public reader


def test_remember_schedules_through_the_limited_spawn(monkeypatch):
    spawned: list[UUID] = []
    monkeypatch.setattr(memory_service, "spawn_embedding_task", spawned.append)
    svc = memory_service.MemoryService.__new__(memory_service.MemoryService)
    prepared = SimpleNamespace(memory=SimpleNamespace(id=uuid4()), embedding_scheduled=False)
    svc._schedule_embedding(prepared)  # type: ignore[arg-type]
    svc._schedule_embedding(prepared)  # type: ignore[arg-type]
    assert spawned == [prepared.memory.id]


def test_no_write_path_starts_an_unlimited_embedding_task():
    """Every fire-and-forget embedding goes through ``spawn_embedding_task``."""
    source = inspect.getsource(memory_service)
    assert "create_task(process_pending_embedding(" not in source
    for name in ("_update_in_place", "patch_memory"):
        body = inspect.getsource(getattr(memory_service.MemoryService, name))
        assert "spawn_embedding_task(memory.id)" in body, name
