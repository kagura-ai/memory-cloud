"""Bounded staging and preparation; never publishes events before commit."""

from datetime import timedelta
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models.management_schemas import digest
from models.sync_feed import SyncChunk, SyncFeed, SyncRecord, SyncRun
from models.sync_feed_schemas import (
    MAX_RUN_BYTES,
    SCHEMA_HASH,
    ChunkBody,
    RunManifest,
    destructive,
    snapshot_digest,
)
from services.sync_credentials import CredentialAuthority
from services.sync_feed_authority import FeedFence, current_schema, locked
from utils.datetime import to_utc_iso, utcnow
from utils.exceptions import ConflictError, NotFoundException


def generations(authority: CredentialAuthority) -> list[int]:
    """Credential rotation does not participate in the logical run identity."""
    return [
        authority.service.generation,
        authority.client.generation,
        authority.installation.generation,
    ]


async def schema_matches(db: AsyncSession, authority: CredentialAuthority) -> None:
    """The dedicated schema is approved during human feed configuration only."""
    schema = await current_schema(db, authority.installation)
    if schema is None or digest(schema.field_definitions) != SCHEMA_HASH:
        raise ConflictError("Approved synchronization schema changed")


async def feed_for(db: AsyncSession, iid: UUID, fid: UUID) -> SyncFeed:
    """Resolve the exact feed inside this installation under the workspace lock."""
    feed = await locked(
        db, SyncFeed, SyncFeed.id == fid, SyncFeed.installation_id == iid, write=True
    )
    if feed is None:
        raise NotFoundException("Synchronization feed")
    return feed


async def run_for(
    db: AsyncSession, authority: CredentialAuthority, rid: UUID
) -> tuple[SyncRun, SyncFeed]:
    """A foreign run is indistinguishable from a missing run."""
    row = await db.scalar(
        select(SyncRun)
        .join(SyncFeed)
        .where(SyncRun.id == rid, SyncFeed.installation_id == authority.installation.id)
    )
    if row is None:
        raise NotFoundException("Synchronization run")
    feed = await feed_for(db, authority.installation.id, row.feed_id)
    row = await locked(db, SyncRun, SyncRun.id == rid, write=True)
    if row is None:
        raise NotFoundException("Synchronization run")
    return row, feed


async def editable(
    db: AsyncSession, authority: CredentialAuthority, row: SyncRun, feed: SyncFeed, fence: FeedFence
) -> None:
    """Bind every mutation to current schema, generation, config and baseline."""
    if row.state not in {"uploading", "prepared"} or utcnow() >= row.expires_at:
        raise ConflictError("Synchronization run is no longer editable")
    if (
        row.generations != generations(authority)
        or feed.config_hash != row.manifest["config_hash"]
        or feed.config_version != row.manifest["config_version"]
        or feed.revision != row.base_revision
    ):
        raise ConflictError("Synchronization source or authority changed")
    await schema_matches(db, authority)
    schema = await current_schema(db, authority.installation)
    if schema is None or schema.schema_version != row.manifest["schema_version"]:
        raise ConflictError("Approved synchronization schema version changed")
    fence.deadlines.append(row.expires_at)


async def create_run(
    db: AsyncSession, authority: CredentialAuthority, body: RunManifest
) -> SyncRun:
    """Stable operation identity survives retries, lost responses and new tokens."""
    feed = await feed_for(db, authority.installation.id, body.feed_id)
    manifest = body.model_dump(mode="json")
    request_hash = digest(manifest)
    old = await db.scalar(
        select(SyncRun).where(SyncRun.feed_id == feed.id, SyncRun.operation_id == body.operation_id)
    )
    if old is not None:
        if old.request_hash != request_hash:
            raise ConflictError("Operation identity was used with different content")
        return old
    if (
        feed.config_hash != body.config_hash
        or feed.config_version != body.config_version
        or body.schema_hash != SCHEMA_HASH
        or (body.records == 0) != (body.chunks == 0)
        or body.records < body.chunks
        or body.records > body.chunks * 100
    ):
        raise HTTPException(400, "invalid_manifest")
    await schema_matches(db, authority)
    pending = await db.scalar(
        select(func.count())
        .select_from(SyncRun)
        .join(SyncFeed)
        .where(
            SyncFeed.installation_id == authority.installation.id,
            SyncRun.state.in_(["uploading", "prepared"]),
            SyncRun.expires_at > utcnow(),
        )
    )
    schema = await current_schema(db, authority.installation)
    if schema is None or schema.schema_version != body.schema_version:
        raise ConflictError("Approved synchronization schema version changed")
    if pending is not None and pending >= 4:
        raise ConflictError("Too many active synchronization runs")
    row = SyncRun(
        feed_id=feed.id,
        operation_id=body.operation_id,
        request_hash=request_hash,
        manifest=manifest,
        generations=generations(authority),
        base_revision=feed.revision,
        state="uploading",
        expires_at=utcnow() + timedelta(hours=24),
    )
    db.add(row)
    await db.flush()
    return row


