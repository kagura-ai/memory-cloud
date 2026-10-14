"""Tests for the APScheduler leader lock (Issue #1979).

With ``UVICORN_WORKERS > 1`` every worker runs the FastAPI lifespan, so the
in-process APScheduler must run in exactly one process of the deployment.
``SchedulerLeader`` holds a Postgres session-level advisory lock on a dedicated
connection; the holder registers the jobs and starts the scheduler, the others
stand by and retry. Postgres is faked here (no DB / Redis needed).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import OperationalError

import tasks.scheduler_leader as sl


def _conn(*scalar_results: object) -> MagicMock:
    """A fake ``AsyncConnection`` whose ``scalar`` calls return ``scalar_results``."""
    conn = MagicMock()
    conn.start = AsyncMock()
    conn.scalar = AsyncMock(side_effect=list(scalar_results))
    conn.commit = AsyncMock()
    conn.close = AsyncMock()
    conn.invalidate = AsyncMock()
    return conn


def _engine(*conns: MagicMock) -> MagicMock:
    """A fake engine handing out ``conns`` in order from ``connect()``."""
    engine = MagicMock()
    engine.connect = MagicMock(side_effect=list(conns))
    return engine


@pytest.fixture
def sched():
    """Patch the scheduler helpers the leader calls; yield the mocks."""
    scheduler = MagicMock(name="scheduler")
    with (
        patch.object(sl, "get_scheduler", return_value=scheduler) as get_scheduler,
        patch.object(sl, "start_scheduler") as start_scheduler,
        patch.object(sl, "shutdown_scheduler") as shutdown_scheduler,
    ):
        yield MagicMock(
            scheduler=scheduler,
            get_scheduler=get_scheduler,
            start=start_scheduler,
            shutdown=shutdown_scheduler,
        )


def _leader(register: MagicMock, retry_seconds: float = 60) -> sl.SchedulerLeader:
    return sl.SchedulerLeader(register_jobs=register, retry_seconds=retry_seconds)


@pytest.mark.asyncio
async def test_acquired_starts_scheduler_and_registers_jobs_once(sched):
    """Lock acquired at startup → jobs registered once, scheduler started once."""
    conn = _conn(True, True)  # try-lock True, unlock True
    register = MagicMock()
    leader = _leader(register)

    with patch.object(sl, "_get_engine", return_value=_engine(conn)):
        await leader.start()
        try:
            assert leader.is_leader is True
            register.assert_called_once_with(sched.scheduler)
            sched.start.assert_called_once_with()
            # The try-lock's autobegun transaction is committed: the session lock
            # survives COMMIT, no idle-in-transaction snapshot is pinned.
            conn.commit.assert_awaited()
            # The lock connection is held, not returned to the pool.
            conn.close.assert_not_awaited()
        finally:
            await leader.stop()


@pytest.mark.asyncio
async def test_not_acquired_stands_by_and_creates_retry_task(sched):
    """Another process holds the lock → no scheduler, a retry task is running."""
    conn = _conn(False)
    register = MagicMock()
    leader = _leader(register)

    with patch.object(sl, "_get_engine", return_value=_engine(conn)):
        await leader.start()
        try:
            assert leader.is_leader is False
            register.assert_not_called()
            sched.start.assert_not_called()
            assert leader.retry_task is not None
            assert not leader.retry_task.done()
            # Standby keeps no connection: the probe connection went back to the pool.
            conn.close.assert_awaited_once()
        finally:
            await leader.stop()
    assert leader.retry_task is None or leader.retry_task.done()


@pytest.mark.asyncio
async def test_retry_acquires_later_and_starts_scheduler_once(sched):
    """The leader dies → a standby's retry takes the lock and starts the scheduler."""
    first = _conn(False)
    second = _conn(False)
    third = _conn(True, True)
    register = MagicMock()
    leader = _leader(register)

    with patch.object(sl, "_get_engine", return_value=_engine(first, second, third)):
        await leader.start()
        assert leader.is_leader is False
        await leader.tick()  # still held elsewhere
        assert leader.is_leader is False
        sched.start.assert_not_called()
        await leader.tick()  # the old leader is gone
        assert leader.is_leader is True
        register.assert_called_once_with(sched.scheduler)
        sched.start.assert_called_once_with()
        await leader.stop()


