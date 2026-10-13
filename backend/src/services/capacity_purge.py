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
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import User, Workspace
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
#: Lifetime of a notice claim in Redis: past the 30 days, so no stage repeats.
NOTICE_KEY_TTL_SECONDS = 45 * 24 * 3600


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


def notice_stage(elapsed_days: int) -> int | None:
    """The latest :data:`NOTICE_DAYS` entry due after ``elapsed_days`` — one mail per run.

    ``None`` before day 0 (a stamp in the future: clock skew).
    """
    due = [d for d in NOTICE_DAYS if d <= elapsed_days]
    return max(due) if due else None


def notice_key(workspace_id: UUID, locked_since: datetime, stage: int) -> str:
    """Redis claim: one mail per workspace × lock start × stage.

    A cleared and re-stamped lock has a new ``locked_since`` and therefore
    fresh keys — the countdown restarts at day 0.
    """
    return (
        f"capacity_purge_notice:{workspace_id}:{locked_since.isoformat(timespec='seconds')}:{stage}"
    )


def _days_between(start: datetime, end: datetime) -> int:
    """Calendar days from ``start`` to ``end`` (UTC dates).

    The sweep runs once a day at a fixed time, but the stamp is written a few
    seconds into its run: counting whole 24-hour periods would put every later
    run on the previous day and send each mail a day late.
    """
    return (end.date() - start.date()).days


@dataclass(frozen=True)
class NoticeCounts:
    """What one notice pass did.

    ``sent`` includes deliveries whose outcome is uncertain (the claim is kept).
    ``skipped``: no stage due, within capacity / no longer a candidate, or no
    owner e-mail. ``failed``: delivery failed (claim released) or Redis down.
    """

    considered: int
    sent: int
    already_sent: int
    skipped: int
    failed: int


async def _stamped_workspaces(db: AsyncSession) -> list[Workspace]:
    """Every live workspace with a lock clock running."""
    rows = await db.execute(
        select(Workspace).where(
            Workspace.deleted_at.is_(None),
            Workspace.capacity_locked_since.isnot(None),
        )
    )
    return list(rows.scalars().all())


async def _owner_contact(db: AsyncSession, workspace: Workspace) -> tuple[str, str] | None:
    """The owner's ``(email, locale)``, or ``None`` when there is no address."""
    row = (
        await db.execute(
            select(User.email, User.locale).where(User.user_id == workspace.owner_user_id)
        )
    ).one_or_none()
    if row is None or not row[0]:
        return None
    return str(row[0]), str(row[1] or "en")


async def _release_claim(redis: Any, key: str, workspace_id: UUID) -> None:
    try:
        await redis.delete(key)
    except Exception as exc:  # noqa: BLE001 — the TTL still bounds it
        logger.warning(
            "capacity_purge_notice_release_failed",
            workspace_id=str(workspace_id),
            error_type=type(exc).__name__,
        )


async def send_due_purge_notices(
    db: AsyncSession, *, now: datetime, redis: Any, email: Any
) -> NoticeCounts:
    """Mail the owner of every stamped workspace whose latest due stage is unclaimed.

    The lock is re-read (``capacity_lock_state``) so a workspace cleaned up or
    re-subscribed since the stamp run is skipped — the stamp sweep clears it
    next run. One mail per workspace per run: the LATEST due stage, so a sweep
    that missed days catches up with a single message. A failed delivery
    releases the claim; an uncertain one (``TimeoutError``) keeps it — at
    most once. Redis unavailable fails closed (no mail, counted as failed).

    Args:
        db: Session to read with; nothing is written.
        now: The sweep's clock (naive UTC).
        redis: Async Redis client (``set(nx, ex)`` / ``delete``).
        email: An ``EmailService``.

    Returns:
        The per-outcome counts of this pass.
    """
    from config.settings import get_settings

    base_url = get_settings().frontend_url.strip().rstrip("/")
    contexts_url = f"{base_url}/workspace/contexts"
    considered = sent = already = skipped = failed = 0
    for ws in await _stamped_workspaces(db):
        considered += 1
        since = ws.capacity_locked_since
        deadline = purge_at(since)
        if since is None or deadline is None:
            skipped += 1
            continue
        stage = notice_stage(_days_between(since, now))
        if stage is None:
            skipped += 1
            continue
        lock = await capacity_lock_state(db, ws) if is_lock_candidate(ws) else None
        if lock is None:
            skipped += 1
            continue
        contact = await _owner_contact(db, ws)
        if contact is None:
            logger.warning("capacity_purge_notice_no_owner_email", workspace_id=str(ws.id))
            skipped += 1
            continue
        to_email, locale = contact
        key = notice_key(ws.id, since, stage)
        try:
            claimed = await redis.set(key, "1", nx=True, ex=NOTICE_KEY_TTL_SECONDS)
        except Exception as exc:  # noqa: BLE001 — fail closed: never risk a duplicate
            logger.warning(
                "capacity_purge_notice_dedup_unavailable",
                workspace_id=str(ws.id),
                error_type=type(exc).__name__,
            )
            failed += 1
            continue
        if not claimed:
            already += 1
            continue
        try:
            delivered = await email.send_capacity_purge_notice(
                to_email=to_email,
                workspace_name=ws.name,
                locale=locale,
                purge_at=deadline,
                days_left=max(_days_between(now, deadline), 0),
                over_memories=lock.over_memories,
                over_bytes=lock.over_bytes,
                cleanup_url=lock.cleanup_url,
                contexts_url=contexts_url,
            )
        except TimeoutError:
            # The provider may have accepted it: keep the claim (at most once).
            logger.warning("capacity_purge_notice_delivery_uncertain", workspace_id=str(ws.id))
            sent += 1
            continue
        except Exception as exc:  # noqa: BLE001 — implementations must not raise; be safe
            logger.warning(
                "capacity_purge_notice_send_raised",
                workspace_id=str(ws.id),
                error_type=type(exc).__name__,
            )
            delivered = False
        if not delivered:
            await _release_claim(redis, key, ws.id)
            failed += 1
            continue
        sent += 1
        logger.info(
            "capacity_purge_notice_sent",
            workspace_id=str(ws.id),
            stage=stage,
            purge_at=deadline.isoformat(),
        )
    logger.info(
        "capacity_purge_notice_pass",
        considered=considered,
        sent=sent,
        already_sent=already,
        skipped=skipped,
        failed=failed,
    )
    return NoticeCounts(
        considered=considered, sent=sent, already_sent=already, skipped=skipped, failed=failed
    )
