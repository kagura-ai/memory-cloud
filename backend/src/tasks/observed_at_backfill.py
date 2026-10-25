"""One-shot backfill of the vector-store ``observed_at`` payload (#2330).

Points written before the ``observed_at`` payload existed carry none, and
recall's ``observed_after`` / ``observed_before`` filters use must semantics,
so a memory whose ``details.observed_at`` predates the payload would never
match them. This walk reads the f16 generated column (the validated source,
the 20-char UTC form) and sets the payload on the point each row names
(``Memory.point_id``), like the location backfill in ``tasks/geo_backfill``.

Run by hand (``python -m src.cli.observed_at_backfill``), not at startup:
the population of rows with an event time grows with use, and a per-boot
sweep over it would not stay cheap. Idempotent: setting the same value again
changes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.memory import Memory
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class ObservedAtBackfill:
    """What a run found (plan) or wrote (apply)."""

    dry_run: bool
    rows: int = 0
    written: int = 0
    shared_skipped: int = 0
    failed: list[UUID] = field(default_factory=list)


def _rows_query(workspace_id: UUID | None):  # noqa: ANN202 - a SQLAlchemy Select
    query = (
        select(Memory.id, Memory.summary_embedding_id, Memory.context_id, Memory.observed_at)
        .where(
            Memory.observed_at.is_not(None),
            Memory.deleted_at.is_(None),
            Memory.embedding_status == "success",
        )
        .order_by(Memory.id)
    )
    if workspace_id is not None:
        query = query.where(Memory.workspace_id == workspace_id)
    return query


async def backfill_observed_at_payloads(
    db: AsyncSession, *, dry_run: bool, workspace_id: UUID | None = None
) -> ObservedAtBackfill:
    """Set the ``observed_at`` payload of every embedded row that has one.

    Args:
        db: The session (read only; the writes go to the vector store).
        dry_run: Count the rows and write nothing.
        workspace_id: Limit the run to one workspace (a canary).

    Returns:
        The counts. A row whose point fails is logged by id and counted in
        ``failed``; the walk goes on.
    """
    from db.qdrant import update_memory_payload_in_qdrant
    from services.context_routing import resolve_collection_name
    from services.resource_indexer import unshared_point_id

    rows = (await db.execute(_rows_query(workspace_id))).all()
    result = ObservedAtBackfill(dry_run=dry_run, rows=len(rows))
    if dry_run:
        return result

    collections: dict[UUID | None, str] = {}
    for row in rows:
        try:
            point_id = await unshared_point_id(db, row.id, row.summary_embedding_id or row.id)
            if point_id is None:
                # A document point another live row names too is left as it
                # is, like the metadata patch (#2100).
                result.shared_skipped += 1
                continue
            collection = collections.get(row.context_id)
            if collection is None:
                collection = await resolve_collection_name(db, row.context_id)
                collections[row.context_id] = collection
            await update_memory_payload_in_qdrant(
                memory_id=point_id,
                payload_updates={"observed_at": row.observed_at},
                collection_name=collection,
            )
            result.written += 1
        except Exception as e:  # noqa: BLE001 — best-effort per row
            result.failed.append(row.id)
            logger.warning("observed_at_backfill_row_failed", memory_id=str(row.id), error=str(e))
    return result
