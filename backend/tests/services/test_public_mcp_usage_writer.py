"""The bounded per-process writer of public MCP usage events (#2678).

A fake session factory stands in for the database: healthy, slow past the
write timeout, or raising. The request path only ever calls ``offer``.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID

import pytest

import services.public_mcp_usage_writer as writer_module
from services.public_mcp_usage_events import UsageEventInput
from services.public_mcp_usage_writer import PublicMcpUsageWriter

pytestmark = pytest.mark.asyncio(loop_scope="session")

RESOURCE = UUID("0b6f6f0e-4444-4c1d-8a2b-000000000001")


def _event(**overrides: Any) -> UsageEventInput:
    values: dict[str, Any] = {
        "endpoint_id": UUID("0b6f6f0e-1111-4c1d-8a2b-000000000001"),
        "workspace_id": UUID("0b6f6f0e-2222-4c1d-8a2b-000000000001"),
        "context_id": UUID("0b6f6f0e-3333-4c1d-8a2b-000000000001"),
        "resource_pk": RESOURCE,
        "resource_id": "catalog",
        "tool": "recall",
        "outcome": "success",
        "era": "session",
        "started_at": datetime.now(UTC),
        "duration_ms": 3,
        "doc_ids": ("doc-a", "doc-a", "doc-b"),
    }
    values.update(overrides)
    return UsageEventInput(**values)


class FakeDB:
    """Records committed batches; each write follows the next scripted behaviour."""

    def __init__(self, behaviours: list[str] | None = None, default: str = "ok") -> None:
        self.behaviours = list(behaviours or [])
        self.default = default
        self.committed: list[dict[str, list[dict]]] = []
        self.attempts = 0

    def factory(self) -> FakeSession:
        self.attempts += 1
        behaviour = self.behaviours.pop(0) if self.behaviours else self.default
        return FakeSession(self, behaviour)


class _Nested:
    async def __aenter__(self) -> _Nested:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class FakeSession:
    def __init__(self, db: FakeDB, behaviour: str) -> None:
        self.db = db
        self.behaviour = behaviour
        self.pending: dict[str, list[dict]] = {}

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def execute(self, stmt: Any, params: list[dict] | None = None) -> None:
        if self.behaviour == "slow":
            await asyncio.sleep(30)
        if self.behaviour == "raise":
            raise OSError("database unavailable")
        self.pending.setdefault(stmt.table.name, []).extend(params or [])

    def begin_nested(self) -> _Nested:
        return _Nested()

    async def commit(self) -> None:
        self.db.committed.append(self.pending)


def _writer(db: FakeDB, **overrides: Any) -> PublicMcpUsageWriter:
    values: dict[str, Any] = {
        "session_factory": db.factory,
        "queue_size": 100,
        "flush_interval": 0.02,
        "write_timeout": 0.1,
        "retry_backoff": 0.01,
        "key": "k" * 40,
        "session_details_enabled": False,
    }
    values.update(overrides)
    return PublicMcpUsageWriter(**values)


async def _until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


def _events(batch: dict[str, list[dict]]) -> list[dict]:
    return batch.get("public_mcp_usage_events", [])


async def test_healthy_batch_writes_events_and_doc_keys_in_one_transaction():
    db = FakeDB()
    writer = _writer(db)
    writer.start()
    for _ in range(3):
        writer.offer(_event())
    await _until(lambda: sum(len(_events(b)) for b in db.committed) == 3)
    await writer.stop(timeout=1)

    batch = db.committed[0]
    assert [r["kind"] for r in _events(batch)] == ["tool_call"] * len(_events(batch))
    keys = batch["public_mcp_doc_keys"]
    assert {k["resource_doc_id"] for k in keys} == {"doc-a", "doc-b"}
    assert all(k["resource_pk"] == RESOURCE for k in keys)
    # Duplicate keys within a batch are sent once.
    assert len({(k["resource_pk"], k["doc_key"]) for k in keys}) == len(keys)
    health = writer.health()
    assert health.last_flush_ok is True
    assert health.last_flush_at is not None
    assert health.dropped_since_start == 0


@pytest.mark.parametrize("failure", ["slow", "raise"])
async def test_failed_batch_is_retried_then_dropped_and_reported_as_a_gap(failure, monkeypatch):
    log = MagicMock()
    monkeypatch.setattr(writer_module, "logger", log)
    db = FakeDB(behaviours=[failure, failure])
    writer = _writer(db)
    writer.start()
    started = time.perf_counter()
    for _ in range(2):
        writer.offer(_event())
    # The request path never waits on the slow database.
    assert time.perf_counter() - started < 0.05
    await _until(lambda: writer.health().dropped_since_start == 2)
    assert writer.health().last_flush_ok is False
    assert db.attempts == 2  # one write and one retry

    writer.offer(_event())
    await _until(lambda: any(r["kind"] == "tool_call" for b in db.committed for r in _events(b)))
    await writer.stop(timeout=1)

    # The gap is written (alone, or at the head of the next batch) before
    # the event that followed it.
    rows = [r for b in db.committed for r in _events(b)]
    assert len(rows) == 2
    gap, event = rows[0], rows[1]
    assert gap["kind"] == "gap"
    assert gap["gap_reason"] == "write_failed"
    assert gap["dropped_count"] == 2
    assert gap["gap_started_at"] <= gap["gap_ended_at"]
    assert event["kind"] == "tool_call"
    events = [c.args[0] for c in log.warning.call_args_list]
    assert "public_mcp_telemetry_write_failed" in events
    failed = next(
        c for c in log.warning.call_args_list if c.args[0] == "public_mcp_telemetry_write_failed"
    )
    assert set(failed.kwargs) <= {"error_type", "dropped", "attempts"}


async def test_queue_full_is_a_gap_row_before_the_events_that_followed():
    db = FakeDB()
    writer = _writer(db, queue_size=2)
    for _ in range(5):
        writer.offer(_event())  # not started: 2 queued, 3 dropped
    assert writer.health().queue_depth == 2
    assert writer.health().dropped_since_start == 3
    assert writer.health().pending_gap == 3
    writer.start()
    await _until(lambda: len(db.committed) >= 1)
    await writer.stop(timeout=1)
    rows = _events(db.committed[0])
    assert rows[0]["kind"] == "gap"
    assert rows[0]["gap_reason"] == "queue_full"
    assert rows[0]["dropped_count"] == 3
    assert [r["kind"] for r in rows[1:]] == ["tool_call", "tool_call"]
    assert writer.health().pending_gap == 0


async def test_offer_is_fast_and_never_raises(monkeypatch):
    db = FakeDB(default="slow")
    writer = _writer(db, queue_size=100)
    worst = 0.0
    for _ in range(500):  # 100 queued, the rest dropped
        started = time.perf_counter()
        writer.offer(_event(doc_ids=tuple(f"d{i}" for i in range(25))))
        worst = max(worst, time.perf_counter() - started)
    assert worst < 0.005

    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("builder bug")

    monkeypatch.setattr(writer_module, "build_usage_event_row", boom)
    writer.offer(_event())  # does not raise
    assert writer.health().dropped_since_start == 401


async def test_stop_flushes_the_queue():
    db = FakeDB()
    writer = _writer(db, flush_interval=5.0)
    writer.start()
    for _ in range(3):
        writer.offer(_event())
    await writer.stop(timeout=2)
    assert sum(len(_events(b)) for b in db.committed) == 3
    assert writer.health().queue_depth == 0


async def test_stop_with_a_dead_database_logs_the_loss(monkeypatch):
    log = MagicMock()
    monkeypatch.setattr(writer_module, "logger", log)
    db = FakeDB(default="slow")
    writer = _writer(db, write_timeout=5.0)
    writer.start()
    for _ in range(4):
        writer.offer(_event())
    await writer.stop(timeout=0.3)
    lost = [
        c
        for c in log.warning.call_args_list
        if c.args[0] == "public_mcp_telemetry_lost_at_shutdown"
    ]
    assert len(lost) == 1
    assert lost[0].kwargs == {"count": 4}
    assert db.committed == []


async def test_shutdown_failure_becomes_a_shutdown_gap(monkeypatch):
    db = FakeDB(behaviours=["raise", "raise"])
    writer = _writer(db, flush_interval=5.0)
    writer.start()
    writer.offer(_event())
    await writer.stop(timeout=2)
    rows = [r for b in db.committed for r in _events(b)]
    assert len(rows) == 1
    assert rows[0]["kind"] == "gap"
    assert rows[0]["gap_reason"] == "shutdown"
    assert rows[0]["dropped_count"] == 1


async def test_disabled_switch_starts_no_writer(monkeypatch):
    from config.settings import get_settings

    monkeypatch.setattr(get_settings(), "mcp_public_telemetry_enabled", False)
    assert writer_module.start_usage_writer() is None
    assert writer_module.get_usage_writer() is None


async def test_enabled_switch_starts_one_writer(monkeypatch):
    from config.settings import get_settings

    monkeypatch.setattr(get_settings(), "mcp_public_telemetry_enabled", True)
    db = FakeDB()
    writer = writer_module.start_usage_writer(session_factory=db.factory)
    try:
        assert writer is not None
        assert writer_module.get_usage_writer() is writer
        assert writer.health().enabled is True
        assert writer.health().queue_capacity == get_settings().mcp_public_telemetry_queue_size
    finally:
        await writer_module.stop_usage_writer(timeout=1)
    assert writer_module.get_usage_writer() is None
