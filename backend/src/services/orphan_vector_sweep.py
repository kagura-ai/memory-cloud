"""Orphan vector sweep (#1798).

Postgres is the system of record and the vector store follows it best-effort:
every write path commits the row first and touches the vector afterwards. A
vector delete that fails (or, before #1798, a context deletion that never
tried) leaves a point no reader can reach — hits are hydrated from live rows —
but that still occupies candidate slots and makes the point count useless as
an integrity check.

This sweep removes those points. A point is an orphan when:

* it is a memory point whose row is gone, or was soft-deleted longer ago than
  the grace period; or
* it is a resource point (its id is derived from the document, not from a
  memory row) whose context is gone, or was soft-deleted longer ago than the
  grace period; or
* it is a resource point whose row — found by ``summary_embedding_id``, the
  column that holds the point id — was soft-deleted longer ago than the grace
  period, or is gone, was written longer ago than the grace period, and no live
  row carries the document's natural key ``(context, resource_id, doc_id,
  version)`` either (#1829: ``forget`` on a resource-ingested memory used to
  leave its point behind for good). The age check covers the indexer, which
  writes the point before the transaction that owns the row commits.

A point whose memory row is live is never an orphan, whatever else is true.
A resource point whose row cannot be decided (no natural key in the payload,
no context) is kept. Its payload's ``memory_id`` is still not used as a key:
before #1829 ``ResourceIndexer._apply_upsert`` wrote a fresh ``uuid4()`` there
on every re-index, so points written by older releases carry ids no row has.

It runs daily from ``tasks/neural_tasks.py`` and on demand from
``cli/sweep_orphan_vectors.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from db.point_writer_lock import wait_for_point_writers
from db.qdrant import (
    PointRef,
    delete_points_from_qdrant,
    list_memory_collections,
    scroll_point_refs,
)
from models.auth import Context
from models.memory import Memory
from utils.datetime import utcnow
from utils.exceptions import QdrantError
from utils.logger import get_logger

logger = get_logger(__name__)

# A soft-delete younger than this is left alone: its own vector delete may
# still be in flight, and an embed task that raced the delete can still land.
DEFAULT_GRACE = timedelta(hours=1)
# How long the delete pass waits for a writer that upserted points before
# committing their rows (db/point_writer_lock.py). Past it the pass gives up
# and the next run tries again.
POINT_WRITER_WAIT_SECONDS = 30.0
# The scheduled run refuses to delete more than this share of what it scanned:
# that is what a sweep pointed at the wrong database looks like. The CLI shows
# the plan to an operator instead.
SCHEDULED_MAX_ORPHAN_RATIO = 0.5

REASON_NO_ROW = "no_row"
REASON_TOMBSTONED = "tombstoned"
REASON_CONTEXT_DELETED = "context_deleted"
# A resource point whose row is tombstoned past the grace period, or gone with
# no live row for the document either (#1829).
REASON_RESOURCE_TOMBSTONED = "resource_tombstoned"
REASON_RESOURCE_NO_ROW = "resource_no_row"

_DELETE_BATCH = 1000


@dataclass
class CollectionSweep:
    """One collection's share of a sweep."""

    collection: str
    scanned: int = 0
    no_row: int = 0
    tombstoned: int = 0
    context_deleted: int = 0
    # Resource points judged by their own row (#1829).
    resource_tombstoned: int = 0
    resource_no_row: int = 0
    deleted: int = 0
    # Why the collection could not be read (dropped mid-run, say); its counts
    # are then partial and nothing is deleted from it.
    error: str | None = None

    @property
    def orphans(self) -> int:
        return (
            self.no_row
            + self.tombstoned
            + self.context_deleted
            + self.resource_tombstoned
            + self.resource_no_row
        )


