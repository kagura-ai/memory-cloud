"""Run the in-process APScheduler in at most one process (Issue #1979).

Every uvicorn worker runs the FastAPI lifespan, and the lifespan used to start
APScheduler unconditionally — so with ``UVICORN_WORKERS=N`` each of the ~15
cron families would fire N times per tick (only the Sleep, neural and capacity
purge tasks guard themselves with :mod:`tasks.single_flight`). This module
elects one leader per deployment with a Postgres session-level advisory lock,
reusing the design and helpers of :mod:`tasks.single_flight`:

- ``pg_try_advisory_lock(hashtextextended('apscheduler_leader', 0))`` on a
  dedicated ``engine.connect()`` connection, committed right after the
  try-lock so no idle-in-transaction snapshot is pinned (the session lock
  survives COMMIT).
- The leader holds that connection for its lifetime — one slot of its own
  async pool stays checked out. When the process dies Postgres drops the
  session and the lock with it, and a standby takes over.
- Standby processes keep no connection: each retry checks one out, tries the
  lock, and returns it to the pool on failure.
- On release, an unlock that does not provably succeed ends in
  ``invalidate()`` so a still-locked backend never rides back into the pool.

Startup never waits on Postgres: :meth:`SchedulerLeader.start` only creates
the background loop. Its first tick tries the lock immediately; after that,
every ``SCHEDULER_LEADER_RETRY_SECONDS``, it either retries the lock (standby)
or checks that the held connection is alive (leader). A leader whose
connection died or stopped answering has lost the lock to Postgres, so it
stops its scheduler and goes back to standby instead of running every cron
twice next to the new leader.

What the lock does and does not guarantee: **at most one scheduler at a
time**, not one execution of each job. :func:`tasks.scheduler.shutdown_scheduler`
does not wait for running jobs (``shutdown(wait=False)``), so a job already
running on the old leader can overlap the new leader's first tick of the same
job during a handover. Jobs that must never overlap keep their own
``single_flight`` guard.

Single-process deployments acquire the lock on the first tick and behave as
before.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from db.base import _get_engine
from tasks.scheduler import get_scheduler, shutdown_scheduler, start_scheduler
from tasks.single_flight import TRY_LOCK_SQL, release_session_lock
from utils.logger import get_logger

logger = get_logger(__name__)

LOCK_KEY = "apscheduler_leader"

_PING_SQL = text("SELECT 1")

# Upper bound on one lock attempt (connect + try-lock) and on one liveness
# ping, so an unreachable or hung Postgres cannot stall the loop for asyncpg's
# 60s connect timeout or forever on a dead TCP peer.
_ATTEMPT_TIMEOUT_SECONDS = 15.0


async def _discard(conn: AsyncConnection) -> None:
    """Invalidate and close ``conn`` without raising.

    Used when it is unknown whether the backend holds the lock (the try-lock or
    its commit failed), so the connection must not return to the pool.
    """
    try:
        await conn.invalidate()
    except Exception:
        logger.warning("scheduler_leader_invalidate_failed", exc_info=True)
    try:
        await conn.close()
    except Exception:
        logger.warning("scheduler_leader_close_failed", exc_info=True)


async def _release_and_close(conn: AsyncConnection) -> None:
    """Release the leader lock (unlock, else invalidate) and close ``conn``."""
    await release_session_lock(conn, LOCK_KEY)
    try:
        await conn.close()
    except Exception:
        logger.warning("scheduler_leader_close_failed", exc_info=True)


class SchedulerLeader:
    """Leader election for the in-process APScheduler.

    Args:
        register_jobs: Adds every cron job to the given scheduler. Called each
            time this process becomes leader, on a fresh scheduler.
        retry_seconds: Interval of the background loop (standby retry and
            leader liveness check).
    """

    def __init__(
        self,
        *,
        register_jobs: Callable[[AsyncIOScheduler], None],
        retry_seconds: float,
    ) -> None:
        self._register_jobs = register_jobs
        self._retry_seconds = retry_seconds
        self._conn: AsyncConnection | None = None
        self._standby_logged = False
        self.retry_task: asyncio.Task[None] | None = None

    @property
    def is_leader(self) -> bool:
        """Whether this process holds the lock and runs the scheduler."""
        return self._conn is not None

    async def start(self) -> None:
        """Start the background loop; its first tick tries the lock at once.

        Returns without touching Postgres, so the lifespan never waits on the
        database. Hold a strong reference to this object: the event loop keeps
        tasks only weakly.
        """
        self.retry_task = asyncio.create_task(self._loop(), name="scheduler_leader_loop")

    async def stop(self) -> None:
        """Stop the loop and the scheduler, then release the lock."""
        task, self.retry_task = self.retry_task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self._conn is not None:
            shutdown_scheduler()
            conn, self._conn = self._conn, None
            await _release_and_close(conn)
            logger.info("scheduler_leader_released", pid=os.getpid())

    async def tick(self) -> None:
        """One loop iteration: liveness check when leader, lock attempt otherwise."""
        if self.is_leader:
            await self._check_alive()
        else:
            await self._attempt()

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                # E.g. job registration failed: logged, the lock was released,
                # and the next tick tries again.
                logger.exception("scheduler_leader_loop_error")
            await asyncio.sleep(self._retry_seconds)

    def _note_standby(self) -> None:
        if self._standby_logged:
            logger.debug("scheduler_leader_still_standby", pid=os.getpid())
            return
        self._standby_logged = True
        logger.info("scheduler_leader_standby", pid=os.getpid())

    async def _attempt(self) -> None:
        """Try the lock; on success register the jobs and start the scheduler.

        Raises:
            Exception: Whatever job registration or scheduler start raised; the
                lock is released first.
        """
        try:
            conn = await asyncio.wait_for(self._try_acquire(), timeout=_ATTEMPT_TIMEOUT_SECONDS)
        except Exception as e:  # noqa: BLE001 — DB down must not crash the worker
            logger.warning("scheduler_leader_check_failed", error=str(e), pid=os.getpid())
            self._note_standby()
            return
        if conn is None:
            self._note_standby()
            return
        try:
            self._register_jobs(get_scheduler())
            start_scheduler()
        except Exception:
            # Holding the lock without a running scheduler would keep every
            # other worker in standby while no cron runs at all.
            logger.exception("scheduler_leader_register_failed", pid=os.getpid())
            shutdown_scheduler()
            await _release_and_close(conn)
            raise
        self._conn = conn
        self._standby_logged = False
        logger.info("scheduler_leader_acquired", pid=os.getpid())

    async def _try_acquire(self) -> AsyncConnection | None:
        """Return the lock-holding connection, or ``None`` when held elsewhere."""
        conn = _get_engine().connect()
        await conn.start()
        try:
            acquired = bool(await conn.scalar(TRY_LOCK_SQL, {"key": LOCK_KEY}))
            await conn.commit()
        except BaseException:
            # Unknown whether the backend took the lock — never pool it.
            await _discard(conn)
            raise
        if not acquired:
            await conn.close()
            return None
        return conn

    async def _check_alive(self) -> None:
        """Verify the held connection; on failure stop the scheduler and stand by."""
        conn = self._conn
        if conn is None:
            return
        try:
            await asyncio.wait_for(self._ping(conn), timeout=_ATTEMPT_TIMEOUT_SECONDS)
            return
        except Exception as e:  # noqa: BLE001 — incl. TimeoutError; handled by stepping down
            logger.warning(
                "scheduler_leader_lost", error=str(e) or type(e).__name__, pid=os.getpid()
            )
        self._conn = None
        shutdown_scheduler()
        await _discard(conn)

    @staticmethod
    async def _ping(conn: AsyncConnection) -> None:
        await conn.scalar(_PING_SQL)
        await conn.commit()
