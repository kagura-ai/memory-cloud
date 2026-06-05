"""Tests for the sleep_maintenance_task cross-process single-flight guard (Issue #933).

The bug: APScheduler runs in-process with an in-memory jobstore, so when both
the blue and green API containers are alive (during/after a blue-green deploy)
each process fires the nightly sleep cron independently — every context was
swept twice. ``max_instances=1`` only dedupes within one process.

The fix wraps the task body in a Postgres session-level advisory lock
(``single_flight``) so the sweep runs at most once per tick across the whole
deployment: the first process to acquire the lock runs, the others no-op.

These tests pin the contract at two boundaries without a real Postgres:
- task boundary: skip the sweep when the lock is held elsewhere; run it when acquired.
- helper boundary: acquire/release semantics (unlock in finally; no unlock when not held).
"""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@asynccontextmanager
async def _fake_single_flight(acquired: bool):
    yield acquired


@pytest.mark.asyncio
async def test_sleep_maintenance_skips_when_lock_not_acquired(monkeypatch):
    """Another process holds the advisory lock → the task must do NO DB work."""
    monkeypatch.setenv("ENABLE_NEURAL_MEMORY", "true")
    monkeypatch.setenv("SLEEP_ENABLED", "true")

    from tasks import sleep_tasks

    get_db_mock = MagicMock()
    with (
        patch.object(sleep_tasks, "single_flight", lambda key: _fake_single_flight(False)),
        patch.object(sleep_tasks, "get_db", get_db_mock),
    ):
        await sleep_tasks.sleep_maintenance_task()

    # Lock not acquired → returned early, never touched the database.
    get_db_mock.assert_not_called()


@pytest.mark.asyncio
async def test_sleep_maintenance_runs_when_lock_acquired(monkeypatch):
    """Lock acquired → the task proceeds into the sweep body (get_db iterated)."""
    monkeypatch.setenv("ENABLE_NEURAL_MEMORY", "true")
    monkeypatch.setenv("SLEEP_ENABLED", "true")

    from tasks import sleep_tasks

    db = MagicMock()
    db.commit = AsyncMock()
    empty = MagicMock()
    empty.all = MagicMock(return_value=[])  # zero distinct contexts → quick exit
    db.execute = AsyncMock(return_value=empty)

    async def _get_db():
        yield db

    cfg = MagicMock()
    cfg.tag_cooccurrence_enabled = False

    with (
        patch.object(sleep_tasks, "single_flight", lambda key: _fake_single_flight(True)),
        patch.object(sleep_tasks, "get_db", _get_db),
        patch("neural.config.NeuralMemoryConfig.from_db", AsyncMock(return_value=cfg)),
        patch("neural.config.NeuralMemoryConfig.invalidate_cache", MagicMock()),
    ):
        await sleep_tasks.sleep_maintenance_task()

    # Lock acquired → body ran → the distinct-context SELECT executed.
    db.execute.assert_awaited()


@pytest.mark.asyncio
async def test_single_flight_releases_lock_on_exit():
    """single_flight must pg_advisory_unlock in finally, even when the body raises."""
    from tasks.single_flight import single_flight

    conn = MagicMock()
    # 1st scalar = pg_try_advisory_lock → True, 2nd = pg_advisory_unlock → True
    conn.scalar = AsyncMock(side_effect=[True, True])
    conn.invalidate = AsyncMock()
    conn.commit = AsyncMock()

    @asynccontextmanager
    async def _fake_connect():
        yield conn

    engine = MagicMock()
    engine.connect = lambda: _fake_connect()

    # Capture the exception manually rather than via ``pytest.raises`` as a
    # context manager: the static analyzer (CodeQL) treats the post-``with``
    # asserts as unreachable because it does not model ``pytest.raises`` swallowing
    # the exception. try/except keeps the asserts reachable and intent identical.
    raised = None
    with patch("tasks.single_flight._get_engine", return_value=engine):
        try:
            async with single_flight("sleep_maintenance") as acquired:
                assert acquired is True
                raise RuntimeError("boom")
        except RuntimeError as exc:
            raised = exc

    assert isinstance(raised, RuntimeError) and str(raised) == "boom"
    # try + unlock both ran (unlock fired from finally despite the raise).
    assert conn.scalar.await_count == 2
    # Unlock succeeded → the connection is clean, so it is NOT invalidated.
    conn.invalidate.assert_not_awaited()
    # The try-lock's autobegun transaction is committed (the session lock survives
    # COMMIT), so no idle-in-transaction is pinned for the run's duration.
    conn.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_single_flight_yields_false_when_lock_held():
    """pg_try_advisory_lock returns False → yield False and do NOT unlock."""
    from tasks.single_flight import single_flight

    conn = MagicMock()
    conn.scalar = AsyncMock(side_effect=[False])  # only the try; no unlock expected

    @asynccontextmanager
    async def _fake_connect():
        yield conn

    engine = MagicMock()
    engine.connect = lambda: _fake_connect()

    with patch("tasks.single_flight._get_engine", return_value=engine):
        async with single_flight("sleep_maintenance") as acquired:
            assert acquired is False

    # Only the try call — never held the lock, so never unlocked.
    assert conn.scalar.await_count == 1


