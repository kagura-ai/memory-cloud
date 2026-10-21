"""One-shot startup backfill of the Qdrant ``location`` geo payload (#1332).

Points embedded before the geo payload existed carry no ``location`` field,
and ``filters.near`` uses must semantics — without a backfill every
pre-#1332 located memory would silently vanish from geo-filtered recall
(the #1229 failure shape). This sweep reads the e69 generated columns (the
single validated source) and rewrites the payload for every located row,
on the point the row names (``Memory.point_id``): its own id for an API row,
its document's point for a ResourceIndexer row (#1829). Resource points carry
``location`` since #2100 / #2160, so the reconcile matches a point to its row
by either id (#2218).

Cost note: the sweep is unconditional-idempotent (set_payload of the same
value is a no-op server-side) and runs as a fire-and-forget task at API
startup. Production currently has zero located rows, so this is a no-op
boot log line; an install accumulating many located memories pays one
payload write per located row per boot — revisit with a sync watermark if
that population ever grows large.
"""

from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.memory import Memory
from utils.logger import get_logger

logger = get_logger(__name__)


async def located_point_ids(db: AsyncSession, point_ids: list[UUID]) -> set[UUID]:
    """The ids among ``point_ids`` that a live, located row names (#2218).

    A row names its point by ``Memory.point_id`` (``summary_embedding_id``,
    else its own id), so a ResourceIndexer row's document point is found by
    ``summary_embedding_id``, served by the partial index
    idx_memories_summary_embedding_live (#1869). A point several rows name
    (a document shared across contexts) counts as located while any of them
    is.
    """
    if not point_ids:
        return set()
    rows = await db.execute(
        select(Memory.id, Memory.summary_embedding_id).where(
            or_(Memory.id.in_(point_ids), Memory.summary_embedding_id.in_(point_ids)),
            Memory.location_lat.is_not(None),
            Memory.location_lon.is_not(None),
            Memory.deleted_at.is_(None),
        )
    )
    return {summary_point or memory_id for memory_id, summary_point in rows}


async def backfill_location_payloads(db: AsyncSession) -> int:
    """Write the ``location`` geo payload for every located, embedded row.

    Returns the number of points written. Never logs coordinates — count
    and memory ids only (the geo privacy invariant from services/geo_memory).
    """
    from db.qdrant import update_memory_payload_in_qdrant
    from services.context_routing import resolve_collection_name
    from services.resource_indexer import unshared_point_id

    rows = (
        await db.execute(
            select(
                Memory.id,
                Memory.summary_embedding_id,
                Memory.context_id,
                Memory.location_lat,
                Memory.location_lon,
            ).where(
                Memory.location_lat.is_not(None),
                Memory.location_lon.is_not(None),
                Memory.deleted_at.is_(None),
                Memory.embedding_status == "success",
            )
        )
    ).all()

    count = 0
    failed = 0
    # resolve_collection_name is an uncached per-call lookup — memoize per
    # context so N located rows in one context cost one resolution.
    collections: dict = {}
    for row in rows:
        # Per-row isolation: one poisoned row (missing point, transient
        # Qdrant error) must not strand the rest of the sweep until the
        # next boot. Ids only in logs — never coordinates.
        try:
            # #2218: the point the row names (``Memory.point_id``). An own-id
            # point skips the sharing lookup; a document point another live
            # row names too is left as it is, like the metadata patch (#2100).
            point_id = await unshared_point_id(db, row.id, row.summary_embedding_id or row.id)
            if point_id is None:
                logger.info("geo_payload_backfill_shared_point_skipped", memory_id=str(row.id))
                continue
            collection = collections.get(row.context_id)
            if collection is None:
                collection = await resolve_collection_name(db, row.context_id)
                collections[row.context_id] = collection
            await update_memory_payload_in_qdrant(
                memory_id=point_id,
                payload_updates={"location": {"lat": row.location_lat, "lon": row.location_lon}},
                collection_name=collection,
                # #2015: chunk points copy location when they are written.
                cascade_chunks=False,
            )
            count += 1
        except Exception as e:  # noqa: BLE001 — best-effort per row
            failed += 1
            logger.warning(
                "geo_payload_backfill_row_failed",
                memory_id=str(row.id),
                error=str(e),
            )
    if failed:
        logger.warning("geo_payload_backfill_partial", written=count, failed=failed)
    return count


