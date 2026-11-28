"""Per-day counts of public usage events for the owner statistics (#3053).

``public_mcp_usage_day_counts`` holds the ``tool_call`` events of one
endpoint and UTC day by workspace, tool, outcome, route and caller, so that
the statistics read whole past days from a few rows instead of from every
event. ``public_mcp_usage_closed_days`` says which days are complete there.

The usage writer adds a batch's calls in the batch's own transaction
(``add_batch_to_day_counts``), after the events: it counts the rows of the
batch (their event ids) that this transaction inserted (the ``txid``
column), not the rows it sent, so a batch sent again after a commit that
landed adds nothing, and a batch that fails adds nothing either. The event
ids bound the count because a stored ``txid`` can repeat: a database
restored from a logical dump keeps its rows' values while its transaction
counter starts again. A call counts for the UTC day it started on, whenever
it is written.

A day is trusted only once it is CLOSED: counted again from the events in
one transaction (``count_day``) and marked. Events the writer did not count
(written before these tables existed, or by a release without them while a
deployment changes over) are counted then. The hourly job closes every
retained day that ended at least ``DAY_CLOSE_GRACE`` ago, newest first
(``close_usage_days``).

Writers and the close of a day exclude each other through one advisory lock
per UTC day: the writer holds it shared from its count to its commit, the
close holds it exclusively from before its count to its commit. A writer
that had the lock first has committed before the close counts, so the
close's count includes its calls; a writer that asks for it later waits and
adds its calls on top of the close's count. So a closed day's counts equal
its events at every commit, for as long as every ``tool_call`` row is written
by the usage writer.

The retention purge deletes the counts and the closed mark of every day it
may have deleted events of (``first_whole_day``); no statistics window
reaches such a day.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Iterable
from datetime import date, datetime, timedelta
from datetime import time as day_time

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import get_settings
from models.public_mcp_usage import PMU_CALLER_ANONYMOUS, PMU_ROUTE_MCP, PMU_TXID_DEFAULT
from utils.datetime import utcnow
from utils.logger import get_logger

logger = get_logger(__name__)

# A day is closed once it ended this long ago: far longer than a call runs
# and a batch waits, so a closing day rarely meets a writer of that day.
DAY_CLOSE_GRACE = timedelta(minutes=15)
# One run of the hourly job stops starting days after this long.
CLOSE_RUN_BUDGET_SECONDS = 600.0

_DAY_LOCK_PREFIX = "public_mcp_usage_day:"

_LOCK_SHARED = text("SELECT pg_advisory_xact_lock_shared(hashtextextended(:key, 0))")
_LOCK_EXCLUSIVE = text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))")

# The batch's calls this transaction inserted, added to their days. Sorted,
# so two processes take the rows in one order.
_ADD_BATCH = text(
    "INSERT INTO public_mcp_usage_day_counts AS c "
    "(endpoint_id, day, workspace_id, tool, outcome, route, caller, calls) "
    "SELECT endpoint_id, (occurred_at)::date, workspace_id, tool, outcome, "
    "COALESCE(route, :default_route), COALESCE(caller, :default_caller), count(*) "
    "FROM public_mcp_usage_events "
    "WHERE event_id = ANY(CAST(:event_ids AS uuid[])) "
    "AND txid = (SELECT " + PMU_TXID_DEFAULT + ") AND kind = 'tool_call' "
    "GROUP BY 1, 2, 3, 4, 5, 6, 7 "
    "ORDER BY 1, 2, 3, 4, 5, 6, 7 "
    "ON CONFLICT (endpoint_id, day, workspace_id, tool, outcome, route, caller) "
    "DO UPDATE SET calls = c.calls + EXCLUDED.calls"
)
_IS_CLOSED = text("SELECT 1 FROM public_mcp_usage_closed_days WHERE day = :day")
_CLOSED_BETWEEN = text(
    "SELECT day FROM public_mcp_usage_closed_days WHERE day >= :first AND day <= :last"
)
_DELETE_DAY = text("DELETE FROM public_mcp_usage_day_counts WHERE day = :day")
_COUNT_DAY = text(
    "INSERT INTO public_mcp_usage_day_counts "
    "(endpoint_id, day, workspace_id, tool, outcome, route, caller, calls) "
    "SELECT endpoint_id, CAST(:day AS date), workspace_id, tool, outcome, "
    "COALESCE(route, :default_route), COALESCE(caller, :default_caller), count(*) "
    "FROM public_mcp_usage_events "
    "WHERE kind = 'tool_call' AND occurred_at >= :day_start AND occurred_at < :next_day_start "
    "GROUP BY 1, 3, 4, 5, 6, 7"
)
_MARK_CLOSED = text("INSERT INTO public_mcp_usage_closed_days (day, closed_at) VALUES (:day, :now)")

_DEFAULTS = {"default_route": PMU_ROUTE_MCP, "default_caller": PMU_CALLER_ANONYMOUS}


def day_lock_key(day: date) -> str:
    """The advisory-lock key of one UTC day's counts."""
    return _DAY_LOCK_PREFIX + day.isoformat()