@dataclass
class SweepResult:
    """Outcome of one pass over the vector store."""

    dry_run: bool
    grace: timedelta
    collections: list[CollectionSweep] = field(default_factory=list)
    # Live memories that should each have exactly one point. Deployment-wide,
    # whatever ``collections`` the sweep was limited to.
    live_embedded_memories: int = 0
    # Set when the sweep found orphans and deleted none: too large a share of
    # the store, or a point writer that did not finish in time.
    refused: str | None = None

    @property
    def scanned(self) -> int:
        return sum(c.scanned for c in self.collections)

    @property
    def orphans(self) -> int:
        return sum(c.orphans for c in self.collections)

    @property
    def deleted(self) -> int:
        return sum(c.deleted for c in self.collections)

    @property
    def remaining(self) -> int:
        """Points left in the swept collections after this pass."""
        return self.scanned - self.deleted


def _as_uuid(value: str | None) -> UUID | None:
    if not value:
        return None
    try:
        return UUID(str(value))
    except ValueError:
        return None


async def _classify(db: AsyncSession, refs: list[PointRef], cutoff: datetime) -> dict[str, str]:
    """Map the orphans among ``refs`` to the reason they are orphans.

    Anything that cannot be decided — an id that is not a UUID, a resource
    point with no ``context_id`` — is left out, i.e. kept.
    """
    memory_ids: dict[UUID, str] = {}
    resource_contexts: dict[str, UUID] = {}
    for ref in refs:
        if ref.is_resource:
            context_id = _as_uuid(ref.context_id)
            if context_id is not None:
                resource_contexts[ref.point_id] = context_id
            continue
        memory_id = _as_uuid(ref.point_id)
        if memory_id is not None:
            memory_ids[memory_id] = ref.point_id

    orphans: dict[str, str] = {}

    if memory_ids:
        rows = await db.execute(
            select(Memory.id, Memory.deleted_at).where(Memory.id.in_(list(memory_ids)))
        )
        deleted_at_by_id = {row.id: row.deleted_at for row in rows}
        for memory_id, point_id in memory_ids.items():
            if memory_id not in deleted_at_by_id:
                orphans[point_id] = REASON_NO_ROW
            else:
                deleted_at = deleted_at_by_id[memory_id]
                if deleted_at is not None and deleted_at < cutoff:
                    orphans[point_id] = REASON_TOMBSTONED

    if resource_contexts:
        context_ids = set(resource_contexts.values())
        rows = await db.execute(
            select(Context.id, Context.deleted_at).where(Context.id.in_(context_ids))
        )
        deleted_at_by_id = {row.id: row.deleted_at for row in rows}
        dead = {
            context_id
            for context_id in context_ids
            if context_id not in deleted_at_by_id
            or (deleted_at_by_id[context_id] is not None and deleted_at_by_id[context_id] < cutoff)
        }
        for point_id, context_id in resource_contexts.items():
            if context_id in dead:
                orphans[point_id] = REASON_CONTEXT_DELETED

        # #1829: a resource point in a live context follows its own row. The
        # point id is the row's ``summary_embedding_id``.
        live_resource = [
            ref
            for ref in refs
            if ref.is_resource and ref.point_id in resource_contexts and ref.point_id not in orphans
        ]
        orphans.update(await _classify_resource_rows(db, live_resource, cutoff))

    return orphans


