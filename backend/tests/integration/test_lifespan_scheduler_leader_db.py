"""Lifespan shutdown with the scheduler leader on and a pooled engine (#2241).

Test processes default to ``DB_NULL_POOL=true`` and
``SCHEDULER_LEADER_ENABLED=false`` (root ``tests/conftest.py``, #1988 / #2180),
so no other lifespan test takes the leader lock or checks out a pooled
connection for it. Production runs both the other way round, and the leader
holds one connection of the engine's pool for its whole life: shutdown must
stop the leader (releasing the lock and returning that connection) before
``close_db()`` disposes the pool. Here the real lifespan runs against the test
Postgres with a fresh pooled engine; only APScheduler itself is stubbed, so no
cron job runs.
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool, NullPool

import db.base as db_base
import db.qdrant as qdrant_db
import tasks
import tasks.dense_vector_config as dense_vector_config
import tasks.geo_backfill as geo_backfill
import tasks.scheduler_leader as sl
from config.database import to_async_database_url
from config.settings import get_settings
from tasks.single_flight import TRY_LOCK_SQL, release_session_lock

_ACQUIRE_TIMEOUT_SECONDS = 20.0


@pytest.fixture
def leader_on_pooled_engine(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, object]]:
    """Leader on, a fresh pooled engine, APScheduler stubbed; records the shutdown order."""
    settings = get_settings()
    monkeypatch.setattr(settings, "scheduler_leader_enabled", True)
    monkeypatch.setattr(settings, "scheduler_leader_retry_seconds", 1)
    monkeypatch.setattr(settings, "scheduler_leader_ping_seconds", 1)
    monkeypatch.setattr(settings, "db_null_pool", False)
    # The lifespan builds the engine lazily on its own loop and disposes it in
    # close_db(); the session-wide engine comes back afterwards.
    monkeypatch.setattr(db_base, "engine", None)
    monkeypatch.setattr(db_base, "async_session_factory", None)

    monkeypatch.setattr(sl, "get_scheduler", lambda: object())
    monkeypatch.setattr(sl, "start_scheduler", lambda: None)
    monkeypatch.setattr(sl, "shutdown_scheduler", lambda: None)
    monkeypatch.setattr(tasks, "register_scheduled_jobs", lambda _scheduler: None)

    # The lifespan's one-shot background passes would take pooled sessions of
    # their own (and wait on Qdrant); only the leader's connection is pinned here.
    async def _noop() -> None:
        return None

    monkeypatch.setattr(geo_backfill, "run_location_payload_backfill", _noop)
    monkeypatch.setattr(dense_vector_config, "run_dense_vector_config_reconcile", _noop)
    monkeypatch.setattr(qdrant_db, "ensure_chunk_of_index_on_all", _noop)

    calls: list[tuple[str, object]] = []
    real_stop = sl.SchedulerLeader.stop
    real_close_db = db_base.close_db

    async def stop(self: sl.SchedulerLeader) -> None:
        calls.append(("scheduler_leader.stop", self.is_leader))
        await real_stop(self)

    async def close_db() -> None:
        # Connections still checked out of the pool when it is disposed.
        engine = db_base.engine
        calls.append(("close_db", engine.pool.checkedout() if engine is not None else None))
        await real_close_db()

    monkeypatch.setattr(sl.SchedulerLeader, "stop", stop)
    monkeypatch.setattr(db_base, "close_db", close_db)
    return calls


async def _lock_is_free() -> bool:
    """Take and release the leader lock on a separate connection."""
    engine = create_async_engine(
        to_async_database_url(os.environ["DATABASE_URL"]), poolclass=NullPool
    )
    try:
        async with engine.connect() as conn:
            acquired = bool(await conn.scalar(TRY_LOCK_SQL, {"key": sl.LOCK_KEY}))
            await conn.commit()
            if acquired:
                await release_session_lock(conn, sl.LOCK_KEY)
            return acquired
    finally:
        await engine.dispose()


def test_shutdown_stops_the_leader_before_close_db(
    leader_on_pooled_engine: list[tuple[str, object]],
) -> None:
    calls = leader_on_pooled_engine

    from api.main import app

    with TestClient(app):
        leader = app.state.scheduler_leader
        assert leader is not None
        deadline = time.monotonic() + _ACQUIRE_TIMEOUT_SECONDS
        while not leader.is_leader:
            # Another session on the test DB holding the leader lock lands here.
            assert time.monotonic() < deadline, "the leader never took the lock"
            time.sleep(0.05)
        assert isinstance(db_base.engine.pool, AsyncAdaptedQueuePool)
        assert db_base.engine.pool.checkedout() >= 1  # the leader's lock connection

    assert [name for name, _ in calls] == ["scheduler_leader.stop", "close_db"]
    assert calls[0][1] is True  # it was still the leader when shutdown stopped it
    assert calls[1][1] == 0  # its connection was back in the pool before dispose
    assert leader.is_leader is False
    assert asyncio.run(_lock_is_free())
