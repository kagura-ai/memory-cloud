"""Durable index wakeups and conservative, separately observable search progress."""

from datetime import timedelta

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import get_settings
from models.memory import Memory
from models.resource import IndexerState, Resource
from models.sync_feed import SyncChunk, SyncFeed, SyncIndexOutbox, SyncRecord, SyncRun
from utils.datetime import utcnow


async def maintain_sync_runs(db: AsyncSession) -> None:
    """Periodic, bounded work; replay an undelivered wakeup after crashes/lost updates."""
    if not get_settings().sync_runs_enabled:
        return
    expired = list(
        (
            await db.scalars(
                select(SyncRun.id)
                .where(SyncRun.state.in_(["uploading", "prepared"]), SyncRun.expires_at <= utcnow())
                .limit(100)
                .with_for_update(skip_locked=True)
            )
        ).all()
    )
    if expired:
        await db.execute(
            update(SyncChunk).where(SyncChunk.run_id.in_(expired)).values(records=None)
        )
        await db.execute(
            update(SyncRun)
            .where(SyncRun.id.in_(expired))
            .values(state="expired", approved_by=None, approval_id=None, approval_epoch=None)
        )
    stale = utcnow() - timedelta(minutes=30)
    eligible = (
        select(SyncIndexOutbox.run_id)
        .outerjoin(
            IndexerState,
            and_(
                IndexerState.resource_pk == SyncIndexOutbox.resource_pk,
                IndexerState.context_id == SyncIndexOutbox.context_id,
            ),
        )
        .where(
            SyncIndexOutbox.delivered_at.is_(None),
            or_(
                IndexerState.id.is_(None),
                IndexerState.last_offset >= SyncIndexOutbox.last_event_id,
                IndexerState.job_status.in_(["idle", "failed"]),
                and_(
                    IndexerState.job_status == "running",
                    or_(IndexerState.last_run_at.is_(None), IndexerState.last_run_at <= stale),
                ),
            ),
        )
        .distinct(SyncIndexOutbox.resource_pk, SyncIndexOutbox.context_id)
        .order_by(
            SyncIndexOutbox.resource_pk,
            SyncIndexOutbox.context_id,
            SyncIndexOutbox.last_event_id.desc(),
        )
    )
    rows = (
        await db.scalars(
            select(SyncIndexOutbox)
            .where(SyncIndexOutbox.run_id.in_(eligible))
            .order_by(SyncIndexOutbox.last_event_id)
            .limit(100)
            .with_for_update(skip_locked=True)
        )
    ).all()
    for outbox in rows:
        resource = await db.get(Resource, outbox.resource_pk)
        if resource is None:
            continue
        await db.execute(
            insert(IndexerState)
            .values(
                resource_pk=resource.id,
                resource_id=resource.resource_id,
                context_id=outbox.context_id,
                last_offset=0,
                job_status="queued",
                next_run_at=utcnow(),
            )
            .on_conflict_do_nothing(
                index_elements=[IndexerState.resource_pk, IndexerState.context_id],
                index_where=IndexerState.resource_pk.is_not(None),
            )
        )
        state = await db.scalar(
            select(IndexerState)
            .where(
                IndexerState.resource_pk == resource.id,
                IndexerState.context_id == outbox.context_id,
            )
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        if state is None:
            continue
        if state.last_offset >= outbox.last_event_id:
            await db.execute(
                update(SyncIndexOutbox)
                .where(
                    SyncIndexOutbox.resource_pk == resource.id,
                    SyncIndexOutbox.context_id == outbox.context_id,
                    SyncIndexOutbox.last_event_id <= state.last_offset,
                    SyncIndexOutbox.delivered_at.is_(None),
                )
                .values(delivered_at=utcnow())
            )
        elif state.job_status in {"idle", "failed"} or (
            state.job_status == "running"
            and (state.last_run_at is None or state.last_run_at <= stale)
        ):
            state.job_status, state.next_run_at = "queued", utcnow()
    await db.commit()


async def hold_sync_index_receipt(db: AsyncSession, resource_pk, context_id) -> None:
    """A live batch holds the newest receipt; stale recovery skips its locked target."""
    if get_settings().sync_runs_enabled:
        await db.scalar(
            select(SyncIndexOutbox)
            .where(
                SyncIndexOutbox.resource_pk == resource_pk,
                SyncIndexOutbox.context_id == context_id,
                SyncIndexOutbox.delivered_at.is_(None),
            )
            .order_by(SyncIndexOutbox.last_event_id.desc())
            .limit(1)
            .with_for_update(read=True, nowait=True)
        )


async def index_status(db: AsyncSession, row: SyncRun, feed: SyncFeed) -> str:
    """An advanced cursor alone never proves success; validate visible rows too."""
    if row.state != "committed":
        return "not_committed"
    if feed.revision != row.base_revision + 1:
        return "superseded"
    outbox = await db.get(SyncIndexOutbox, row.id)
    if outbox is None:
        return "unchanged"
    state = await db.scalar(
        select(IndexerState).where(
            IndexerState.resource_pk == outbox.resource_pk,
            IndexerState.context_id == outbox.context_id,
        )
    )
    if state is None or state.last_offset < outbox.last_event_id:
        return "failed" if state is not None and state.job_status == "failed" else "pending"
    resource = await db.get(Resource, outbox.resource_pk)
    if resource is None:
        return "failed"
    records = (
        await db.scalars(
            select(SyncRecord).where(SyncRecord.feed_id == feed.id, SyncRecord.payload.is_not(None))
        )
    ).all()
    for start in range(0, len(records), 100):
        batch = records[start : start + 100]
        memories = (
            await db.scalars(
                select(Memory)
                .where(
                    Memory.context_id == outbox.context_id,
                    Memory.resource_id == resource.resource_id,
                    Memory.resource_doc_id.in_([item.doc_id for item in batch]),
                    Memory.deleted_at.is_(None),
                )
                .limit(len(batch) + 1)
            )
        ).all()
        by_doc = {memory.resource_doc_id: memory for memory in memories}
        if (
            len(memories) != len(batch)
            or len(by_doc) != len(batch)
            or any(
                (
                    by_doc[item.doc_id].resource_version != item.version
                    or by_doc[item.doc_id].embedding_status != "success"
                    or by_doc[item.doc_id].version != 1
                    or by_doc[item.doc_id].source != "resource_ingest"
                )
                for item in batch
            )
        ):
            return "failed"
    deleted = [item["external_id"] for item in (row.diff or []) if item["op"] == "delete"]
    if deleted:
        count = await db.scalar(
            select(func.count())
            .select_from(Memory)
            .join(SyncRecord, (Memory.resource_doc_id == SyncRecord.doc_id))
            .where(
                SyncRecord.feed_id == feed.id,
                SyncRecord.external_id.in_(deleted),
                Memory.context_id == outbox.context_id,
                Memory.resource_id == resource.resource_id,
                Memory.deleted_at.is_(None),
            )
        )
        if count:
            return "failed"
    return "complete"