async def _classify_resource_rows(
    db: AsyncSession, refs: list[PointRef], cutoff: datetime
) -> dict[str, str]:
    """Judge resource points whose context is live by their memory rows (#1829).

    A row found by ``summary_embedding_id``: live → kept; soft-deleted before
    ``cutoff`` → orphan. No row: the point must be older than ``cutoff`` (the
    indexer writes a point before its row commits), and a live row that names
    the same document under another point id — an older row, or one whose
    point was re-pointed — keeps it; otherwise it is an orphan. Points that
    cannot be decided (no natural key or no timestamp in the payload) are kept.
    """
    point_ids: dict[UUID, PointRef] = {}
    for ref in refs:
        point_uuid = _as_uuid(ref.point_id)
        if point_uuid is not None:
            point_ids[point_uuid] = ref
    if not point_ids:
        return {}

    # Scoped by the (indexed) context ids as well: summary_embedding_id alone
    # has no index.
    context_ids = {c for c in (_as_uuid(ref.context_id) for ref in point_ids.values()) if c}
    rows = await db.execute(
        select(Memory.summary_embedding_id, Memory.deleted_at).where(
            Memory.context_id.in_(list(context_ids)),
            Memory.summary_embedding_id.in_(list(point_ids)),
        )
    )
    # Several rows can name one point: a forgotten document that was synced
    # again gets a NEW row under the SAME uuid5 id while the tombstone keeps
    # its summary_embedding_id. Any live row keeps the point; only when every
    # row is a tombstone does the newest one decide.
    live_points: set[UUID] = set()
    newest_tombstone: dict[UUID, datetime] = {}
    for row in rows:
        if row.deleted_at is None:
            live_points.add(row.summary_embedding_id)
        else:
            previous = newest_tombstone.get(row.summary_embedding_id)
            if previous is None or row.deleted_at > previous:
                newest_tombstone[row.summary_embedding_id] = row.deleted_at

    orphans: dict[str, str] = {}
    unmatched: list[PointRef] = []
    for point_uuid, ref in point_ids.items():
        if point_uuid in live_points:
            continue
        if point_uuid in newest_tombstone:
            if newest_tombstone[point_uuid] < cutoff:
                orphans[ref.point_id] = REASON_RESOURCE_TOMBSTONED
        elif (
            ref.resource_key is not None
            and ref.updated_at is not None
            and ref.updated_at < cutoff
            and _as_uuid(ref.context_id) is not None
        ):
            unmatched.append(ref)

    if unmatched:
        # One query for every unmatched document: live rows for exactly these
        # natural keys (not every row of the resource).
        keyed: dict[tuple[UUID, str, str, int], PointRef] = {}
        for ref in unmatched:
            context_uuid = _as_uuid(ref.context_id)
            if context_uuid is not None and ref.resource_key is not None:
                keyed[(context_uuid, *ref.resource_key)] = ref
        rows = await db.execute(
            select(
                Memory.context_id,
                Memory.resource_id,
                Memory.resource_doc_id,
                Memory.resource_version,
            ).where(
                tuple_(
                    Memory.context_id,
                    Memory.resource_id,
                    Memory.resource_doc_id,
                    Memory.resource_version,
                ).in_(list(keyed)),
                Memory.deleted_at.is_(None),
            )
        )
        live_keys = {
            (row.context_id, row.resource_id, row.resource_doc_id, row.resource_version)
            for row in rows
        }
        for key, ref in keyed.items():
            if key not in live_keys:
                orphans[ref.point_id] = REASON_RESOURCE_NO_ROW

    return orphans