@pytest.mark.asyncio
async def test_single_flight_unlock_failure_does_not_mask_body_error():
    """A failing unlock in the finally must NOT shadow the body's exception.

    If the connection dies, the unlock ``scalar`` call raises — but Postgres has
    already dropped the session lock on connection close, so there is nothing to
    leak. The body's original exception must still be the one that propagates.
    """
    from tasks.single_flight import single_flight

    conn = MagicMock()
    # 1st scalar = try_advisory_lock → True; 2nd = unlock → raises (dead conn).
    conn.scalar = AsyncMock(side_effect=[True, ConnectionError("db gone")])
    conn.invalidate = AsyncMock()
    conn.commit = AsyncMock()

    @asynccontextmanager
    async def _fake_connect():
        yield conn

    engine = MagicMock()
    engine.connect = lambda: _fake_connect()

    # Manual capture (not ``pytest.raises``) so CodeQL sees the post-block asserts
    # as reachable. This also pins the stronger contract: only the body's
    # RuntimeError may propagate — if the unlock's ConnectionError masked it, the
    # ``except RuntimeError`` would not catch it and the test would error.
    raised = None
    with patch("tasks.single_flight._get_engine", return_value=engine):
        try:
            async with single_flight("sleep_maintenance") as acquired:
                assert acquired is True
                raise RuntimeError("boom")
        except RuntimeError as exc:
            raised = exc

    assert isinstance(raised, RuntimeError) and str(raised) == "boom"
    assert conn.scalar.await_count == 2  # try + (failing) unlock both attempted
    # Unlock did not provably succeed → the connection is invalidated so the
    # session lock cannot leak back onto a pooled connection (it would otherwise
    # survive ROLLBACK and be re-acquired re-entrantly by the next caller).
    conn.invalidate.assert_awaited_once()


@pytest.mark.asyncio
async def test_single_flight_invalidates_when_unlock_returns_false():
    """pg_advisory_unlock returning False means this backend no longer holds the
    lock (e.g. pre-ping swapped the connection) — discard it rather than return a
    possibly-stale-lock-bearing connection to the pool."""
    from tasks.single_flight import single_flight

    conn = MagicMock()
    # try → True (acquired); unlock → False (not held by this backend).
    conn.scalar = AsyncMock(side_effect=[True, False])
    conn.invalidate = AsyncMock()
    conn.commit = AsyncMock()

    @asynccontextmanager
    async def _fake_connect():
        yield conn

    engine = MagicMock()
    engine.connect = lambda: _fake_connect()

    with patch("tasks.single_flight._get_engine", return_value=engine):
        async with single_flight("sleep_maintenance") as acquired:
            assert acquired is True

    conn.invalidate.assert_awaited_once()


@pytest.mark.asyncio
async def test_sleep_maintenance_swallows_lock_acquisition_failure(monkeypatch):
    """If acquiring the lock fails (e.g. Postgres down), the task logs and does not
    propagate — the scheduler must keep running, and the failure is reported via the
    task's own structured handler rather than escaping to APScheduler."""
    monkeypatch.setenv("ENABLE_NEURAL_MEMORY", "true")
    monkeypatch.setenv("SLEEP_ENABLED", "true")

    from tasks import sleep_tasks

    @asynccontextmanager
    async def _raising_single_flight(key):
        raise ConnectionError("postgres down")
        yield True  # pragma: no cover - unreachable, satisfies the generator contract

    get_db_mock = MagicMock()
    with (
        patch.object(sleep_tasks, "single_flight", _raising_single_flight),
        patch.object(sleep_tasks, "get_db", get_db_mock),
    ):
        # Must NOT raise — the task's try/except catches the acquisition failure.
        await sleep_tasks.sleep_maintenance_task()

    # Acquisition failed before any sweep work.
    get_db_mock.assert_not_called()
