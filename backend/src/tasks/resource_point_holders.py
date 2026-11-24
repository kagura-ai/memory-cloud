"""Bring resource points left from before v0.181.0 under the slug-holder rule.

``python -m src.cli.repair_resource_point_holders`` runs it. A resource
point (uuid5 of ``resource_id:doc_id:vN``) belongs to the live row of the
context that holds the slug (``services.resource_indexer.detach_resource_rows``).
Since v0.181.0 every writer keeps to that, but rows written before it can
still be in one of three states:

* ``released``: a live row names a resource point while its context no longer
  holds the slug (it was unpublished or took another ``resource_id``). It is
  detached: a point of its own under its row id, with a plain memory's
  payload, copied from the shared point while that point still holds the
  row's context's document, else queued for the embedding pipeline.
* ``shared``: a row of the holder names a point that a live row of another
  context names too. The other rows are detached the same way.
* ``foreign``: a row of the holder names a point whose payload carries
  another context. The point is rebuilt from the holder's row
  (``ResourceIndexer.rebuild_point``, one embedding request per document),
  after the other rows are detached.

Per context, by id, a page of rows at a time. Plan mode reads the database
and the vector store and counts; it writes nothing. Apply mode takes the
locks every writer of these points takes: the holder's context ``FOR SHARE``,
the rows ``FOR UPDATE NOWAIT``, the point-writer lock that keeps the orphan
sweep's delete pass out while a point is written ahead of its row; a row
another transaction holds is skipped and counted (``skipped``), and a later
run takes it. Each row runs in its own SAVEPOINT and commits on its own, so
stopping a run is safe and a rerun only finds what is left (a second run over
a repaired deployment counts nothing).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.qdrant import retrieve_vectors_and_contexts
from models.auth import Context
from models.memory import Memory
from services.context_routing import resolve_collection_name, resolve_context_routing
from services.embedding_service import EmbeddingService
from services.resource_indexer import (
    ResourceIndexer,
    context_holds_slug,
    detach_resource_rows,
)
from utils.logger import get_logger

logger = get_logger(__name__)

PAGE_SIZE = 200

Progress = Callable[[str], None]


@dataclass
class HolderRepair:
    """What a run found (plan) or did (apply). Counts only: no ids, no content."""

    dry_run: bool
    contexts: int = 0
    released_rows: int = 0
    shared_points: int = 0
    foreign_points: int = 0
    missing_points: int = 0
    detached: int = 0
    rebuilt: int = 0
    skipped: int = 0
    failed: int = 0
    last_context: UUID | None = None
    stopped: str | None = None
    seconds: float = 0.0

    @property
    def found(self) -> int:
        return self.released_rows + self.shared_points + self.foreign_points

    @property
    def repaired(self) -> int:
        return self.detached + self.rebuilt


@dataclass(frozen=True)
class _Row:
    memory_id: UUID
    point_id: UUID
    resource_id: str


async def _contexts(
    db: AsyncSession, context_id: UUID | None, after: UUID | None, limit: int | None
) -> tuple[list[UUID], bool]:
    """The live contexts of this run, by id, and whether more remain past ``limit``."""
    if context_id is not None:
        return [context_id], False
    stmt = select(Context.id).where(Context.deleted_at.is_(None)).order_by(Context.id)
    if after is not None:
        stmt = stmt.where(Context.id > after)
    if limit is not None:
        stmt = stmt.limit(limit + 1)
    ids = list((await db.execute(stmt)).scalars().all())
    if limit is not None and len(ids) > limit:
        return ids[:limit], True
    return ids, False


async def _page(db: AsyncSession, context_id: UUID, after: UUID | None) -> list[_Row]:
    """Live rows of the context that name a point other than their own id."""
    stmt = select(Memory.id, Memory.summary_embedding_id, Memory.resource_id).where(
        Memory.context_id == context_id,
        Memory.deleted_at.is_(None),
        Memory.resource_id.is_not(None),
        Memory.summary_embedding_id.is_not(None),
        Memory.summary_embedding_id != Memory.id,
    )
    if after is not None:
        stmt = stmt.where(Memory.id > after)
    rows = await db.execute(stmt.order_by(Memory.id).limit(PAGE_SIZE))
    return [_Row(row.id, row.summary_embedding_id, row.resource_id) for row in rows]


async def _named_elsewhere(db: AsyncSession, context_id: UUID, points: Sequence[UUID]) -> set[UUID]:
    """Those of ``points`` a live row of another context names."""
    if not points:
        return set()
    rows = await db.execute(
        select(Memory.summary_embedding_id).where(
            Memory.summary_embedding_id.in_(list(points)),
            Memory.deleted_at.is_(None),
            Memory.context_id != context_id,
        )
    )
    return {point for point in rows.scalars().all() if point is not None}


async def _classify(
    db: AsyncSession, context_id: UUID, slug: str | None, rows: list[_Row]
) -> dict[str, list[_Row]]:
    """Sort a page into ``released`` / ``shared`` / ``foreign`` / ``missing`` (the rest is fine)."""
    out: dict[str, list[_Row]] = {"released": [], "shared": [], "foreign": [], "missing": []}
    held = [row for row in rows if row.resource_id == slug]
    out["released"] = [row for row in rows if row.resource_id != slug]
    if not held:
        return out
    collection = await resolve_collection_name(db, context_id)
    found = await retrieve_vectors_and_contexts(
        collection, sorted({str(row.point_id) for row in held})
    )
    elsewhere = await _named_elsewhere(db, context_id, [row.point_id for row in held])
    for row in held:
        point = found.get(str(row.point_id))
        if point is None:
            out["missing"].append(row)
        elif point[1] != str(context_id):
            out["foreign"].append(row)
        elif row.point_id in elsewhere:
            out["shared"].append(row)
    return out


async def _attempt(db: AsyncSession, result: HolderRepair, what: str, work: Any) -> None:
    """Run one row's repair in a SAVEPOINT and commit it.

    A busy row is skipped, a failure counted. The commit after each row ends
    its locks, the point-writer lock among them, so the orphan sweep's
    delete pass never waits for more than one row's repair (an embedding
    request at most).
    """
    from services.sync_feed_provenance import sync_lock_busy

    try:
        async with db.begin_nested():
            changed = await work()
    except Exception as e:  # noqa: BLE001 — one row never stops the run
        await db.commit()
        if sync_lock_busy(e):
            result.skipped += 1
        else:
            result.failed += 1
            logger.warning(
                "resource_point_holder_repair_failed",
                step=what,
                error_class=type(e.__cause__ or e).__name__,
            )
        return
    await db.commit()
    if changed:
        if what == "rebuild":
            result.rebuilt += 1
        else:
            result.detached += changed


async def _repair_context(
    db: AsyncSession, context_id: UUID, result: HolderRepair, progress: Progress | None
) -> None:
    context = await db.get(Context, context_id, populate_existing=True)
    if context is None or context.deleted_at is not None:
        return
    slug = context.resource_id
    after: UUID | None = None
    indexer: ResourceIndexer | None = None
    while True:
        rows = await _page(db, context_id, after)
        if not rows:
            break
        after = rows[-1].memory_id
        groups = await _classify(db, context_id, slug, rows)
        result.released_rows += len(groups["released"])
        result.shared_points += len(groups["shared"])
        result.foreign_points += len(groups["foreign"])
        result.missing_points += len(groups["missing"])
        if result.dry_run:
            await db.rollback()
            continue

        for row in groups["released"]:

            async def detach_released(row: _Row = row) -> int:
                return await detach_resource_rows(
                    db, row.point_id, holder_context_id=None, memory_ids=[row.memory_id]
                )

            await _attempt(db, result, "released", detach_released)

        for row in groups["shared"]:

            async def detach_others(row: _Row = row) -> int:
                if slug is None or not await context_holds_slug(db, context_id, slug):
                    return 0
                return await detach_resource_rows(db, row.point_id, holder_context_id=context_id)

            await _attempt(db, result, "shared", detach_others)

        if groups["foreign"]:
            collection, embedding_service = await resolve_context_routing(
                db, context_id, default_service=EmbeddingService(db)
            )
            if indexer is None:
                indexer = ResourceIndexer(db)
            for row in groups["foreign"]:

                async def rebuild(
                    row: _Row = row,
                    indexer: ResourceIndexer = indexer,
                    collection: str = collection,
                    embedding_service: EmbeddingService = embedding_service,
                ) -> int:
                    memory = await db.get(Memory, row.memory_id, populate_existing=True)
                    if memory is None or memory.deleted_at is not None:
                        return 0
                    await indexer.rebuild_point(
                        memory, collection_name=collection, embedding_service=embedding_service
                    )
                    return 1

                await _attempt(db, result, "rebuild", rebuild)
        await db.commit()
        if progress is not None:
            progress(
                f"context {context_id}: detached {result.detached}, rebuilt {result.rebuilt}, "
                f"skipped {result.skipped}, failed {result.failed}"
            )


async def run_holder_repair(
    db: AsyncSession,
    *,
    context_id: UUID | None = None,
    after: UUID | None = None,
    limit: int | None = None,
    dry_run: bool = True,
    progress: Progress | None = None,
) -> HolderRepair:
    """Plan or run the repair.

    Args:
        db: A session; apply commits row by row.
        context_id: One context; ``None`` = every live context, by id.
        after: Every context: only those with an id above this (``resume``).
        limit: Every context: at most this many contexts in this run.
        dry_run: Count only.
        progress: Called with a line after each applied page.

    Returns:
        The counts; ``last_context`` is the resume point.
    """
    started = time.monotonic()
    result = HolderRepair(dry_run=dry_run)
    contexts, more = await _contexts(db, context_id, after, limit)
    for current in contexts:
        await _repair_context(db, current, result, progress)
        await db.commit()
        result.contexts += 1
        result.last_context = current
    if more:
        result.stopped = "limit"
    result.seconds = time.monotonic() - started
    return result
