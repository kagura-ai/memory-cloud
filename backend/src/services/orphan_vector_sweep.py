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
  grace period.

A point whose memory row is live is never an orphan, whatever else is true.
A resource point in a live context is always kept, even when its document's
memory was forgotten: the point id cannot be matched to a row.

It runs daily from ``tasks/neural_tasks.py`` and on demand from
``cli/sweep_orphan_vectors.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

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
# merge_contexts upserts its copies before the rows that own them are
# committed, so for as long as it runs those points look row-less. It holds
# this advisory lock shared for that span; the sweep takes it exclusively
# before its second look, which therefore never sees a merge half-way.
_POINT_WRITER_LOCK_KEY = "orphan_vector_sweep:point_writers"
# The scheduled run refuses to delete more than this share of what it scanned:
# that is what a sweep pointed at the wrong database looks like. The CLI shows
# the plan to an operator instead.
SCHEDULED_MAX_ORPHAN_RATIO = 0.5

REASON_NO_ROW = "no_row"
REASON_TOMBSTONED = "tombstoned"
REASON_CONTEXT_DELETED = "context_deleted"

_DELETE_BATCH = 1000


@dataclass
class CollectionSweep:
    """One collection's share of a sweep."""

    collection: str
    scanned: int = 0
    no_row: int = 0
    tombstoned: int = 0
    context_deleted: int = 0
    deleted: int = 0
    # Why the collection could not be read (dropped mid-run, say); its counts
    # are then partial and nothing is deleted from it.
    error: str | None = None

    @property
    def orphans(self) -> int:
        return self.no_row + self.tombstoned + self.context_deleted


@dataclass
class SweepResult:
    """Outcome of one pass over the vector store."""

    dry_run: bool
    grace: timedelta
    collections: list[CollectionSweep] = field(default_factory=list)
    # Live memories that should each have exactly one point. Deployment-wide,
    # whatever ``collections`` the sweep was limited to.
    live_embedded_memories: int = 0
    # Set when the sweep found orphans and refused to delete them.
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


async def hold_point_writer_lock(db: AsyncSession) -> None:
    """Keep the sweep's delete pass out until this transaction ends.

    For a writer that upserts points before committing the rows they belong
    to. Shared: writers do not block each other.
    """
    await db.execute(
        text("SELECT pg_advisory_xact_lock_shared(hashtextextended(:key, 0))").bindparams(
            key=_POINT_WRITER_LOCK_KEY
        )
    )


async def _wait_for_point_writers(db: AsyncSession) -> None:
    """Block until no point writer is mid-transaction; held until this one ends."""
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))").bindparams(
            key=_POINT_WRITER_LOCK_KEY
        )
    )


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
        db: Async session. Nothing is written to Postgres, but a pass that
            deletes holds an advisory lock until the session's transaction
            ends, so end it promptly.
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
        QdrantError: If the collections cannot be listed or a delete fails.
            A single collection that cannot be read is skipped and reported.
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
                reasons = await _classify(db, page, cutoff)
                for ref in page:
                    reason = reasons.get(ref.point_id)
                    if reason is None:
                        continue
                    found.append(ref)
                    if reason == REASON_NO_ROW:
                        stats.no_row += 1
                    elif reason == REASON_TOMBSTONED:
                        stats.tombstoned += 1
                    else:
                        stats.context_deleted += 1
        except QdrantError as e:
            # One collection dropped mid-run (an embedding migration purge)
            # must not cost the others their sweep.
            stats.error = str(e)
            found = []
            logger.warning("orphan_vector_sweep_collection_skipped", collection=name, error=str(e))
        candidates[name] = found

    live = await db.execute(
        select(func.count())
        .select_from(Memory)
        .where(Memory.deleted_at.is_(None), Memory.embedding_status == "success")
    )
    result.live_embedded_memories = int(live.scalar_one())

    if dry_run or not result.orphans:
        return result

    if max_orphan_ratio is not None and result.orphans > result.scanned * max_orphan_ratio:
        result.refused = (
            f"{result.orphans} of {result.scanned} points look orphaned, more than "
            f"{max_orphan_ratio:.0%}; nothing deleted"
        )
        logger.error(
            "orphan_vector_sweep_refused",
            orphans=result.orphans,
            scanned=result.scanned,
            max_orphan_ratio=max_orphan_ratio,
        )
        return result

    # Second look, with every point writer out of the way: what was row-less
    # only because its transaction was still open has its row by now.
    await _wait_for_point_writers(db)

    for stats in result.collections:
        refs = candidates[stats.collection]
        for start in range(0, len(refs), _DELETE_BATCH):
            batch = refs[start : start + _DELETE_BATCH]
            still_orphaned = await _classify(db, batch, cutoff)
            point_ids = [ref.point_id for ref in batch if ref.point_id in still_orphaned]
            await delete_points_from_qdrant(point_ids, stats.collection)
            stats.deleted += len(point_ids)

    logger.info(
        "orphan_vector_sweep_completed",
        scanned=result.scanned,
        orphans=result.orphans,
        deleted=result.deleted,
        collections=[c.collection for c in result.collections if c.deleted],
    )
    return result