async def reconcile_stale_location_payloads(db: AsyncSession) -> int:
    """Clear ``location`` payloads whose PG row is no longer located.

    The forward backfill only ADDs payloads. A details write that removed
    the location pairs with a Qdrant ``delete_payload`` — but patch_memory's
    metadata-only path commits PG first and swallows a Qdrant failure by
    design (#439), so a stale coordinate could otherwise linger forever and
    keep matching ``filters.near`` at a location the user removed. This
    sweep scrolls each collection's located points and clears any that no
    live, located row names (``located_point_ids``: by its own id or, for a
    resource point, by ``summary_embedding_id``, #2218). Returns the number
    cleared.

    Qdrant backend only — the LanceDB preview backend exposes no scroll
    surface (documented preview limitation).
    """
    from qdrant_client.models import IsEmptyCondition, PayloadField

    from db.qdrant import (
        get_qdrant_client,
        summary_points_filter,
        update_memory_payload_in_qdrant,
    )
    from db.vector_store import get_active_store
    from services.content_chunk_writer import chunk_cascade_needed

    if get_active_store() is not None:
        return 0

    client = get_qdrant_client()
    collections = [
        c.name
        for c in (await client.get_collections()).collections
        if c.name.startswith("kagura_memories")
    ]
    # #2015: summary points only — a content chunk point (chunk_of set) shares
    # its memory's location and is cleared by that point's payload cascade.
    located_filter = summary_points_filter(
        must_not=[IsEmptyCondition(is_empty=PayloadField(key="location"))],
    )
    cleared = 0
    # #2015: whether a context's points need the chunk cascade (memoized).
    cascade_by_context: dict[str, bool] = {}
    for collection in collections:
        offset = None
        while True:
            points, offset = await client.scroll(
                collection_name=collection,
                scroll_filter=located_filter,
                with_payload=["context_id"],
                with_vectors=False,
                limit=256,
                offset=offset,
            )
            if not points:
                break
            # Point ids are Memory.id for memory points and the document's
            # uuid5 for ResourceIndexer points (#1829); both are UUIDs, but
            # guard the parse anyway.
            point_ids: list[UUID] = []
            context_of: dict[UUID, str | None] = {}
            for p in points:
                try:
                    pid = UUID(str(p.id))
                except ValueError:
                    continue
                point_ids.append(pid)
                payload = p.payload if isinstance(p.payload, dict) else {}
                context_of[pid] = payload.get("context_id")
            if point_ids:
                located = await located_point_ids(db, point_ids)
                for pid in point_ids:
                    if pid in located:
                        continue
                    try:
                        context_id = context_of.get(pid)
                        if context_id is None:
                            cascade = True  # unknown context: the safe side
                        else:
                            if context_id not in cascade_by_context:
                                cascade_by_context[context_id] = await chunk_cascade_needed(
                                    db, UUID(context_id)
                                )
                            cascade = cascade_by_context[context_id]
                        await update_memory_payload_in_qdrant(
                            memory_id=pid,
                            payload_updates={},
                            collection_name=collection,
                            delete_keys=["location"],
                            cascade_chunks=cascade,
                        )
                        cleared += 1
                    except Exception as e:  # noqa: BLE001 — best-effort per row
                        logger.warning(
                            "geo_payload_reconcile_row_failed",
                            memory_id=str(pid),
                            error=str(e),
                        )
            if offset is None:
                break
    return cleared


async def run_location_payload_backfill() -> None:
    """Fire-and-forget lifespan entry point — best-effort, never raises."""
    from db.base import get_db

    try:
        async for db in get_db():
            count = await backfill_location_payloads(db)
            cleared = await reconcile_stale_location_payloads(db)
            if count or cleared:
                logger.info("geo_payload_backfill_completed", points=count, cleared=cleared)
            else:
                logger.debug("geo_payload_backfill_noop")
    except Exception as e:  # noqa: BLE001 — startup must never die on backfill
        logger.error("geo_payload_backfill_failed", error=str(e))
