"""Daily retention of public MCP usage events (#2678).

Events older than ``mcp_public_telemetry_retention_days`` are deleted in
batches in ``(txid, id)`` order. After each batch the purge mark in
``public_mcp_usage_retention`` advances to the largest ``(txid, id)`` deleted,
in the same transaction as the deletes: every purged row is at or below the
mark, so an export cursor below it may have missed rows and is answered with
``cursor_expired`` instead of skipping them silently. The mark never moves
back, and each batch is idempotent, so a rerun (or a second process) is
harmless.

Doc keys of resources that have had no live public endpoint since before the
cutoff are deleted too: no retained event can name them any more.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import get_settings
from db.base import get_db
from utils.datetime import utcnow
from utils.logger import get_logger

logger = get_logger(__name__)

PURGE_BATCH_SIZE = 5_000

_DELETE_BATCH = text(
    "DELETE FROM public_mcp_usage_events WHERE id IN ("
    "  SELECT id FROM public_mcp_usage_events WHERE occurred_at < :cutoff"
    "  ORDER BY txid, id LIMIT :limit"
    ") RETURNING txid, id"
)

# Upsert so a database built without the migration's row still gets a mark;
# the CASE keeps the mark monotonic.
_ADVANCE_MARK = text(
    "INSERT INTO public_mcp_usage_retention AS r "
    "  (id, purged_through_txid, purged_through_id, purged_total, purged_at) "
    "VALUES (1, :t, :i, :n, :now) "
    "ON CONFLICT (id) DO UPDATE SET "
    "  purged_through_txid = CASE WHEN (r.purged_through_txid, r.purged_through_id) < (:t, :i) "
    "    THEN :t ELSE r.purged_through_txid END, "
    "  purged_through_id = CASE WHEN (r.purged_through_txid, r.purged_through_id) < (:t, :i) "
    "    THEN :i ELSE r.purged_through_id END, "
    "  purged_total = r.purged_total + :n, "
    "  purged_at = :now"
)

_DELETE_STALE_DOC_KEYS = text(
    "DELETE FROM public_mcp_doc_keys AS k "
    "WHERE NOT EXISTS ("
    "  SELECT 1 FROM public_mcp_endpoints AS e "
    "  WHERE e.resource_pk = k.resource_pk AND e.revoked_at IS NULL"
    ") AND COALESCE("
    "  (SELECT max(e.revoked_at) FROM public_mcp_endpoints AS e "
    "   WHERE e.resource_pk = k.resource_pk),"
    "  k.first_seen_at"
    ") < :cutoff"
)


async def purge_public_mcp_usage_events(
    db: AsyncSession, now: datetime | None = None, *, batch_size: int = PURGE_BATCH_SIZE
) -> int:
    """Delete events past the retention window, advancing the purge mark.

    Commits after every batch (the batch and its mark together).

    Args:
        db: Async session.
        now: Current naive UTC time (defaults to ``utcnow()``; tests pin it).
        batch_size: Rows per DELETE.

    Returns:
        Number of events deleted.
    """
    now = now or utcnow()
    cutoff = now - timedelta(days=get_settings().mcp_public_telemetry_retention_days)
    total = 0
    while True:
        deleted = (await db.execute(_DELETE_BATCH, {"cutoff": cutoff, "limit": batch_size})).all()
        if not deleted:
            break
        t, i = max((int(row.txid), int(row.id)) for row in deleted)
        await db.execute(_ADVANCE_MARK, {"t": t, "i": i, "n": len(deleted), "now": now})
        await db.commit()
        total += len(deleted)
        if len(deleted) < batch_size:
            break
    keys = await db.execute(_DELETE_STALE_DOC_KEYS, {"cutoff": cutoff})
    await db.commit()
    logger.info(
        "public_mcp_usage_purged",
        deleted=total,
        doc_keys_deleted=int(getattr(keys, "rowcount", 0) or 0),
    )
    return total


async def purge_public_mcp_usage_events_task() -> None:
    """APScheduler entry point — owns its own session via ``get_db()``."""
    async for db in get_db():
        try:
            await purge_public_mcp_usage_events(db)
        except Exception:
            logger.exception("public_mcp_usage_purge_failed")
            await db.rollback()
        return


def schedule_public_mcp_usage_tasks(scheduler: AsyncIOScheduler) -> None:
    """Register the daily usage event purge.

    Args:
        scheduler: The shared APScheduler instance.
    """
    scheduler.add_job(
        purge_public_mcp_usage_events_task,
        trigger=CronTrigger(hour=4, minute=50),  # daily, after the 04:40 device cleanup
        id="purge_public_mcp_usage_events",
        name="Purge public MCP usage events (#2678)",
        replace_existing=True,
    )
    logger.info("scheduled_public_mcp_usage_purge", cron="04:50 UTC")
