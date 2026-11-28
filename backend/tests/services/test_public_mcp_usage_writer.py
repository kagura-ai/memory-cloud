"""The bounded per-process writer of public MCP usage events (#2678).

A fake session factory stands in for the database: healthy, slow past the
write timeout, or raising. The request path only ever calls ``offer``.
"""

from __future__ import annotations

import asyncio
import gc
import time
from datetime import UTC, datetime, timedelta
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
        # #3053: the statements of each committed transaction, in order.
        self.logs: list[list[str]] = []
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
        self.log: list[str] = []

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def execute(self, stmt: Any, params: Any = None) -> None:
        if self.behaviour == "slow":
            await asyncio.sleep(30)
        if self.behaviour == "raise":
            raise OSError("database unavailable")
        name = _table(stmt)
        if name is None:
            # A text statement (#3053: the day locks and the day counts).
            self.log.append(str(stmt))
            self.pending.setdefault("sql", []).append({"sql": str(stmt), "params": params})
            return
        self.log.append(name)
        self.pending.setdefault(name, []).extend(params or [])

    def begin_nested(self) -> _Nested:
        return _Nested()

    async def commit(self) -> None:
        self.db.committed.append(self.pending)
        self.db.logs.append(self.log)


def _table(stmt: Any) -> str | None:
    """The table an INSERT writes, or None for a text statement."""
    table = getattr(stmt, "table", None)
    return getattr(table, "name", None)


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
    # The threshold measures offer() itself. A full garbage collection late in
    # the suite, with coverage on, can pause one call for over a second on CI
    # (seen twice on PR #2777, #2780), so collect first and keep the collector
    # off while timing.
    gc.collect()
    gc.disable()
    try:
        for _ in range(500):  # 100 queued, the rest dropped
            started = time.perf_counter()
            writer.offer(_event(doc_ids=tuple(f"d{i}" for i in range(25))))
            worst = max(worst, time.perf_counter() - started)
    finally:
        gc.enable()
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


# ---------------------------------------------------------------------------
# #3035: daily visitors, written apart from the events and in crowds
# ---------------------------------------------------------------------------

DAY_NOON = datetime(2026, 11, 6, 12, 0, 0)
ENDPOINT_2 = UUID("0b6f6f0e-1111-4c1d-8a2b-000000000002")
KEY = "k" * 40


def _visit(value: str, *, endpoint: UUID | None = None, started=None, **overrides: Any):
    from services.public_mcp_usage_events import visitor_key

    endpoint = endpoint or UUID("0b6f6f0e-1111-4c1d-8a2b-000000000001")
    started = started or DAY_NOON.replace(tzinfo=UTC)
    values: dict[str, Any] = {
        "endpoint_id": endpoint,
        "route": "rest",
        "caller": "bound_key",
        "era": "stateless",
        "started_at": started,
        "visitor_key": visitor_key(endpoint, value, started.date(), KEY),
    }
    values.update(overrides)
    return _event(**values)


def _visitor_batches(db: FakeDB) -> list[list[dict]]:
    return [
        b["public_mcp_daily_visitors"] for b in db.committed if "public_mcp_daily_visitors" in b
    ]


@pytest.fixture
def writer_today(monkeypatch):
    """The writer's clock: noon of DAY_NOON's day (the visitors' own day)."""
    now = {"value": DAY_NOON}
    monkeypatch.setattr(writer_module, "utcnow", lambda: now["value"])
    return now


async def _passes(writer: PublicMcpUsageWriter, count: int = 8) -> None:
    """Let the drainer run several passes (each at most two flush intervals)."""
    await asyncio.sleep(writer._flush_interval * 2 * count)


