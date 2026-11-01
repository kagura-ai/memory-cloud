"""One-shot backfill of ``type`` / ``tags`` / ``context_summary`` on resource points (#2254).

Since v0.122.0 (#2160) the resource indexer writes the owning row's ``type``,
``tags`` and ``context_summary`` into the point of each document version, so
recall's payload filters match it. Points indexed earlier carry none of the
three and gain them only through a Sleep reindex, an embedding migration or a
metadata edit of the row (#2100): until then ``filters.type="resource_data"``
misses them (``must`` semantics on a missing key).

This walk takes every live, embedded row that owns its resource point
(``names_own_resource_point``: ``summary_embedding_id`` is the uuid5 of the
row's ``resource_id:doc_id:vN``) and sets the three keys on that point from
the row, the way ``rebuild_point`` builds them (``context_summary`` through
``normalize_for_search``, like every other writer of that key). Set-payload
only: the point's vectors and its other keys stay as they are, nothing is
deleted.

Run by hand (``python -m src.cli.resource_point_metadata_backfill``), once
after the upgrade, not at startup. Idempotent: setting the same values again
changes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.memory import Memory
from utils.logger import get_logger
from utils.text import normalize_for_search

logger = get_logger(__name__)


@dataclass
class ResourcePointMetadataBackfill:
    """What a run found (plan) or wrote (apply)."""

    dry_run: bool
    rows: int = 0
    contexts: int = 0
    written: int = 0
    shared_skipped: int = 0
    gone_skipped: int = 0
    failed: list[UUID] = field(default_factory=list)


def _live_embedded() -> tuple[Any, ...]:
    """A live row with a written point; one an erasure suppressed has none."""
    return (
        Memory.deleted_at.is_(None),
        Memory.embedding_status == "success",
        Memory.embedding_suppressed_at.is_(None),
    )


def _rows_query(workspace_id: UUID | None):  # noqa: ANN202 - a SQLAlchemy Select
    # The natural key and a point id other than the row's own: the candidates
    # ``names_own_resource_point`` is then checked on (uuid5 is not computed
    # in SQL). Rows whose embedding an erasure suppressed have no point to
    # write to.
    query = (
        select(
            Memory.id,
            Memory.summary_embedding_id,
            Memory.context_id,
            Memory.resource_id,
            Memory.resource_doc_id,
            Memory.resource_version,
        )
        .where(
            *_live_embedded(),
            Memory.resource_id.is_not(None),
            Memory.resource_doc_id.is_not(None),
            Memory.resource_version.is_not(None),
            Memory.summary_embedding_id.is_not(None),
            Memory.summary_embedding_id != Memory.id,
        )
        .order_by(Memory.id)
    )
    if workspace_id is not None:
        query = query.where(Memory.workspace_id == workspace_id)
    return query


async def _current_values(db: AsyncSession, memory_id: UUID, point_id: UUID) -> Any | None:
    """The row's ``type`` / ``tags`` / ``context_summary`` now, or ``None``.

    Read right before the write, not from the walk's opening snapshot: a row
    edited during the run (its metadata patch already wrote the new values to
    the point) must not get its old values written back, and a row deleted,
    erased or re-pointed meanwhile must get nothing. In a SAVEPOINT, like
    ``unshared_point_id``, so a failed read fails only its row.
    """
    async with db.begin_nested():
        return (
            await db.execute(
                select(Memory.type, Memory.tags, Memory.context_summary).where(
                    Memory.id == memory_id,
                    Memory.summary_embedding_id == point_id,
                    *_live_embedded(),
                )
            )
        ).one_or_none()


def _payload(row: Any) -> dict[str, Any]:
    """The three keys as ``build_resource_point`` writes them from the row."""
    payload: dict[str, Any] = {"type": row.type, "tags": list(row.tags or [])}
    if row.context_summary is not None:
        payload["context_summary"] = normalize_for_search(row.context_summary)
    return payload


async def backfill_resource_point_metadata(
    db: AsyncSession, *, dry_run: bool, workspace_id: UUID | None = None
) -> ResourcePointMetadataBackfill:
    """Set ``type`` / ``tags`` / ``context_summary`` on every row's own resource point.

    Args:
        db: The session (read only; the writes go to the vector store).
        dry_run: Count the rows and write nothing.
        workspace_id: Limit the run to one workspace (a canary).

    Returns:
        The counts. A row whose point fails (a point that is gone, a
        vector-store error) is logged by id and counted in ``failed``; the
        walk goes on. A point another live row names too is left as it is
        and counted in ``shared_skipped``; a row deleted, erased or
        re-pointed since the walk started is counted in ``gone_skipped``.
        Accepted edge: an edit that commits between a row's read and its
        write and writes its own payload first is overwritten with the
        values just read; the next run (or the row's next edit) corrects it.
    """
    from db.qdrant import update_memory_payload_in_qdrant
    from services.context_routing import resolve_collection_name
    from services.resource_indexer import names_own_resource_point, unshared_point_id

    rows = [
        row
        for row in (await db.execute(_rows_query(workspace_id))).all()
        if names_own_resource_point(
            row.summary_embedding_id, row.resource_id, row.resource_doc_id, row.resource_version
        )
    ]
    result = ResourcePointMetadataBackfill(
        dry_run=dry_run, rows=len(rows), contexts=len({row.context_id for row in rows})
    )
    if dry_run:
        return result

    collections: dict[UUID, str] = {}
    for row in rows:
        try:
            point_id = await unshared_point_id(db, row.id, row.summary_embedding_id)
            if point_id is None:
                # One row's values must not rewrite the filters of another
                # live row naming the same point, like the metadata patch.
                result.shared_skipped += 1
                continue
            current = await _current_values(db, row.id, point_id)
            if current is None:
                # Deleted, erased or re-pointed since the walk started.
                result.gone_skipped += 1
                continue
            collection = collections.get(row.context_id)
            if collection is None:
                # A SAVEPOINT like the other per-row reads: a failed lookup
                # must not leave the session's transaction aborted for the
                # rows after it.
                async with db.begin_nested():
                    collection = await resolve_collection_name(db, row.context_id)
                collections[row.context_id] = collection
            await update_memory_payload_in_qdrant(
                memory_id=point_id,
                payload_updates=_payload(current),
                collection_name=collection,
                # An indexer point is not the row's id: chunk points (keyed by
                # the row id) never hang off it.
                cascade_chunks=False,
            )
            result.written += 1
        except Exception as e:  # noqa: BLE001 — best-effort per row
            result.failed.append(row.id)
            logger.warning(
                "resource_point_metadata_backfill_row_failed",
                memory_id=str(row.id),
                error=str(e),
            )
    return result
