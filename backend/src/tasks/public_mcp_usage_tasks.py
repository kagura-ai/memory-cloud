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

Daily visitor rows (#3035) whose UTC day is before the cutoff's day go next,
in batches, whatever the switches say. Then the per-day counts (#3053) of
every day that starts before the cutoff (the days the purge may have taken
events of): their closed marks first, then their counts in batches.

An hourly job (#3053) closes the finished days of the per-day counts
(``services/public_mcp_usage_rollup.py``).
"""

from __future__ import annotations

from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from db.base import get_db
from services.public_mcp_usage_rollup import (
    close_usage_days,
    first_whole_day,
    retention_cutoff,
)
from tasks.job_runs import JobResult, job_failed
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

_DELETE_VISITOR_BATCH = text(
    "DELETE FROM public_mcp_daily_visitors WHERE (endpoint_id, day, visitor_key) IN ("
    "  SELECT endpoint_id, day, visitor_key FROM public_mcp_daily_visitors"
    "  WHERE day < :cutoff_day LIMIT :limit"
    ") RETURNING day"
)

# #3053: the closed marks, then the counts, of the days before the first
# whole retained day.
_DELETE_CLOSED_DAYS = text("DELETE FROM public_mcp_usage_closed_days WHERE day < :first_kept_day")
_DELETE_DAY_COUNT_BATCH = text(
    "DELETE FROM public_mcp_usage_day_counts "
    "WHERE (endpoint_id, day, workspace_id, tool, outcome, route, caller) IN ("
    "  SELECT endpoint_id, day, workspace_id, tool, outcome, route, caller"
    "  FROM public_mcp_usage_day_counts WHERE day < :first_kept_day LIMIT :limit"
    ") RETURNING day"
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
    # One definition with the close of a day (#3053).
    cutoff = retention_cutoff(now)
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
    # #3035: a day's visitor rows go once the whole day is before the cutoff.
    visitors = 0
    while True:
        gone = (
            await db.execute(
                _DELETE_VISITOR_BATCH, {"cutoff_day": cutoff.date(), "limit": batch_size}
            )
        ).all()
        await db.commit()
        visitors += len(gone)
        if len(gone) < batch_size:
            break
    # #3053: a day the purge may have taken events of is no longer closed,
    # and its counts go. No statistics window reaches such a day.
    first_kept_day = first_whole_day(cutoff)
    unclosed = await db.execute(_DELETE_CLOSED_DAYS, {"first_kept_day": first_kept_day})
    await db.commit()
    day_counts = 0
    while True:
        gone = (
            await db.execute(
                _DELETE_DAY_COUNT_BATCH, {"first_kept_day": first_kept_day, "limit": batch_size}
            )
        ).all()
        await db.commit()
        day_counts += len(gone)
        if len(gone) < batch_size:
            break
    logger.info(
        "public_mcp_usage_purged",
        deleted=total,
        doc_keys_deleted=int(getattr(keys, "rowcount", 0) or 0),
        visitors_deleted=visitors,
        closed_days_deleted=int(getattr(unclosed, "rowcount", 0) or 0),
        day_counts_deleted=day_counts,
    )
    return total


async def purge_public_mcp_usage_events_task() -> JobResult:
    """APScheduler entry point — owns its own session via ``get_db()``."""
    async for db in get_db():
        try:
            deleted = await purge_public_mcp_usage_events(db)
        except Exception as exc:
            logger.exception("public_mcp_usage_purge_failed")
            await db.rollback()
            return job_failed(exc)
        return {"deleted": deleted}
    return None


async def close_public_mcp_usage_days_task() -> JobResult:
    """APScheduler entry point of the hourly close (#3053); owns its session."""
    async for db in get_db():
        try:
            closed = await close_usage_days(db)
        except Exception as exc:
            logger.exception("public_mcp_usage_close_failed")
            await db.rollback()
            return job_failed(exc)
        return {"closed": closed}
    return None


def schedule_public_mcp_usage_tasks(scheduler: AsyncIOScheduler) -> None:
    """Register the daily usage event purge and the hourly close of finished days.

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
    scheduler.add_job(
        close_public_mcp_usage_days_task,
        trigger=CronTrigger(minute=20),  # hourly: a day closes at 00:20 the day after
        id="close_public_mcp_usage_days",
        name="Close public MCP usage days (#3053)",
        replace_existing=True,
    )
