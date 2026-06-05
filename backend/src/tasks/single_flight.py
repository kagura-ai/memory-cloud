"""Cross-process single-flight guard via Postgres session-level advisory locks.

Issue #933. APScheduler runs in-process with an in-memory jobstore, so when
more than one API process is alive (e.g. both the blue and green containers
during/after a blue-green deploy) each process fires the same cron
independently. ``max_instances=1`` / ``coalesce`` only dedupe *within* one
process. This guard makes a scheduled task run at most once per tick across the
whole deployment: the first process to acquire the Postgres advisory lock runs
the body; the others no-op.

Design notes (see issue #933 gate1 review):

- **Session-scoped, not transaction-scoped.** The codebase already uses
  ``pg_advisory_xact_lock`` elsewhere (``quota_service``,
  ``connector_provisioning``, admin workspace-create), but those locks release
  at transaction end. Callers of this guard (e.g. ``sleep_maintenance_task``)
  commit repeatedly inside the run, so the lock must outlive those commits —
  hence ``pg_try_advisory_lock`` (session-level) held on a dedicated
  connection for the lifetime of the ``async with`` block.
- **Explicit unlock in ``finally``.** A session-level lock is bound to the
  physical connection, and SQLAlchemy returns connections to the pool rather
  than closing them — so the lock would *leak* onto a pooled connection if we
  relied on connection close. We unlock explicitly before the connection is
  returned. If the whole process dies the lock is still released automatically
  by Postgres (the physical connection drops), so no manual cleanup table is
  needed.
- **Key derivation** matches the rest of the codebase:
  ``hashtextextended(:key, 0)`` → the 64-bit bigint signature of
  ``pg_try_advisory_lock``.
- **Dedicated connection.** The lock is held on a connection from
  ``engine.connect()`` that is independent of any ``AsyncSession`` the caller
  opens (e.g. via ``get_db()``) — they are separate pool checkouts. So the
  caller committing repeatedly on its own session does NOT release this
  session-level lock; only this module's explicit unlock (or process death)
  does. Consequence: a guarded task holds **two** pool slots for the run's
  duration (the lock connection here plus the caller's work session). That is
  fine for an off-peak nightly cron, but weigh it before applying the guard to
  high-frequency tasks — consider a dedicated/NullPool engine for the lock there.
- **Lock keys must be globally unique** across all single-flight consumers in
  the deployment, since they all share the one advisory-lock keyspace. Existing
  callers scope theirs (``workspace_create:<id>``, ``connector_seat:<id>``); a
  process-wide cron uses a bare name like ``sleep_maintenance``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from db.base import _get_engine
from utils.logger import get_logger

logger = get_logger(__name__)

_TRY_LOCK_SQL = text("SELECT pg_try_advisory_lock(hashtextextended(:key, 0))")
_UNLOCK_SQL = text("SELECT pg_advisory_unlock(hashtextextended(:key, 0))")


async def _release(conn: AsyncConnection, lock_key: str) -> None:
    """Release the advisory lock; safe to call from a ``finally`` (never raises).

    Critical invariant: the connection must NOT return to the pool while still
    holding the session-level lock. A pooled connection is reset with ROLLBACK
    (SQLAlchemy's default ``reset_on_return``), which does NOT release session
    advisory locks — and a later checkout that re-runs ``pg_try_advisory_lock`` on
    that same backend gets ``True`` (re-entrant), silently defeating the
    cross-process guard. So whenever the unlock does not *provably* succeed we
    ``invalidate()`` the connection: the physical backend is closed and Postgres
    drops the lock on session end.
    """
    try:
        released = bool(await conn.scalar(_UNLOCK_SQL, {"key": lock_key}))
        if released:
            return
        # Unlock returned False — this backend did not hold the lock (e.g. pre-ping
        # swapped the underlying connection). Discard it so a stale lock can't ride
        # a pooled connection into the next caller.
        logger.warning("single_flight_unlock_unexpected", lock_key=lock_key)
    except Exception:
        # Unlock failed for a reason other than provable success (statement
        # timeout, InterfaceError, transient OSError, ...). The connection may
        # still be alive AND still holding the lock — fall through to invalidate.
        logger.warning("single_flight_unlock_failed", lock_key=lock_key, exc_info=True)

    try:
        await conn.invalidate()
    except Exception:
        logger.warning("single_flight_invalidate_failed", lock_key=lock_key, exc_info=True)


@asynccontextmanager
async def single_flight(lock_key: str) -> AsyncIterator[bool]:
    """Yield ``True`` iff this process acquired the cross-process lock for ``lock_key``.

    Usage::

        async with single_flight("sleep_maintenance") as acquired:
            if not acquired:
                return
            ...  # do the work; safe across multiple API processes

    Args:
        lock_key: stable string identifying the task. Distinct tasks MUST use
            distinct keys so they don't block one another.

    Yields:
        ``True`` when the advisory lock was acquired (caller should run),
        ``False`` when another process already holds it (caller should skip).
    """
    engine = _get_engine()
    async with engine.connect() as conn:
        acquired = bool(await conn.scalar(_TRY_LOCK_SQL, {"key": lock_key}))
        if not acquired:
            # debug, not info: the sole caller already logs a domain-level skip
            # (e.g. sleep_maintenance_task_skipped), so an info line here just
            # doubles it on every contended tick.
            logger.debug("single_flight_skipped", lock_key=lock_key)
            yield False
            return
        try:
            yield True
        finally:
            # ``_release`` never raises, so the body's exception (if any) still
            # propagates, and it guarantees the lock is dropped (unlock or, on any
            # non-success, connection invalidation) rather than leaked to the pool.
            await _release(conn, lock_key)
