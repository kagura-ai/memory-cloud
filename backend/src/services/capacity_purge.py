"""Capacity-over purge for Free workspaces (#1969).

The capacity-over lock (#1941, ``services/capacity_lock.py``) pauses a Free,
billing-managed workspace that holds more than Free allows. Policy: if it
stays over for 30 consecutive days, memory-cloud deletes its memories,
contexts and files; the account and an empty Free workspace remain.

memory-cloud owns the clock — ``workspaces.capacity_locked_since``:

* ``stamp_capacity_locks`` (daily) evaluates every lock candidate with the
  lock's own ``capacity_lock_state``; a workspace over the cap is stamped on
  its first sighting and the stamp is kept; one within the cap, or no longer
  a candidate (re-subscribed, admin-managed), is cleared. A cleanup therefore
  restarts the 30 days.
* ``send_due_purge_notices`` mails the owner on days 0/7/14/23/27/29 of the
  stamp (one claim per workspace × stamp × stage in Redis, like the
  pre-expiry notice in ``api/routes/internal_billing.py``).
* ``purge_expired_workspaces`` deletes the data of a workspace stamped 30
  days ago or more, after re-checking the lock, and mails the completion.

``run_capacity_purge_sweep`` runs the three in that order; the task in
``tasks/capacity_purge_tasks.py`` schedules it once a day under single-flight.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import Workspace
from services.capacity_lock import capacity_lock_state, is_lock_candidate, lock_candidate_predicate
from utils.logger import get_logger

logger = get_logger(__name__)

#: Days of continuous capacity-over after which the data is deleted (policy #12).
PURGE_AFTER = timedelta(days=30)
#: Days after the stamp on which the owner gets a "N days left" mail (policy #13).
NOTICE_DAYS: tuple[int, ...] = (0, 7, 14, 23, 27, 29)
#: Stamp sweep commits every this many workspaces (load: no long transaction).
STAMP_COMMIT_EVERY = 100
#: Workspaces purged per run at most; the rest stay stamped and go tomorrow (load).
PURGE_MAX_WORKSPACES_PER_RUN = 5


def purge_at(locked_since: datetime | None) -> datetime | None:
    """When the data is deleted if the lock holds — ``None`` while not locked."""
    return None if locked_since is None else locked_since + PURGE_AFTER


@dataclass(frozen=True)
class StampCounts:
    """What one stamp sweep did.

    ``evaluated`` also counts candidates that were within capacity and never
    stamped, so it is at least ``stamped + cleared + kept``.
    """

    evaluated: int
    stamped: int
    cleared: int
    kept: int


async def stamp_capacity_locks(db: AsyncSession, *, now: datetime) -> StampCounts:
    """Set / keep / clear ``capacity_locked_since`` on every lock candidate.

    Rows visited: every live Free + external-billing workspace (the candidate
    set) PLUS every stamped live workspace whatever its plan, so a workspace
    that left the candidate set while stamped is cleared. Commits every
    :data:`STAMP_COMMIT_EVERY` workspaces and once at the end, so no single
    transaction spans the whole sweep (sessions are ``expire_on_commit=False``,
    so the rows loaded up front stay usable across those commits).

    Args:
        db: Session to read and write with; committed by this function.
        now: The sweep's clock (naive UTC); written as the stamp on a first
            sighting.

    Returns:
        The per-outcome counts of this run.
    """
    rows = await db.execute(
        select(Workspace)
        .where(
            Workspace.deleted_at.is_(None),
            or_(Workspace.capacity_locked_since.isnot(None), lock_candidate_predicate()),
        )
        .order_by(Workspace.id)
    )
    evaluated = stamped = cleared = kept = 0
    for ws in rows.scalars().all():
        evaluated += 1
        if evaluated % STAMP_COMMIT_EVERY == 0:
            await db.commit()  # keep the transaction short
        lock = await capacity_lock_state(db, ws) if is_lock_candidate(ws) else None
        if lock is None:
            if ws.capacity_locked_since is not None:
                ws.capacity_locked_since = None
                cleared += 1
                logger.info("capacity_lock_cleared", workspace_id=str(ws.id))
        elif ws.capacity_locked_since is None:
            ws.capacity_locked_since = now
            stamped += 1
            logger.info(
                "capacity_lock_stamped",
                workspace_id=str(ws.id),
                over_memories=lock.over_memories,
                over_bytes=lock.over_bytes,
            )
        else:
            kept += 1
    await db.commit()
    logger.info(
        "capacity_lock_stamp_sweep",
        evaluated=evaluated,
        stamped=stamped,
        cleared=cleared,
        kept=kept,
    )
    return StampCounts(evaluated=evaluated, stamped=stamped, cleared=cleared, kept=kept)
