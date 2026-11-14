"""Server outbox receipts, never client metadata, prove managed event ownership."""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import Select, Uuid, case, cast, exists, func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession, AsyncSessionTransaction

from db.point_writer_lock import hold_point_writer_lock
from models.auth import Context
from models.memory import Memory
from models.resource import Resource, ResourceEvent
from models.sync_feed import SyncIndexOutbox, SyncRecord
from models.sync_feed_schemas import MAX_RUN_BYTES
from services.sync_feed_authority import locked
from utils.exceptions import ConflictError


@dataclass(frozen=True)
class SyncMemoryProof:
    """Only these already-locked rows may be mutated by this event/transaction."""

    event_id: int
    transaction: AsyncSessionTransaction | None
    memory_ids: frozenset[UUID]


def assert_sync_memory_ids(
    db: AsyncSession, event: ResourceEvent, memories: Sequence[Memory]
) -> None:
    """Refuse a late details-move collision before vector/current/retirement writes."""
    if not isinstance(event.event_metadata, dict) or "sync_run_id" not in event.event_metadata:
        return
    proof = db.info.get("sync_memory_proof")
    if (
        not isinstance(proof, SyncMemoryProof)
        or proof.event_id != event.id
        or proof.transaction is not db.get_transaction()
        or any(memory.id not in proof.memory_ids for memory in memories)
    ):
        raise ConflictError("Synchronization memory ownership changed")


def sync_candidate_lock(query: Select, event: ResourceEvent) -> Select:
    """Refresh and lock actual sync candidates before comparing their row IDs."""
    if isinstance(event.event_metadata, dict) and "sync_run_id" in event.event_metadata:
        return (
            query.limit(20001)
            .with_for_update(nowait=True)
            .execution_options(populate_existing=True)
        )
    return query