@pytest.mark.asyncio
async def test_retry_loop_takes_over_on_its_own(sched):
    """The background loop (not just ``tick``) performs the takeover."""
    register = MagicMock()
    leader = _leader(register, retry_seconds=0.01)

    with patch.object(sl, "_get_engine", return_value=_engine(_conn(False), _conn(True, True))):
        await leader.start()
        for _ in range(200):
            if leader.is_leader:
                break
            await asyncio.sleep(0.01)
        assert leader.is_leader is True
        sched.start.assert_called_once_with()
        await leader.stop()


@pytest.mark.asyncio
async def test_lock_check_failure_goes_standby_without_crashing(sched):
    """Postgres unreachable at startup → logged, standby + retry, no exception."""
    engine = MagicMock()
    engine.connect = MagicMock(
        side_effect=OperationalError("SELECT 1", {}, Exception("connection refused"))
    )
    register = MagicMock()
    leader = _leader(register)

    with patch.object(sl, "_get_engine", return_value=engine):
        await leader.start()  # must not raise
        try:
            assert leader.is_leader is False
            register.assert_not_called()
            sched.start.assert_not_called()
            assert leader.retry_task is not None and not leader.retry_task.done()
            await leader.tick()  # retry failing again is also swallowed
            assert leader.is_leader is False
        finally:
            await leader.stop()


@pytest.mark.asyncio
async def test_try_lock_query_failure_discards_connection(sched):
    """The try-lock raising → the connection is invalidated (it may hold the lock)."""
    conn = _conn(OperationalError("SELECT", {}, Exception("server closed")))
    leader = _leader(MagicMock())

    with patch.object(sl, "_get_engine", return_value=_engine(conn)):
        await leader.start()
        try:
            assert leader.is_leader is False
            conn.invalidate.assert_awaited_once()
        finally:
            await leader.stop()


@pytest.mark.asyncio
async def test_shutdown_unlock_true_does_not_invalidate(sched):
    """Unlock returns True → connection is clean: closed, not invalidated."""
    conn = _conn(True, True)
    leader = _leader(MagicMock())

    with patch.object(sl, "_get_engine", return_value=_engine(conn)):
        await leader.start()
        await leader.stop()

    sched.shutdown.assert_called_once_with()
    assert conn.scalar.await_count == 2  # try-lock + unlock
    conn.invalidate.assert_not_awaited()
    conn.close.assert_awaited_once()
    assert leader.is_leader is False


@pytest.mark.asyncio
async def test_shutdown_unlock_false_invalidates(sched):
    """Unlock returns False → not provably released: invalidate, then close."""
    conn = _conn(True, False)
    leader = _leader(MagicMock())

    with patch.object(sl, "_get_engine", return_value=_engine(conn)):
        await leader.start()
        await leader.stop()

    sched.shutdown.assert_called_once_with()
    conn.invalidate.assert_awaited_once()
    conn.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_leader_that_loses_its_connection_stops_the_scheduler(sched):
    """The held connection dies (Postgres restart) → stop the scheduler, stand by.

    Postgres has dropped the lock with the session, so another process may take
    over; keeping our scheduler running would run every cron twice.
    """
    conn = _conn(True, OperationalError("SELECT 1", {}, Exception("terminated")))
    register = MagicMock()
    leader = _leader(register)

    with patch.object(sl, "_get_engine", return_value=_engine(conn, _conn(False))):
        await leader.start()
        assert leader.is_leader is True
        await leader.tick()  # liveness probe fails
        assert leader.is_leader is False
        sched.shutdown.assert_called_once_with()
        conn.invalidate.assert_awaited_once()
        await leader.tick()  # back on the retry path; lock now held elsewhere
        assert leader.is_leader is False
        await leader.stop()
    # Not leader at stop → the scheduler is not shut down a second time.
    sched.shutdown.assert_called_once_with()


def test_lifespan_registers_jobs_through_the_leader():
    """The app lifespan hands ``register_scheduled_jobs`` to the leader.

    The per-family ``test_registered_at_app_startup`` tests check that
    ``register_scheduled_jobs`` covers each family; this pins the other half.
    """
    main_src = (Path(__file__).resolve().parents[2] / "src" / "api" / "main.py").read_text()
    assert "register_jobs=register_scheduled_jobs" in main_src
    assert "start_scheduler()" not in main_src  # only the leader starts it
