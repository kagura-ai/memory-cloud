"""Vector point deletes that wait for the point writers to finish (#2476).

``forget`` tombstones its rows and deletes their search points after the
commit, under the point-writer lock taken exclusively
(``db.point_writer_lock``). A writer that upserts a point before its row
commits — the resource indexer for a whole batch, a merge, a Sleep or session
rollback, an admin restore — holds that lock shared, and ``forget`` does not
wait for it: an exclusive request that waits queues every new writer behind
it. So ``forget`` queues each point in ``deferred_point_deletes`` in the
transaction that tombstones the row, removes the row from the queue once it
has handled the point, and :func:`drain_deferred_point_deletes` retries what
is left on a schedule, waiting a bounded time for the writers.

Lock order, everywhere: point-writer lock, then the queue rows and the
``memories`` read, then the vector store, then the DELETE of the queue rows,
then the commit. Nothing here holds a queue-row or ``memories`` row lock while
it waits for the point-writer lock.

A point is handled when it was deleted, or when a live row names it again
(a restore, or a re-sync of the same resource document version, #1829): the
live owner was seen under the exclusive lock, so re-checking it would only
repeat that answer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import delete, func, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from models.deferred_point_delete import DeferredPointDelete
from models.memory import Memory
from utils.logger import get_logger

logger = get_logger(__name__)

# (memory id, point id, collection)
PointDelete = tuple[UUID, UUID, str]
# Rows per drain run: one SELECT, one look, two vector-store calls per collection.
DRAIN_BATCH = 1000


@dataclass(frozen=True)
class QueuedRow:
    """A queue row as read under the lock; ``queued_at`` is its version."""

    id: UUID
    queued_at: datetime


@dataclass
class DrainResult:
    """What one drain run did, for the task's log line."""

    drained: int = 0
    kept: int = 0
    pending: int = 0
    oldest_age_seconds: float | None = None
    busy: bool = False
    failed: str | None = None


def _unique(entries: list[PointDelete]) -> list[PointDelete]:
    return list(dict.fromkeys(entries))


async def queue_point_deletes(db: AsyncSession, entries: list[PointDelete]) -> None:
    """Queue ``entries`` in ``db``'s transaction; the caller commits.

    A key already queued (a row restored and forgotten again before its first
    delete ran) gets a new ``queued_at``: a drain or a fast path that read the
    old version and saw the row live then leaves the new one in the queue.
    """
    entries = _unique(entries)
    if not entries:
        return
    stmt = pg_insert(DeferredPointDelete).values(
        [
            {"id": uuid4(), "memory_id": memory_id, "point_id": point_id, "collection": collection}
            for memory_id, point_id, collection in entries
        ]
    )
    await db.execute(
        stmt.on_conflict_do_update(
            constraint="uq_deferred_point_delete",
            set_={"queued_at": func.clock_timestamp(), "attempts": 0},
        )
    )


async def exclude_point_writers(session: AsyncSession, *, wait_seconds: float | None) -> bool:
    """Take the point-writer lock exclusively for ``session``'s transaction.

    ``wait_seconds`` ``None`` or ``<= 0`` never waits (the request path);
    otherwise waits up to that long, and on ``False`` the session has been
    rolled back (``wait_for_point_writers``).
    """
    from db.point_writer_lock import try_exclude_point_writers, wait_for_point_writers

    if wait_seconds is None or wait_seconds <= 0:
        return await try_exclude_point_writers(session)
    return await wait_for_point_writers(session, timeout_seconds=wait_seconds)


async def live_owned_points(session: AsyncSession, point_ids: set[UUID]) -> set[UUID]:
    """The points of ``point_ids`` that a live row names, in any context.

    Unscoped on purpose: a resource point is shared by every row of the same
    document version (#1829), whichever context or collection it is in.
    """
    if not point_ids:
        return set()
    points = list(point_ids)
    return set(
        (
            await session.execute(
                select(func.coalesce(Memory.summary_embedding_id, Memory.id)).where(
                    Memory.deleted_at.is_(None),
                    or_(Memory.id.in_(points), Memory.summary_embedding_id.in_(points)),
                )
            )
        ).scalars()
    )


async def delete_points_by_collection(entries: list[PointDelete]) -> None:
    """One point delete and one chunk delete per collection. Raises on failure."""
    from db.qdrant import delete_chunk_points, delete_points_from_qdrant

    collections: dict[str, list[PointDelete]] = {}
    for entry in entries:
        collections.setdefault(entry[2], []).append(entry)
    for collection, group in collections.items():
        await delete_points_from_qdrant([str(entry[1]) for entry in group], collection)
        await delete_chunk_points([entry[0] for entry in group], collection)


async def queued_rows_for(session: AsyncSession, entries: list[PointDelete]) -> list[QueuedRow]:
    """The queue rows of ``entries``, read without a row lock."""
    if not entries:
        return []
    rows = (
        await session.execute(
            select(DeferredPointDelete.id, DeferredPointDelete.queued_at).where(
                tuple_(
                    DeferredPointDelete.memory_id,
                    DeferredPointDelete.point_id,
                    DeferredPointDelete.collection,
                ).in_(_unique(entries))
            )
        )
    ).all()
    return [QueuedRow(row.id, row.queued_at) for row in rows]


