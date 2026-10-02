"""The lock between point writers and the orphan vector sweep (#1798).

Postgres is the system of record: a point normally exists only for a row
that is already committed. A few writers break that order — they upsert the
point first and commit the row that owns it afterwards:

* ``merge_contexts`` copies the source's points before the copied rows commit;
* Sleep's rollback and undo-merge re-embed a memory before the UPDATE that
  un-tombstones it commits.

For as long as such a transaction is open, its points look orphaned to every
other session. Each of those writers therefore holds this advisory lock
*shared* until its transaction ends, and the sweep takes it *exclusively*
before the look that precedes a delete. Writers never block each other; the
sweep never sees one of them half-way.

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
_SET_LOCK_TIMEOUT_SQL = text("SELECT set_config('lock_timeout', :timeout, true)")


async def hold_point_writer_lock(db: AsyncSession) -> None:
    """Keep the sweep's delete pass out until this transaction ends.

    Call it before the first point is written. Shared: writers do not block
    each other, only the sweep's delete pass (seconds, once a day).
    """
    await db.execute(_SHARED_SQL)


async def wait_for_point_writers(db: AsyncSession, *, timeout_seconds: float) -> bool:
    """Take the lock exclusively, waiting up to ``timeout_seconds`` for writers.

    The lock is held until the session's transaction ends.

    Returns:
        False when a writer was still mid-transaction at the timeout. The
        session has been rolled back in that case.
    """
    await db.execute(_SET_LOCK_TIMEOUT_SQL, {"timeout": f"{int(timeout_seconds * 1000)}ms"})
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