def retention_cutoff(now: datetime) -> datetime:
    """Events before this naive UTC time are purged."""
    return now - timedelta(days=get_settings().mcp_public_telemetry_retention_days)


def first_whole_day(cutoff: datetime) -> date:
    """The first UTC day that starts at or after ``cutoff``.

    The purge deletes events before ``cutoff``: no event of this day or a
    later one, and possibly some of every earlier day.
    """
    return (cutoff - timedelta(microseconds=1)).date() + timedelta(days=1)


def last_closable_day(now: datetime) -> date:
    """The newest UTC day that ended at least ``DAY_CLOSE_GRACE`` before ``now``."""
    return (now - DAY_CLOSE_GRACE).date() - timedelta(days=1)


async def add_batch_to_day_counts(
    session: AsyncSession, days: Iterable[date], event_ids: Iterable[uuid.UUID]
) -> None:
    """Add the batch's calls this transaction inserted to their days' counts.

    Call it after the batch's INSERT, in the same transaction; it takes each
    day's lock shared, oldest first, until the transaction ends.

    Args:
        session: The batch's session, its INSERT done and not committed.
        days: Every UTC day of the batch's calls.
        event_ids: The event ids of the batch's calls; a row with one of
            them that an earlier transaction inserted is not counted.
    """
    for day in sorted(set(days)):
        await session.execute(_LOCK_SHARED, {"key": day_lock_key(day)})
    await session.execute(_ADD_BATCH, {**_DEFAULTS, "event_ids": list(event_ids)})


async def count_day(db: AsyncSession, day: date, *, now: datetime) -> bool:
    """Count ``day`` again from its events and mark it closed.

    The caller holds the day's lock exclusively and commits.

    Returns:
        False, having written nothing, when the day is closed already, did
        not end ``DAY_CLOSE_GRACE`` before ``now``, or may have lost events
        to the retention purge.
    """
    if day > last_closable_day(now) or day < first_whole_day(retention_cutoff(now)):
        return False
    if (await db.execute(_IS_CLOSED, {"day": day})).first() is not None:
        return False
    start = datetime.combine(day, day_time.min)
    await db.execute(_DELETE_DAY, {"day": day})
    await db.execute(
        _COUNT_DAY,
        {**_DEFAULTS, "day": day, "day_start": start, "next_day_start": start + timedelta(days=1)},
    )
    await db.execute(_MARK_CLOSED, {"day": day, "now": now})
    return True


async def close_usage_day(db: AsyncSession, day: date, *, now: datetime | None = None) -> bool:
    """Close one day in a transaction of its own; True when this call closed it.

    Args:
        db: Async session with no open transaction of the caller's.
        day: The UTC day.
        now: Naive UTC time the checks use (defaults to the time once the
            lock is held).
    """
    try:
        await db.execute(_LOCK_EXCLUSIVE, {"key": day_lock_key(day)})
        closed = await count_day(db, day, now=now or utcnow())
    except Exception:
        await db.rollback()
        raise
    if closed:
        await db.commit()
    else:
        await db.rollback()
    return closed


async def close_usage_days(
    db: AsyncSession,
    now: datetime | None = None,
    *,
    budget_seconds: float = CLOSE_RUN_BUDGET_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Close every retained, finished day that is not closed, newest first.

    Each day in a transaction of its own; no day starts after
    ``budget_seconds`` (the next run goes on).

    Args:
        db: Async session.
        now: Naive UTC time (defaults to ``utcnow()``; tests pin it).
        budget_seconds: Time after which no further day starts.
        clock: Monotonic clock (tests pin it).

    Returns:
        Number of days this run closed.
    """
    run_now = now or utcnow()
    first = first_whole_day(retention_cutoff(run_now))
    last = last_closable_day(run_now)
    if last < first:
        return 0
    closed = set(
        (await db.execute(_CLOSED_BETWEEN, {"first": first, "last": last})).scalars().all()
    )
    await db.commit()
    deadline = clock() + budget_seconds
    count = 0
    left = 0
    day = last
    while day >= first:
        if day not in closed:
            if clock() >= deadline:
                left += 1
            elif await close_usage_day(db, day, now=now):
                count += 1
        day -= timedelta(days=1)
    if count or left:
        logger.info("public_mcp_usage_days_closed", closed=count, left=left)
    return count
