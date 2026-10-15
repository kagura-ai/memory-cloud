"""Tests for the APScheduler leader lock (Issue #1979).

With ``UVICORN_WORKERS > 1`` every worker runs the FastAPI lifespan, so the
in-process APScheduler must run in at most one process of the deployment.
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


async def _until(predicate, steps: int = 200) -> None:
    for _ in range(steps):
        if predicate():
            return
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_acquired_starts_scheduler_and_registers_jobs_once(sched):
    """Lock acquired → jobs registered once, scheduler started once, conn held."""
    conn = _conn(True, True)  # try-lock True, unlock True
    register = MagicMock()
    leader = _leader(register)

    with patch.object(sl, "_get_engine", return_value=_engine(conn)):
        await leader.tick()
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
async def test_start_does_not_touch_postgres_and_first_tick_runs_at_once(sched):
    """start() only creates the loop; the loop's first tick acquires without waiting."""
    engine = _engine(_conn(True, True))
    leader = _leader(MagicMock(), retry_seconds=3600)

    with patch.object(sl, "_get_engine", return_value=engine):
        await leader.start()
        engine.connect.assert_not_called()  # startup never waits on Postgres
        assert leader.retry_task is not None
        await _until(lambda: leader.is_leader)
        assert leader.is_leader is True  # no retry_seconds sleep before tick one
        sched.start.assert_called_once_with()
        await leader.stop()
    assert leader.retry_task is None


@pytest.mark.asyncio
async def test_not_acquired_stands_by_and_logs_standby_once(sched):
    """Another process holds the lock → no scheduler, no held conn, one info line."""
    conns = [_conn(False), _conn(False), _conn(False)]
    register = MagicMock()
    leader = _leader(register)

    with (
        patch.object(sl, "_get_engine", return_value=_engine(*conns)),
        patch.object(sl, "logger") as logger,
    ):
        for _ in conns:
            await leader.tick()
        assert leader.is_leader is False
        register.assert_not_called()
        sched.start.assert_not_called()
        # Standby keeps no connection: each probe went back to the pool.
        for c in conns:
            c.close.assert_awaited_once()
        info_events = [c.args[0] for c in logger.info.call_args_list]
        assert info_events.count("scheduler_leader_standby") == 1
        await leader.stop()


@pytest.mark.asyncio
async def test_retry_acquires_later_and_starts_scheduler_once(sched):
    """The leader dies → a standby's retry takes the lock and starts the scheduler."""
    register = MagicMock()
    leader = _leader(register)

    with patch.object(
        sl, "_get_engine", return_value=_engine(_conn(False), _conn(False), _conn(True, True))
    ):
        await leader.tick()
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
    leader = _leader(MagicMock(), retry_seconds=0.01)

    with patch.object(sl, "_get_engine", return_value=_engine(_conn(False), _conn(True, True))):
        await leader.start()
        await _until(lambda: leader.is_leader)
        assert leader.is_leader is True
        sched.start.assert_called_once_with()
        await leader.stop()


@pytest.mark.asyncio
async def test_lock_check_failure_goes_standby_without_crashing(sched):
    """Postgres unreachable → logged, standby, no exception; the loop keeps going."""
    engine = MagicMock()
    engine.connect = MagicMock(
        side_effect=OperationalError("SELECT 1", {}, Exception("connection refused"))
    )
    register = MagicMock()
    leader = _leader(register, retry_seconds=0.01)

    with patch.object(sl, "_get_engine", return_value=engine):
        await leader.tick()  # must not raise
        assert leader.is_leader is False
        register.assert_not_called()
        sched.start.assert_not_called()
        await leader.start()
        await _until(lambda: engine.connect.call_count >= 3)
        assert leader.retry_task is not None and not leader.retry_task.done()
        await leader.stop()


@pytest.mark.asyncio
async def test_try_lock_query_failure_discards_connection(sched):
    """The try-lock raising → the connection is invalidated (it may hold the lock)."""
    conn = _conn(OperationalError("SELECT", {}, Exception("server closed")))
    leader = _leader(MagicMock())

    with patch.object(sl, "_get_engine", return_value=_engine(conn)):
        await leader.tick()
        assert leader.is_leader is False
        conn.invalidate.assert_awaited_once()
        await leader.stop()