async def test_nine_visitors_of_one_endpoint_and_day_wait(writer_today):
    db = FakeDB()
    writer = _writer(db)
    writer.start()
    for i in range(9):
        writer.offer(_visit(f"v{i}"))
    await _until(lambda: sum(len(_events(b)) for b in db.committed) == 9)
    await _passes(writer)
    assert _visitor_batches(db) == []

    writer.offer(_visit("v9"))
    await _until(lambda: len(_visitor_batches(db)) == 1)
    await writer.stop(timeout=1)
    (batch,) = _visitor_batches(db)
    assert len(batch) == 10
    assert {r["endpoint_id"] for r in batch} == {_visit("v0").endpoint_id}
    assert {r["day"] for r in batch} == {DAY_NOON.date()}
    assert set(batch[0]) == {"endpoint_id", "day", "visitor_key", "workspace_id"}


async def test_the_visitor_transaction_writes_no_event_row(writer_today):
    db = FakeDB()
    writer = _writer(db)
    writer.start()
    for i in range(10):
        writer.offer(_visit(f"v{i}"))
    await _until(lambda: len(_visitor_batches(db)) == 1)
    await writer.stop(timeout=1)
    for batch in db.committed:
        if "public_mcp_daily_visitors" in batch:
            assert set(batch) == {"public_mcp_daily_visitors"}
        else:
            assert "public_mcp_daily_visitors" not in batch


async def test_other_endpoints_do_not_complete_a_crowd(writer_today):
    db = FakeDB()
    writer = _writer(db)
    writer.start()
    for i in range(9):
        writer.offer(_visit(f"a{i}"))
        writer.offer(_visit(f"b{i}", endpoint=ENDPOINT_2))
    await _until(lambda: sum(len(_events(b)) for b in db.committed) == 18)
    await _passes(writer)
    assert _visitor_batches(db) == []
    await writer.stop(timeout=1)


async def test_a_repeated_value_counts_once(writer_today):
    db = FakeDB()
    writer = _writer(db)
    writer.start()
    for i in range(9):
        writer.offer(_visit(f"v{i}"))
    for _ in range(5):
        writer.offer(_visit("v0"))
    await _until(lambda: sum(len(_events(b)) for b in db.committed) == 14)
    await _passes(writer)
    assert _visitor_batches(db) == []
    await writer.stop(timeout=1)
    assert len(_visitor_batches(db)[0]) == 9


async def test_a_finished_day_waits_out_its_late_searches_then_is_written_once(writer_today):
    """A search that started before midnight and answered after it joins its day's last group."""
    assert timedelta(minutes=5) <= writer_module.VISITOR_DAY_GRACE <= timedelta(hours=1)
    db = FakeDB()
    writer = _writer(db)
    writer.start()
    for i in range(3):
        writer.offer(_visit(f"v{i}"))
    await _until(lambda: sum(len(_events(b)) for b in db.committed) == 3)
    await _passes(writer, 3)
    assert _visitor_batches(db) == []

    next_day = datetime(2026, 11, 7)
    writer_today["value"] = next_day + timedelta(seconds=1)
    await _passes(writer, 3)
    assert _visitor_batches(db) == [], "the day's group waits after midnight"
    straddler = datetime(2026, 11, 6, 23, 59, 59, 800000, tzinfo=UTC)
    writer.offer(_visit("straddler", started=straddler))
    await _until(lambda: sum(len(_events(b)) for b in db.committed) == 4)
    await _passes(writer, 3)
    assert _visitor_batches(db) == [], "a late search does not open a group of its own"

    writer_today["value"] = next_day + writer_module.VISITOR_DAY_GRACE - timedelta(seconds=1)
    await _passes(writer, 3)
    assert _visitor_batches(db) == []
    writer_today["value"] = next_day + writer_module.VISITOR_DAY_GRACE
    await _until(lambda: len(_visitor_batches(db)) == 1)
    await writer.stop(timeout=1)
    (batch,) = _visitor_batches(db)
    assert len(batch) == 4
    assert {r["day"] for r in batch} == {DAY_NOON.date()}


