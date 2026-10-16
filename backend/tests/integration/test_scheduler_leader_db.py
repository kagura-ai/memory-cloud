"""The scheduler leader against the real Postgres (#1988).

The integration job runs every ``TestClient(app)`` lifespan with
``SCHEDULER_LEADER_ENABLED=false``, so the leader's real ``engine.connect()``,
advisory-lock and release path is pinned here instead, with the scheduler
itself stubbed (no cron job runs).
"""

from __future__ import annotations

import pytest

import tasks.scheduler_leader as sl


@pytest.fixture
def _no_scheduler(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sl, "get_scheduler", lambda: object())
    monkeypatch.setattr(sl, "start_scheduler", lambda: None)
    monkeypatch.setattr(sl, "shutdown_scheduler", lambda: None)


def _leader() -> sl.SchedulerLeader:
    return sl.SchedulerLeader(register_jobs=lambda _sched: None, retry_seconds=5, ping_seconds=1)


@pytest.mark.asyncio
async def test_leader_takes_and_releases_the_lock(_no_scheduler: None) -> None:
    """One leader at a time: a second tick stands by while the lock is held,
    the held connection answers the liveness ping, and ``stop()`` releases the
    lock so the standby acquires it on its next tick."""
    first, second = _leader(), _leader()
    try:
        await first.tick()
        assert first.is_leader is True

        await second.tick()
        assert second.is_leader is False

        await first.tick()  # liveness ping on the held connection
        assert first.is_leader is True

        await first.stop()
        assert first.is_leader is False

        await second.tick()
        assert second.is_leader is True
    finally:
        await first.stop()
        await second.stop()