@pytest.mark.asyncio
async def test_registration_failure_releases_the_lock(sched):
    """Registering the jobs raises → lock released, no scheduler, not leader.

    Keeping the lock without a scheduler would leave every worker in standby
    while no cron runs at all.
    """
    conn = _conn(True, True)  # try-lock True, unlock True
    register = MagicMock(side_effect=RuntimeError("bad job"))
    leader = _leader(register)

    raised = None
    with patch.object(sl, "_get_engine", return_value=_engine(conn)):
        try:
            await leader.tick()
        except RuntimeError as exc:
            raised = exc

    assert isinstance(raised, RuntimeError)
    assert leader.is_leader is False
    sched.start.assert_not_called()
    sched.shutdown.assert_called_once_with()
    assert conn.scalar.await_count == 2  # try-lock + unlock
    conn.invalidate.assert_not_awaited()
    conn.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_shutdown_unlock_true_does_not_invalidate(sched):
    """Unlock returns True → connection is clean: closed, not invalidated."""
    conn = _conn(True, True)
    leader = _leader(MagicMock())

    with patch.object(sl, "_get_engine", return_value=_engine(conn)):
        await leader.tick()
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
        await leader.tick()
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
    leader = _leader(MagicMock())

    with patch.object(sl, "_get_engine", return_value=_engine(conn, _conn(False))):
        await leader.tick()
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


@pytest.mark.asyncio
async def test_leader_whose_ping_hangs_steps_down(sched):
    """A ping that never answers (dead TCP peer) times out → treated as lost."""
    calls = 0

    async def scalar(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return True  # try-lock
        await asyncio.Event().wait()  # the ping hangs forever

    conn = _conn()
    conn.scalar = AsyncMock(side_effect=scalar)
    leader = _leader(MagicMock())

    with (
        patch.object(sl, "_get_engine", return_value=_engine(conn)),
        patch.object(sl, "_ATTEMPT_TIMEOUT_SECONDS", 0.05),
    ):
        await leader.tick()
        assert leader.is_leader is True
        await asyncio.wait_for(leader.tick(), timeout=5)
    assert leader.is_leader is False
    sched.shutdown.assert_called_once_with()
    conn.invalidate.assert_awaited_once()


def test_lifespan_registers_jobs_through_the_leader():
    """The app lifespan hands ``register_scheduled_jobs`` to the leader.

    The per-family ``test_registered_at_app_startup`` tests check that
    ``register_scheduled_jobs`` covers each family; this pins the other half.
    """
    main_src = (Path(__file__).resolve().parents[2] / "src" / "api" / "main.py").read_text()
    assert "register_jobs=register_scheduled_jobs" in main_src
    assert "start_scheduler()" not in main_src  # only the leader starts it


async def test_discard_is_bounded_when_the_peer_never_answers(monkeypatch):
    """A half-open socket must not hang the loop: invalidate/close time out and are skipped."""
    monkeypatch.setattr(sl, "_ATTEMPT_TIMEOUT_SECONDS", 0.05)
    conn = _conn()
    conn.invalidate = AsyncMock(side_effect=lambda: asyncio.Event().wait())
    conn.close = AsyncMock(side_effect=lambda: asyncio.Event().wait())

    await asyncio.wait_for(sl._discard(conn), timeout=2)

    conn.invalidate.assert_awaited_once()
    conn.close.assert_awaited_once()


def test_register_scheduled_jobs_calls_every_schedule_function():
    """Every exported ``schedule_*`` is registered exactly once: dropping one must fail here."""
    import tasks

    names = [n for n in tasks.__all__ if n.startswith("schedule_")]
    assert len(names) >= 15
    scheduler = MagicMock(name="scheduler")
    with patch.multiple(tasks, **{n: MagicMock(name=n) for n in names}):
        tasks.register_scheduled_jobs(scheduler)
        for n in names:
            getattr(tasks, n).assert_called_once_with(scheduler)