async def test_a_visitor_of_a_day_already_written_is_dropped_not_written_alone(
    writer_today, monkeypatch
):
    log = MagicMock()
    monkeypatch.setattr(writer_module, "logger", log)
    db = FakeDB()
    writer = _writer(db)
    writer.start()
    writer.offer(_visit("v0"))
    await _until(lambda: sum(len(_events(b)) for b in db.committed) == 1)
    next_day = datetime(2026, 11, 7)
    writer_today["value"] = next_day + writer_module.VISITOR_DAY_GRACE
    await _until(lambda: len(_visitor_batches(db)) == 1)

    # A search of that day answering after its group was due: its event is
    # stored, its visitor is dropped and counted.
    writer.offer(_visit("very-late", started=datetime(2026, 11, 6, 23, 59, tzinfo=UTC)))
    await _until(lambda: sum(len(_events(b)) for b in db.committed) == 2)
    await _passes(writer, 3)
    await writer.stop(timeout=1)
    assert [len(b) for b in _visitor_batches(db)] == [1]
    assert writer._visitors_dropped == 1
    dropped = [c for c in log.warning.call_args_list if c.args[0] == "public_mcp_visitors_dropped"]
    assert [c.kwargs["reason"] for c in dropped] == ["late"]
    # The current day's visitors are unaffected.
    assert writer._visitors_pending == 0


async def test_stop_writes_every_group_whatever_its_size(writer_today):
    db = FakeDB()
    writer = _writer(db)
    writer.start()
    writer.offer(_visit("one"))
    writer.offer(_visit("two", endpoint=ENDPOINT_2))
    await _until(lambda: sum(len(_events(b)) for b in db.committed) == 2)
    await writer.stop(timeout=1)
    rows = [r for b in _visitor_batches(db) for r in b]
    assert len(rows) == 2


async def test_rows_are_sorted_by_endpoint_day_and_key(writer_today):
    db = FakeDB()
    writer = _writer(db, visitor_flush_min_keys=1)
    for i in range(6):
        writer.offer(_visit(f"v{i}", endpoint=ENDPOINT_2))
        writer.offer(_visit(f"w{i}"))
    writer.start()
    await writer.stop(timeout=1)
    rows = [r for b in _visitor_batches(db) for r in b]
    assert len(rows) == 12
    for batch in _visitor_batches(db):
        keys = [(r["endpoint_id"], r["day"], r["visitor_key"]) for r in batch]
        assert keys == sorted(keys)


async def test_the_threshold_is_a_constructor_parameter(writer_today):
    assert writer_module.VISITOR_FLUSH_MIN_KEYS == 10
    db = FakeDB()
    writer = _writer(db, visitor_flush_min_keys=2)
    writer.start()
    writer.offer(_visit("v0"))
    await _until(lambda: sum(len(_events(b)) for b in db.committed) == 1)
    await _passes(writer, 3)
    assert _visitor_batches(db) == []
    writer.offer(_visit("v1"))
    await _until(lambda: len(_visitor_batches(db)) == 1)
    await writer.stop(timeout=1)
    assert len(_visitor_batches(db)[0]) == 2


async def test_a_visitor_is_pending_only_after_its_event_was_queued(writer_today, monkeypatch):
    db = FakeDB()
    writer = _writer(db, queue_size=1)
    writer.offer(_visit("queued"))
    writer.offer(_visit("dropped-queue-full"))
    assert writer.health().dropped_since_start == 1

    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("builder bug")

    with monkeypatch.context() as m:
        m.setattr(writer_module, "build_usage_event_row", boom)
        writer.offer(_visit("dropped-build-failed"))
    writer.start()
    await writer.stop(timeout=1)
    rows = [r for b in _visitor_batches(db) for r in b]
    from services.public_mcp_usage_events import visitor_key

    assert [r["visitor_key"] for r in rows] == [
        visitor_key(_visit("x").endpoint_id, "queued", DAY_NOON.date(), KEY)
    ]


async def test_an_anonymous_or_refused_call_adds_no_visitor(writer_today):
    db = FakeDB()
    writer = _writer(db, visitor_flush_min_keys=1)
    writer.offer(_visit("anon", caller="anonymous"))
    writer.offer(_visit("refused", outcome="rate_limited", error_code="rate_limit_exceeded"))
    writer.start()
    await writer.stop(timeout=1)
    assert _visitor_batches(db) == []
    assert sum(len(_events(b)) for b in db.committed) == 2


