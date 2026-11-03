"""The lock between point writers and the orphan vector sweep (#1798).

Postgres is the system of record: a point normally exists only for a row
that is already committed. A few writers break that order — they upsert the
point first and commit the row that owns it afterwards:

* ``merge_contexts`` copies the source's points before the copied rows commit;
* Sleep's rollback and undo-merge re-embed a memory before the UPDATE that
  un-tombstones it commits;
* ``rollback_session`` un-forgets a row the same way (re-embed before the
  UPDATE commits);
* the admin context restore recreates rows for points that already exist.

For as long as such a transaction is open, its points look orphaned to every
other session. Each of those writers therefore holds this advisory lock
*shared* until its transaction ends, and the sweep takes it *exclusively*
before the look that precedes a delete. The embedding-model switch also takes
it exclusively while changing routing, so a restore either finishes under
the old route first or resolves the new route after the switch. The resource
indexer's post-commit point cleanup (#2478, once per batch) and ``forget``'s
post-commit point delete (#2201, once per call), each held at most 2 s, take
it exclusively only when it is free (:func:`try_exclude_point_writers`), so
they never queue: the indexer
leaves its points to the sweep, ``forget`` to the deferred point delete drain
(#2476, ``services.deferred_point_deletes``), which waits a bounded time
(``DEFERRED_POINT_DRAIN_WAIT_SECONDS``) once per interval.

A new writer that upserts before it commits must call
:func:`hold_point_writer_lock` first.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

_LOCK_KEY = "orphan_vector_sweep:point_writers"
_PG_LOCK_NOT_AVAILABLE = "55P03"

_SHARED_SQL = text("SELECT pg_advisory_xact_lock_shared(hashtextextended(:key, 0))").bindparams(
    key=_LOCK_KEY
)
_EXCLUSIVE_SQL = text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))").bindparams(
    key=_LOCK_KEY
)
_TRY_EXCLUSIVE_SQL = text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))").bindparams(
    key=_LOCK_KEY
)
_SET_LOCK_TIMEOUT_SQL = text("SELECT set_config('lock_timeout', :timeout, true)")


async def hold_point_writer_lock(db: AsyncSession) -> None:
    """Keep the sweep's delete pass out until this transaction ends.

    Call it before the first point is written. Shared: writers do not block
    each other, only the sweep's delete pass (seconds, once a day) and the
    short deletes of a ``try_exclude_point_writers`` holder.
    """
    await db.execute(_SHARED_SQL)


async def try_exclude_point_writers(db: AsyncSession) -> bool:
    """Take the lock exclusively if no writer holds it, without waiting.

    For a delete that has a backstop. An exclusive request that waits queues
    every new shared request behind it, so a caller that can skip its delete
    must not wait. The lock is held until the session's transaction ends.

    Returns:
        False when a writer is mid-transaction; the session stays usable.
    """
    return bool((await db.execute(_TRY_EXCLUSIVE_SQL)).scalar())


async def wait_for_point_writers(db: AsyncSession, *, timeout_seconds: float) -> bool:
    """Take the lock exclusively, waiting up to ``timeout_seconds`` for writers.

    The lock is held until the session's transaction ends.

    Returns:
        False when a writer was still mid-transaction at the timeout. The
        session has been rolled back in that case.
    """
    # At least 1 ms: lock_timeout 0 means no timeout, an unbounded wait.
    timeout_ms = max(1, int(timeout_seconds * 1000))
    await db.execute(_SET_LOCK_TIMEOUT_SQL, {"timeout": f"{timeout_ms}ms"})
    try:
        await db.execute(_EXCLUSIVE_SQL)
    except DBAPIError as exc:
        sqlstate = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
        # The transaction is aborted either way; end it before deciding.
        await db.rollback()
        if sqlstate == _PG_LOCK_NOT_AVAILABLE:
            return False
        raise
    # Do not let the timeout apply to the lookups that follow.
    await db.execute(_SET_LOCK_TIMEOUT_SQL, {"timeout": "0"})
    return True