async def stage_chunk(
    db: AsyncSession,
    authority: CredentialAuthority,
    rid: UUID,
    index: int,
    body: ChunkBody,
    byte_count: int,
    fence: FeedFence,
) -> SyncRun:
    """Immutable chunk receipts; even a committed retry cannot change its body."""
    row, feed = await run_for(db, authority, rid)
    records = [record.model_dump(mode="json") for record in body.records]
    chunk_hash = digest(records)
    old = await db.get(SyncChunk, (rid, index))
    if old is not None:
        if old.digest != chunk_hash:
            raise ConflictError("Chunk identity was used with different content")
        return row
    await editable(db, authority, row, feed, fence)
    if row.state != "uploading" or not 0 <= index < row.manifest["chunks"]:
        raise ConflictError("Chunk is outside the declared upload")
    try:
        for record in body.records:
            record.public_payload(feed.kind)
    except ValueError:
        raise HTTPException(400, "invalid_record") from None
    total = (
        await db.scalar(
            select(func.coalesce(func.sum(SyncChunk.byte_count), 0)).where(SyncChunk.run_id == rid)
        )
    ) or 0
    if total + byte_count > MAX_RUN_BYTES:
        raise HTTPException(400, "run_too_large")
    accepted = (
        await db.scalar(
            select(func.coalesce(func.sum(SyncChunk.record_count), 0)).where(
                SyncChunk.run_id == rid
            )
        )
    ) or 0
    if accepted + len(records) > row.manifest["records"]:
        raise HTTPException(400, "too_many_records")
    db.add(
        SyncChunk(
            run_id=rid,
            chunk_index=index,
            digest=chunk_hash,
            byte_count=byte_count,
            record_count=len(records),
            records=records,
        )
    )
    await db.flush()
    return row


async def staged_records(db: AsyncSession, row: SyncRun) -> list[dict]:
    """Validate completeness and digest independent of upload boundaries."""
    chunks = (
        await db.scalars(
            select(SyncChunk).where(SyncChunk.run_id == row.id).order_by(SyncChunk.chunk_index)
        )
    ).all()
    if [chunk.chunk_index for chunk in chunks] != list(range(row.manifest["chunks"])) or any(
        chunk.records is None for chunk in chunks
    ):
        raise ConflictError("Synchronization upload is incomplete")
    records = [record for chunk in chunks for record in (chunk.records or [])]
    if (
        len(records) != row.manifest["records"]
        or len({record["external_id"] for record in records}) != len(records)
        or snapshot_digest(records) != row.manifest["snapshot_hash"]
    ):
        raise ConflictError("Synchronization snapshot does not match its manifest")
    return records


async def build_diff(db: AsyncSession, row: SyncRun, feed: SyncFeed) -> tuple[list[dict], int]:
    """Diff only the exact feed's canonical managed records, including tombstones."""
    from models.sync_feed_schemas import FeedRecord

    incoming = {
        item["external_id"]: FeedRecord.model_validate(item).public_payload(feed.kind)
        for item in await staged_records(db, row)
    }
    previous = {
        item.external_id: item
        for item in (
            await db.scalars(
                select(SyncRecord).where(
                    SyncRecord.feed_id == feed.id, SyncRecord.payload.is_not(None)
                )
            )
        ).all()
    }
    diff = []
    for external_id in sorted(previous.keys() | incoming.keys(), key=lambda value: value.encode()):
        before = previous[external_id].payload if external_id in previous else None
        after = incoming.get(external_id)
        if before != after:
            diff.append(
                {
                    "external_id": external_id,
                    "op": "delete" if after is None else "upsert",
                    "before_hash": digest(before),
                    "after_hash": digest(after),
                }
            )
    return diff, sum(item.payload is not None for item in previous.values())


async def prepare_run(
    db: AsyncSession, authority: CredentialAuthority, rid: UUID, fence: FeedFence
) -> SyncRun:
    """Refresh after expiry issues a new diff hash, invalidating prior human consent."""
    row, feed = await run_for(db, authority, rid)
    await editable(db, authority, row, feed, fence)
    if row.state == "prepared" and row.prepared_until is not None and utcnow() < row.prepared_until:
        return row
    diff, previous_count = await build_diff(db, row, feed)
    row.diff = diff
    row.prepared_until = min(row.expires_at, utcnow() + timedelta(minutes=15))
    row.diff_hash = digest(
        {
            "run": str(row.id),
            "manifest": row.manifest,
            "generations": row.generations,
            "baseline": row.base_revision,
            "diff": diff,
            "nonce": uuid4().hex,
        }
    )
    row.approval_id = row.approved_by = row.approval_epoch = None
    row.state = "prepared"
    row.requires_approval = destructive(diff, previous_count)
    return row


async def clear_staging(db: AsyncSession, row: SyncRun) -> None:
    """Payloads have a finite lifetime; immutable digest receipts remain queryable."""
    await db.execute(update(SyncChunk).where(SyncChunk.run_id == row.id).values(records=None))


async def abort_run(db: AsyncSession, authority: CredentialAuthority, rid: UUID) -> SyncRun:
    """Abort is retryable but cannot undo an already committed snapshot."""
    row, _feed = await run_for(db, authority, rid)
    if row.state == "committed":
        raise ConflictError("Synchronization run is already committed")
    row.state = "aborted"
    await clear_staging(db, row)
    return row


def run_view(row: SyncRun) -> dict:
    """Metadata-only durable status; staging bodies never leak through results."""
    return {
        "run_id": str(row.id),
        "feed_id": str(row.feed_id),
        "operation_id": str(row.operation_id),
        "state": "expired"
        if row.state in {"uploading", "prepared"} and utcnow() >= row.expires_at
        else row.state,
        "diff_hash": row.diff_hash,
        "changes": len(row.diff or []),
        "requires_approval": row.requires_approval,
        "expires_at": to_utc_iso(row.expires_at),
        "prepared_until": to_utc_iso(row.prepared_until),
    }