async def dequeue(session: AsyncSession, rows: list[QueuedRow]) -> None:
    """Delete the handled queue rows still at the version read; the caller commits.

    A row re-queued since (see :func:`queue_point_deletes`) has another
    ``queued_at`` and stays for the next drain.
    """
    if not rows:
        return
    await session.execute(
        delete(DeferredPointDelete)
        .where(
            tuple_(DeferredPointDelete.id, DeferredPointDelete.queued_at).in_(
                [(row.id, row.queued_at) for row in rows]
            )
        )
        .execution_options(synchronize_session=False)
    )


async def points_free_of_writers(
    session: AsyncSession, entries: list[PointDelete], *, wait_seconds: float | None
) -> tuple[list[PointDelete], list[QueuedRow]] | None:
    """Lock, then the entries no live row names and their queue rows.

    ``None`` when a writer holds the lock (after ``wait_seconds``, if any).
    The lock stays held until ``session``'s transaction ends: delete the free
    points, :func:`dequeue` the rows and commit, all before closing it.
    """
    if not await exclude_point_writers(session, wait_seconds=wait_seconds):
        return None
    queued = await queued_rows_for(session, entries)
    owned = await live_owned_points(session, {point_id for _, point_id, _ in entries})
    if owned:
        logger.info("forget_point_kept_live_owner", points=len(owned))
    return [entry for entry in entries if entry[1] not in owned], queued


async def _backlog(db: AsyncSession) -> tuple[int, float | None]:
    count, oldest = (
        await db.execute(
            select(
                func.count(),
                func.extract("epoch", func.now() - func.min(DeferredPointDelete.queued_at)),
            ).select_from(DeferredPointDelete)
        )
    ).one()
    return int(count or 0), (float(oldest) if oldest is not None else None)


async def drain_deferred_point_deletes(
    db: AsyncSession, *, wait_seconds: float, limit: int = DRAIN_BATCH
) -> DrainResult:
    """Delete up to ``limit`` queued points, oldest first, once no writer holds the lock.

    Takes no lock when the queue is empty. Waits up to ``wait_seconds`` for the
    point writers (``<= 0``: does not wait); past that it touches nothing and
    reports ``busy``. Each collection is deleted on its own: a vector-store
    failure keeps that collection's rows queued with one more attempt and
    reports ``failed`` — it never raises for that — while the other
    collections' rows leave the queue. A collection that no longer exists
    holds no point, so its rows are handled.
    """
    from db.qdrant import list_memory_collections

    pending, oldest = await _backlog(db)
    result = DrainResult(pending=pending, oldest_age_seconds=oldest)
    await db.rollback()
    if not pending:
        return result
    try:
        existing = set(await list_memory_collections())
    except Exception as exc:
        result.failed = type(exc).__name__
        logger.warning("deferred_point_deletes_failed", points=0, error_class=result.failed)
        return result

    if not await exclude_point_writers(db, wait_seconds=wait_seconds):
        await db.rollback()
        result.busy = True
        return result

    rows = (
        await db.execute(
            select(
                DeferredPointDelete.id,
                DeferredPointDelete.memory_id,
                DeferredPointDelete.point_id,
                DeferredPointDelete.collection,
                DeferredPointDelete.queued_at,
            )
            .order_by(DeferredPointDelete.queued_at, DeferredPointDelete.id)
            .limit(limit)
        )
    ).all()
    owned = await live_owned_points(db, {row.point_id for row in rows})
    by_collection: dict[str, list[Any]] = {}
    for row in rows:
        by_collection.setdefault(row.collection, []).append(row)

    handled: list[Any] = []
    failed: list[Any] = []
    for collection, group in by_collection.items():
        free = [row for row in group if row.point_id not in owned]
        if collection in existing and free:
            try:
                await delete_points_by_collection(
                    [(row.memory_id, row.point_id, row.collection) for row in free]
                )
            except Exception as exc:
                failed.extend(group)
                result.failed = type(exc).__name__
                logger.warning(
                    "deferred_point_deletes_failed",
                    points=len(free),
                    error_class=result.failed,
                )
                continue
            result.drained += len(free)
        result.kept += len(group) - len(free)
        handled.extend(group)

    await dequeue(db, [QueuedRow(row.id, row.queued_at) for row in handled])
    # Ends the transaction, which releases the lock before the bump.
    await db.commit()
    if failed:
        await db.execute(
            update(DeferredPointDelete)
            .where(DeferredPointDelete.id.in_([row.id for row in failed]))
            .values(attempts=DeferredPointDelete.attempts + 1)
            .execution_options(synchronize_session=False)
        )
        await db.commit()
    result.pending = max(0, pending - len(handled))
    return result