async def test_a_failed_visitor_write_is_retried_once_then_dropped(writer_today, monkeypatch):
    log = MagicMock()
    monkeypatch.setattr(writer_module, "logger", log)

    class VisitorFailDB(FakeDB):
        def factory(self) -> FakeSession:
            self.attempts += 1
            return _VisitorFailSession(self, "ok")

    class _VisitorFailSession(FakeSession):
        async def execute(self, stmt: Any, params: Any = None) -> None:
            if _table(stmt) == "public_mcp_daily_visitors":
                raise OSError("database unavailable")
            await super().execute(stmt, params)

    db = VisitorFailDB()
    writer = _writer(db, visitor_flush_min_keys=2)
    writer.start()
    writer.offer(_visit("v0"))
    writer.offer(_visit("v1"))
    await _until(
        lambda: any(
            c.args[0] == "public_mcp_visitors_write_failed" for c in log.warning.call_args_list
        )
    )
    # Events keep flowing.
    writer.offer(_event())
    await _until(lambda: sum(len(_events(b)) for b in db.committed) == 3)
    await writer.stop(timeout=1)
    failed = next(
        c for c in log.warning.call_args_list if c.args[0] == "public_mcp_visitors_write_failed"
    )
    assert failed.kwargs == {"error_type": "OSError", "dropped": 2}
    assert _visitor_batches(db) == []


async def test_visitors_lost_at_shutdown_are_counted(writer_today, monkeypatch):
    log = MagicMock()
    monkeypatch.setattr(writer_module, "logger", log)
    db = FakeDB(default="slow")
    writer = _writer(db, write_timeout=5.0)
    writer.offer(_visit("v0"))
    writer.offer(_visit("v1"))
    writer.start()
    await writer.stop(timeout=0.3)
    lost = [
        c
        for c in log.warning.call_args_list
        if c.args[0] == "public_mcp_telemetry_lost_at_shutdown"
    ]
    assert len(lost) == 1
    assert lost[0].kwargs == {"count": 2, "visitors": 2}


async def test_a_visitor_write_cancelled_at_shutdown_is_counted(writer_today, monkeypatch):
    """The events were stored; the final visitor write hangs until ``stop`` cancels it."""
    log = MagicMock()
    monkeypatch.setattr(writer_module, "logger", log)

    class _HangingVisitorSession(FakeSession):
        async def execute(self, stmt: Any, params: Any = None) -> None:
            if _table(stmt) == "public_mcp_daily_visitors":
                await asyncio.sleep(30)
            await super().execute(stmt, params)

    class HangingVisitorDB(FakeDB):
        def factory(self) -> FakeSession:
            self.attempts += 1
            return _HangingVisitorSession(self, "ok")

    db = HangingVisitorDB()
    writer = _writer(db, write_timeout=5.0)
    writer.start()
    for i in range(4):
        writer.offer(_visit(f"v{i}"))
    await _until(lambda: sum(len(_events(b)) for b in db.committed) == 4)
    await writer.stop(timeout=0.3)
    lost = [
        c
        for c in log.warning.call_args_list
        if c.args[0] == "public_mcp_telemetry_lost_at_shutdown"
    ]
    assert len(lost) == 1
    assert lost[0].kwargs == {"count": 0, "visitors": 4}
    assert _visitor_batches(db) == []


async def test_the_pending_set_is_bounded(writer_today, monkeypatch):
    log = MagicMock()
    monkeypatch.setattr(writer_module, "logger", log)
    monkeypatch.setattr(writer_module, "VISITOR_PENDING_MAX", 3)
    db = FakeDB()
    writer = _writer(db, visitor_flush_min_keys=100)
    for i in range(5):
        writer.offer(_visit(f"v{i}"))
    writer.start()
    await writer.stop(timeout=1)
    rows = [r for b in _visitor_batches(db) for r in b]
    assert len(rows) == 3
    dropped = [c for c in log.warning.call_args_list if c.args[0] == "public_mcp_visitors_dropped"]
    assert dropped and dropped[0].kwargs["reason"] == "pending_full"