async def sweep_orphan_points(
    db: AsyncSession,
    *,
    dry_run: bool = True,
    grace: timedelta = DEFAULT_GRACE,
    collections: list[str] | None = None,
    max_orphan_ratio: float | None = None,
) -> SweepResult:
    """Find the vector store's orphaned points and, unless ``dry_run``, delete them.

    Args:
        db: Async session. Nothing is written to Postgres. The session is
            rolled back after every page of the scan (#1804) and, on a pass
            that deletes, after each batch, which releases the point-writer
            lock: no transaction stays open while the vector store is read
            or written. Give it a session of its own.
        dry_run: Count only.
        grace: How long a soft-delete must have stood before its point counts
            as an orphan.
        collections: Limit the sweep to these collections (default: every
            ``kagura_memories*`` collection).
        max_orphan_ratio: Refuse to delete when orphans exceed this share of
            the points scanned; ``result.refused`` then says so. ``None``
            disables the check.

    Returns:
        SweepResult with per-collection counts.

    Raises:
        QdrantError: If the collections cannot be listed. A single collection
            that cannot be read or written is skipped and reported.
    """
    cutoff = utcnow() - grace
    names = collections if collections is not None else await list_memory_collections()
    result = SweepResult(dry_run=dry_run, grace=grace)
    candidates: dict[str, list[PointRef]] = {}

    for name in names:
        stats = CollectionSweep(collection=name)
        result.collections.append(stats)
        found: list[PointRef] = []
        try:
            async for page in scroll_point_refs(name):
                stats.scanned += len(page)
                try:
                    reasons = await _classify(db, page, cutoff)
                finally:
                    # #1804: end the read transaction before the next page is
                    # fetched. Scrolling every collection can take longer than
                    # a deployment's idle_in_transaction_session_timeout, and
                    # a transaction left open across it would be cut off.
                    await db.rollback()
                for ref in page:
                    reason = reasons.get(ref.point_id)
                    if reason is None:
                        continue
                    found.append(ref)
                    if reason == REASON_NO_ROW:
                        stats.no_row += 1
                    elif reason == REASON_TOMBSTONED:
                        stats.tombstoned += 1
                    elif reason == REASON_RESOURCE_TOMBSTONED:
                        stats.resource_tombstoned += 1
                    elif reason == REASON_RESOURCE_NO_ROW:
                        stats.resource_no_row += 1
                    else:
                        stats.context_deleted += 1
        except QdrantError as e:
            # One collection dropped mid-run (an embedding migration purge)
            # must not cost the others their sweep.
            stats.error = str(e)
            found = []
            # A partial count would be asked about, weighed against the ratio
            # guard and never deleted.
            stats.no_row = stats.tombstoned = stats.context_deleted = 0
            stats.resource_tombstoned = stats.resource_no_row = 0
            logger.warning("orphan_vector_sweep_collection_skipped", collection=name, error=str(e))
        candidates[name] = found

    live = await db.execute(
        select(func.count())
        .select_from(Memory)
        .where(Memory.deleted_at.is_(None), Memory.embedding_status == "success")
    )
    result.live_embedded_memories = int(live.scalar_one())
    await db.rollback()

    if dry_run or not result.orphans:
        return result

    # Weighed over the collections that were read in full: a skipped one
    # contributes no orphans, and its partial point count must not dilute
    # the share either.
    readable = sum(c.scanned for c in result.collections if c.error is None)
    if max_orphan_ratio is not None and result.orphans > readable * max_orphan_ratio:
        result.refused = (
            f"{result.orphans} of {readable} points look orphaned, more than "
            f"{max_orphan_ratio:.0%}; nothing deleted"
        )
        logger.error(
            "orphan_vector_sweep_refused",
            orphans=result.orphans,
            scanned=readable,
            max_orphan_ratio=max_orphan_ratio,
        )
        return result

    # Second look, a batch at a time, with every point writer out of the way:
    # what was row-less only because its transaction was still open has its
    # row by now. The lock is released after each batch, so a merge or a Sleep
    # rollback never waits for more than one of them.
    for stats in result.collections:
        refs = candidates[stats.collection]
        for start in range(0, len(refs), _DELETE_BATCH):
            batch = refs[start : start + _DELETE_BATCH]
            if not await wait_for_point_writers(db, timeout_seconds=POINT_WRITER_WAIT_SECONDS):
                result.refused = (
                    f"a merge or a Sleep rollback was still writing points after "
                    f"{POINT_WRITER_WAIT_SECONDS:.0f}s; stopped after deleting {result.deleted}"
                )
                logger.warning(
                    "orphan_vector_sweep_point_writers_busy",
                    orphans=result.orphans,
                    deleted=result.deleted,
                )
                return result
            try:
                still_orphaned = await _classify(db, batch, cutoff)
                point_ids = [ref.point_id for ref in batch if ref.point_id in still_orphaned]
                await delete_points_from_qdrant(point_ids, stats.collection)
                stats.deleted += len(point_ids)
            except QdrantError as e:
                # Same rule as the scan: one collection that cannot be
                # written must not cost the others their sweep.
                stats.error = str(e)
                logger.warning(
                    "orphan_vector_sweep_collection_skipped",
                    collection=stats.collection,
                    error=str(e),
                )
                break
            finally:
                # Ends the transaction, which is what releases the lock.
                await db.rollback()

    logger.info(
        "orphan_vector_sweep_completed",
        scanned=result.scanned,
        orphans=result.orphans,
        deleted=result.deleted,
        collections=[c.collection for c in result.collections if c.deleted],
    )
    return result