def enrolled_event():
    """Correlated proof that the event was committed by the feed transaction."""
    run_text = ResourceEvent.event_metadata["sync_run_id"].as_string()
    run_uuid = cast(
        case(
            (
                run_text.op("~")(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"),
                run_text,
            ),
            else_=None,
        ),
        Uuid(),
    )
    return exists(
        select(SyncIndexOutbox.run_id).where(
            SyncIndexOutbox.resource_pk == ResourceEvent.resource_pk,
            SyncIndexOutbox.run_id == run_uuid,
            SyncIndexOutbox.event_ids.op("@>")(func.jsonb_build_array(ResourceEvent.id)),
        )
    )


async def assert_managed_memories(
    db: AsyncSession,
    context: Context,
    resource_id: str,
    resource_pk: UUID,
    docs: list[str],
    *,
    version: int | None = None,
) -> list[Memory]:
    """Hold memory rows and reject another author's or a manual writer's content."""
    query = select(Memory).where(
        Memory.context_id == context.id,
        Memory.resource_id == resource_id,
        Memory.resource_doc_id.in_(docs),
    )
    # Upsert's retirement also mutates old tombstones. Fence every row it can
    # touch, not only currently published versions; the result remains bounded.
    if version is not None:
        query = query.where(Memory.resource_version == version)
    summaries = (
        await db.execute(
            query.with_only_columns(
                Memory.id,
                Memory.user_id,
                Memory.version,
                Memory.source,
                func.coalesce(func.octet_length(Memory.content), 0),
            )
            .limit(20001)
            .with_for_update(read=True, nowait=True)
        )
    ).all()
    if len(summaries) > 20000 or sum(row[4] for row in summaries) > 2 * MAX_RUN_BYTES:
        raise ConflictError("Synchronization ownership check limit exceeded")
    if any(
        row[1] != str(context.created_by) or row[2] != 1 or row[3] != "resource_ingest"
        for row in summaries
    ):
        raise ConflictError("Synchronization document was independently edited")
    if not summaries:
        return []
    # Reject foreign/oversized rows before loading any bodies into the worker.
    memories = list(
        (
            await db.scalars(
                select(Memory)
                .where(Memory.id.in_([row[0] for row in summaries]))
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    pairs = list({(memory.resource_doc_id, memory.resource_version) for memory in memories})
    by_version = {}
    for start in range(0, len(pairs), 1000):
        originals = (
            await db.execute(
                select(ResourceEvent.doc_id, ResourceEvent.version, ResourceEvent.payload)
                .where(
                    ResourceEvent.resource_pk == resource_pk,
                    tuple_(ResourceEvent.doc_id, ResourceEvent.version).in_(
                        pairs[start : start + 1000]
                    ),
                    ResourceEvent.op == "upsert",
                    enrolled_event(),
                )
                .limit(1001)
            )
        ).all()
        by_version.update({(doc, version): payload for doc, version, payload in originals})
    for memory in memories:
        try:
            payload = json.loads(memory.content or "null")
        except (ValueError, TypeError):
            raise ConflictError("Synchronization document ownership changed") from None
        if (
            memory.user_id != str(context.created_by)
            or memory.version != 1
            or memory.source != "resource_ingest"
            or (memory.resource_doc_id, memory.resource_version) not in by_version
            or payload != by_version[(memory.resource_doc_id, memory.resource_version)]
        ):
            raise ConflictError("Synchronization document was independently edited")
    return memories


async def guard_sync_event(db: AsyncSession, event: ResourceEvent, context: Context) -> bool:
    """Fence independently edited rows before any asynchronous vector side effect.

    Normal resource ingestion is unchanged. A sync event must have a real
    server receipt; a caller-supplied sync_run_id alone conveys no authority.
    The Resource/Context UPDATE locks also fence new Memory FK insertions.
    """
    if not isinstance(event.event_metadata, dict) or "sync_run_id" not in event.event_metadata:
        return True
    db.info.pop("sync_memory_proof", None)
    if event.resource_pk is None or event.version is None:
        raise ConflictError("Invalid synchronization event")
    try:
        run_id = UUID(event.event_metadata["sync_run_id"])
    except (ValueError, TypeError, AttributeError):
        raise ConflictError("Invalid synchronization receipt") from None
    receipt = await db.scalar(
        select(SyncIndexOutbox)
        .where(
            SyncIndexOutbox.run_id == run_id,
            SyncIndexOutbox.resource_pk == event.resource_pk,
            SyncIndexOutbox.context_id == context.id,
            SyncIndexOutbox.event_ids.op("@>")(func.jsonb_build_array(event.id)),
        )
        .with_for_update(read=True, nowait=True)
    )
    if receipt is None or str(receipt.run_id) != event.event_metadata["sync_run_id"]:
        raise ConflictError("Unregistered synchronization event")
    # Match global vector-writer order before taking Context/Memory locks.
    await hold_point_writer_lock(db)
    resource = await locked(db, Resource, Resource.id == event.resource_pk, write=True)
    fresh = await locked(db, Context, Context.id == context.id, write=True)
    if (
        resource is None
        or fresh is None
        or resource.workspace_id != context.workspace_id
        or resource.resource_id != event.resource_id
        or fresh.resource_id != event.resource_id
        or fresh.deleted_at is not None
        or fresh.is_private
        or not fresh.is_public
    ):
        raise ConflictError("Synchronization target changed")
    record = await locked(
        db, SyncRecord, SyncRecord.resource_pk == resource.id, SyncRecord.doc_id == event.doc_id
    )
    if record is None:
        raise ConflictError("Synchronization canonical record is missing")
    if event.op == "upsert":
        # An obsolete snapshot must never reappear after a newer commit/delete.
        if record.payload is None or record.version != event.version:
            return False
        if record.payload != event.payload:
            raise ConflictError("Synchronization event content changed")
    memories = await assert_managed_memories(
        db,
        fresh,
        event.resource_id,
        resource.id,
        [event.doc_id],
        version=event.version if event.op == "delete" else None,
    )
    db.info["sync_memory_proof"] = SyncMemoryProof(
        event.id, db.get_transaction(), frozenset(memory.id for memory in memories)
    )
    return True


async def obsolete_sync_event_ids(db: AsyncSession, events: Sequence[ResourceEvent]) -> set[int]:
    """A monotonic canonical version proves obsolete upserts cannot create rows.

    Use a per-event mask, including across alternating upsert/delete history;
    an obsolete event never enters capacity or creation-quota accounting.
    The batch still processes its receipt and advances the event cursor.
    """
    ids = [
        event.id
        for event in events
        if event.op == "upsert"
        and isinstance(event.event_metadata, dict)
        and "sync_run_id" in event.event_metadata
    ]
    if not ids:
        return set()
    return set(
        (
            await db.scalars(
                select(ResourceEvent.id)
                .join(
                    SyncRecord,
                    (SyncRecord.resource_pk == ResourceEvent.resource_pk)
                    & (SyncRecord.doc_id == ResourceEvent.doc_id),
                )
                .where(
                    ResourceEvent.id.in_(ids),
                    enrolled_event(),
                    or_(SyncRecord.payload.is_(None), SyncRecord.version > ResourceEvent.version),
                )
            )
        ).all()
    )