async def test_doc_key_rows_are_sorted():
    from services.public_mcp_usage_events import BuiltUsageEvent
    from services.public_mcp_usage_writer import _doc_key_rows

    a = UUID("0b6f6f0e-4444-4c1d-8a2b-00000000000a")
    b = UUID("0b6f6f0e-4444-4c1d-8a2b-000000000002")
    batch = [
        BuiltUsageEvent(row={}, doc_keys=(("ff" * 16, "d1"), ("00" * 16, "d2")), resource_pk=a),
        BuiltUsageEvent(row={}, doc_keys=(("aa" * 16, "d3"),), resource_pk=b),
    ]
    rows = _doc_key_rows(batch)
    assert [(r["resource_pk"], r["doc_key"]) for r in rows] == sorted(
        (r["resource_pk"], r["doc_key"]) for r in rows
    )
    assert rows[0]["resource_pk"] == b


# ----------------------------------------------------------------------------
# #3053: the day counts move with the events, in their transaction
# ----------------------------------------------------------------------------


async def test_day_counts_follow_the_events_in_their_transaction():
    """After the events and doc keys: one shared lock per UTC day of the
    batch's calls, oldest first, then the counts of the rows this transaction
    inserted; one commit for all of it."""
    from datetime import time as day_time

    from models.public_mcp_usage import PMU_CALLER_ANONYMOUS, PMU_ROUTE_MCP
    from services import public_mcp_usage_rollup as rollup

    db = FakeDB()
    writer = _writer(db)
    today = datetime.now(UTC).date()
    midnight = datetime.combine(today, day_time.min, tzinfo=UTC)
    for at in (midnight, midnight - timedelta(microseconds=1), midnight):
        writer.offer(_event(started_at=at))
    writer.start()
    await _until(lambda: sum(len(_events(b)) for b in db.committed) == 3)
    await writer.stop(timeout=1)

    [batch] = db.committed
    [log] = db.logs
    assert log[:2] == ["public_mcp_usage_events", "public_mcp_doc_keys"]
    locks, counts = batch["sql"][:-1], batch["sql"][-1]
    assert [s["sql"] for s in locks] == [str(rollup._LOCK_SHARED)] * 2
    yesterday = today - timedelta(days=1)
    assert [s["params"] for s in locks] == [
        {"key": rollup.day_lock_key(yesterday)},
        {"key": rollup.day_lock_key(today)},
    ]
    assert counts["sql"] == str(rollup._ADD_BATCH)
    assert counts["params"] == {
        "default_route": PMU_ROUTE_MCP,
        "default_caller": PMU_CALLER_ANONYMOUS,
    }
    assert log[2:] == [str(rollup._LOCK_SHARED)] * 2 + [str(rollup._ADD_BATCH)]


async def test_a_batch_of_gap_rows_takes_no_day_lock():
    db = FakeDB()
    writer = _writer(db)
    now = datetime.now(UTC).replace(tzinfo=None)
    writer._record_drop("queue_full", 2, now, now)
    assert await writer._flush([]) is True
    [batch] = db.committed
    assert [r["kind"] for r in _events(batch)] == ["gap"]
    assert "sql" not in batch


async def test_the_counts_take_the_rows_of_their_own_transaction():
    """Counted: what this transaction inserted (a row a retry sent again and
    the conflict skipped is not), with the read's defaults bound."""
    from models.public_mcp_usage import PMU_TXID_DEFAULT
    from services import public_mcp_usage_rollup as rollup

    sql = str(rollup._ADD_BATCH)
    assert f"txid = (SELECT {PMU_TXID_DEFAULT})" in sql
    assert "kind = 'tool_call'" in sql
    assert "COALESCE(route, :default_route)" in sql
    assert "COALESCE(caller, :default_caller)" in sql
    assert "ORDER BY 1, 2, 3, 4, 5, 6, 7" in sql
    assert "DO UPDATE SET calls = c.calls + EXCLUDED.calls" in sql
    for literal in ("'mcp'", "'anonymous'"):
        assert literal not in sql
