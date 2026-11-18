"""Durable index wakeups and conservative, separately observable search progress."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from time import monotonic
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import get_settings
from models.resource import IndexerState, Resource
from models.sync_feed import SyncChunk, SyncFeed, SyncIndexOutbox, SyncRecord, SyncRun
from utils.datetime import utcnow
from utils.logger import get_logger

logger = get_logger(__name__)

# #2915: the activity retention is not tick work. One pass of up to 500 rows an
# hour per process keeps the 90-day bound; the first tick after a start runs it.
ACTIVITY_PRUNE_INTERVAL_SECONDS = 3600


@dataclass
class ActivityPruneSchedule:
    """When this process may run the activity retention next (``time.monotonic``)."""

    due_at: float = 0.0


activity_prune_schedule = ActivityPruneSchedule()


async def prune_activity_on_schedule(db: AsyncSession) -> None:
    """Run the sync activity retention at most once an interval, in its own transaction.

    The session must hold no work of its caller: the pass commits on its own,
    and a failing pass is rolled back and logged here, so it neither cancels
    the run sweep after it nor ends the indexer tick. A failed pass waits for
    the next interval like a successful one.
    """
    now = monotonic()
    if now < activity_prune_schedule.due_at:
        return
    activity_prune_schedule.due_at = now + ACTIVITY_PRUNE_INTERVAL_SECONDS
    from services.sync_audit import prune_activity

    try:
        await prune_activity(db)
        await db.commit()
    except Exception as exc:
        await db.rollback()
        logger.error("sync_activity_prune_failed", error_class=type(exc).__name__)


async def maintain_sync_runs(db: AsyncSession) -> None:
    """Periodic, bounded work; replay an undelivered wakeup after crashes/lost updates."""
    await prune_activity_on_schedule(db)
    if not get_settings().sync_runs_enabled:
        from services.sync_management import maintain_sync_management

        await maintain_sync_management(db)
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
    from services.sync_management import maintain_sync_management

    await maintain_sync_management(db)


async def hold_sync_index_receipt(db: AsyncSession, resource_pk: UUID, context_id: UUID) -> None:
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


def run_failure_recorded(metrics: Any, event_ids: Sequence[int] | None) -> bool:
    """Whether the target's failure record holds, or may have dropped, a run's event (#2905).

    ``metrics`` is the target's ``indexer_state.metrics``; ``event_ids`` the
    run's outbox ``event_ids``. The record keeps only the
    ``MAX_RECORDED_FAILED_EVENTS`` lowest event ids, so once it is full a
    failure above its highest id is not in it: a full record whose highest id
    is below the run's last event counts as a failure of the run too. Such a
    target needs an operator rewind or clear anyway
    (docs/ops/resource-indexer-backfill.md); a retry that applies the events
    still turns the run ``complete``.
    """
    from services.resource_indexer import MAX_RECORDED_FAILED_EVENTS, recorded_failed_events

    run_ids = {event_id for event_id in event_ids or () if type(event_id) is int}
    recorded = {entry["event_id"] for entry in recorded_failed_events(metrics)}
    if not run_ids or not recorded:
        return False
    if not recorded.isdisjoint(run_ids):
        return True
    return len(recorded) >= MAX_RECORDED_FAILED_EVENTS and max(recorded) < max(run_ids)


async def index_status(db: AsyncSession, row: SyncRun, feed: SyncFeed) -> str:
    """An advanced cursor alone never proves success; validate visible rows too.

    Below the run's watermark the run is ``pending`` while the indexer has not
    applied its events yet, a batch stopped at a busy lock included, and
    ``failed`` once the target's job failed or the indexer recorded one of the
    run's events as failed (#2905). ``failed`` there can still turn
    ``complete``: the outbox replay re-queues the target and a retry that
    applies the event moves the cursor past the watermark.
    """
    if row.state != "committed":
        return "not_committed"
    if feed.revision != row.base_revision + 1:
        return "superseded"
    from models.sync_installation import SyncInstallation
    from services.sync_management import canonical_index_complete

    installation = await db.get(SyncInstallation, feed.installation_id)
    if installation is None:
        return "failed"
    outbox = await db.get(SyncIndexOutbox, row.id)
    watermark = (
        outbox.last_event_id
        if outbox is not None
        else (
            await db.scalar(
                select(func.max(SyncRecord.last_event_id)).where(SyncRecord.feed_id == feed.id)
            )
        )
        or 0
    )
    state = (
        await db.execute(
            select(IndexerState.last_offset, IndexerState.job_status, IndexerState.metrics).where(
                IndexerState.resource_pk == installation.resource_pk,
                IndexerState.context_id == installation.context_id,
            )
        )
    ).first()
    if watermark and (state is None or state.last_offset < watermark):
        if state is not None and (
            state.job_status == "failed"
            or (outbox is not None and run_failure_recorded(state.metrics, outbox.event_ids))
        ):
            return "failed"
        return "pending"
    # A shared job's historical/other-feed errors cannot override actual
    # current canonical proof for this feed. Stop/drain retains its stricter
    # context-wide failure barrier independently.
    if not await canonical_index_complete(db, installation, feed_id=feed.id):
        return "failed"
    return "unchanged" if outbox is None else "complete"
