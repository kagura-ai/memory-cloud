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
  The day-29 mail ("1 day left") is recorded in
  ``workspaces.capacity_purge_final_notice_at``.
* ``purge_expired_workspaces`` deletes the data of a workspace stamped 30
  days ago or more whose final notice went out on an earlier calendar day,
  after re-checking the lock, and mails the completion. Without that notice
  nothing is deleted — so the purge flag alone, with the notices off, never
  deletes anything.

``run_capacity_purge_sweep`` runs the three in that order; the task in
``tasks/capacity_purge_tasks.py`` schedules it once a day under single-flight.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import AuditLog, User, Workspace
from services.capacity_lock import (
    CapacityLock,
    capacity_lock_state,
    is_lock_candidate,
    lock_candidate_predicate,
)
from services.context_service import CAPACITY_PURGE_ACTOR, ContextService, WorkspacePurgeReport
from utils.datetime import to_utc_iso
from utils.logger import get_logger

logger = get_logger(__name__)

#: Days of continuous capacity-over after which the data is deleted (policy #12).
PURGE_AFTER = timedelta(days=30)
#: Hour (UTC) of the daily sweep (``tasks/capacity_purge_tasks.py``); the
#: deletion date shown to the owner is that run on calendar day 30.
SWEEP_HOUR_UTC = 4
#: Days after the stamp on which the owner gets a "N days left" mail (policy #13).
NOTICE_DAYS: tuple[int, ...] = (0, 7, 14, 23, 27, 29)
#: The last of them; the purge waits until a calendar day after it went out.
FINAL_NOTICE_DAY = NOTICE_DAYS[-1]
#: Stamp sweep commits every this many workspaces (load: no long transaction).
STAMP_COMMIT_EVERY = 100
#: Workspaces purged per run at most; the rest stay stamped and go tomorrow (load).
PURGE_MAX_WORKSPACES_PER_RUN = 5
#: Lifetime of a notice claim in Redis: past the 30 days, so no stage repeats.
NOTICE_KEY_TTL_SECONDS = 45 * 24 * 3600


def purge_at(locked_since: datetime | None) -> datetime | None:
    """When the data is deleted if the lock holds — ``None`` while not locked.

    The :data:`SWEEP_HOUR_UTC` run on calendar day 30 of the stamp: the first
    run :func:`purge_due` says yes to, whatever time of day the stamp was
    written. Naive UTC. The usage payload, the banner and the countdown mails
    all show this value.
    """
    if locked_since is None:
        return None
    return datetime.combine(locked_since.date() + PURGE_AFTER, time(SWEEP_HOUR_UTC))


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
    so the rows loaded up front stay usable across those commits). Clearing
    the stamp also clears ``capacity_purge_final_notice_at``: the next lock
    needs its own final notice.

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
                _clear_stamp(ws)
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


def _clear_stamp(ws: Workspace) -> None:
    """End the lock clock: no stamp, and no final notice for it."""
    ws.capacity_locked_since = None
    ws.capacity_purge_final_notice_at = None


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

    A day-29 mail sent, or possibly sent, sets the workspace's
    ``capacity_purge_final_notice_at`` to ``now`` and commits: the purge waits
    for it. A failed one does not, so the purge waits for the retry.

    Args:
        db: Session to read with; commits once per recorded final notice.
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
            await _record_final_notice(db, ws, stage=stage, now=now)
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
        await _record_final_notice(db, ws, stage=stage, now=now)
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


async def _record_final_notice(
    db: AsyncSession, ws: Workspace, *, stage: int, now: datetime
) -> None:
    """After the day-29 mail went out (or may have): let the purge run tomorrow."""
    if stage != FINAL_NOTICE_DAY:
        return
    ws.capacity_purge_final_notice_at = now
    await db.commit()


def _lock_expired(locked_since: datetime | None, now: datetime) -> bool:
    """Calendar day 30 or later of the lock (:func:`_days_between`).

    Calendar days, like the notices: the stamp lands a few seconds into the
    04:00 run, so the day-30 run starts a few seconds short of 30 x 24 hours
    and would otherwise purge a day late — after the "1 day left" mail of day
    29 promised otherwise.
    """
    return locked_since is not None and _days_between(locked_since, now) >= PURGE_AFTER.days


def _final_notice_given(final_notice_at: datetime | None, now: datetime) -> bool:
    """The day-29 mail went out on an earlier calendar day than ``now``."""
    return final_notice_at is not None and _days_between(final_notice_at, now) >= 1


def purge_due(
    locked_since: datetime | None, now: datetime, *, final_notice_at: datetime | None
) -> bool:
    """Whether a lock stamped at ``locked_since`` is due for the purge at ``now``.

    Calendar day 30 of the lock or later, AND the final ("1 day left") notice
    recorded at ``final_notice_at`` went out on an earlier calendar day: no
    data is deleted without that warning — a missed or failed day-29 mail
    postpones the purge to the day after it finally goes out.
    """
    return _lock_expired(locked_since, now) and _final_notice_given(final_notice_at, now)


@dataclass(frozen=True)
class PurgeCounts:
    """What one purge pass did.

    ``due`` counts every workspace on calendar day 30 or later this run,
    including the ones past :data:`PURGE_MAX_WORKSPACES_PER_RUN` that wait for
    tomorrow and the ``awaiting_notice`` ones. ``awaiting_notice``: day 30 or
    later, but the final notice has not gone out on an earlier calendar day
    (notices off, or the day-29 mail failed) — nothing deleted, still stamped.
    ``skipped_within``: within capacity / no longer a candidate at purge time
    (un-stamped). ``failed``: the purge or its audit commit raised (stays
    stamped, retried next run). ``aborted``: stopped mid-way by a re-subscribe
    or an erasure. ``nothing_to_delete``: due and over, but the purge removed
    no context, memory or file (e.g. the retry after a lost audit commit) —
    un-stamped and audited, no completion mail.
    """

    due: int
    purged: int
    skipped_within: int
    failed: int
    aborted: int
    nothing_to_delete: int = 0
    awaiting_notice: int = 0


async def _workspace_state(db: AsyncSession, ws: Workspace) -> str:
    """Re-read the workspace row: ``"purgeable"``, ``"left"`` or ``"gone"``.

    ``"left"``: re-subscribed (no longer Free + billing-managed) or
    soft-deleted. ``"gone"``: the row no longer exists (hard-deleted by an
    erasure) — the instance must not be touched again, its attributes are
    expired.
    """
    try:
        await db.refresh(ws)
    except InvalidRequestError:
        return "gone"
    return "purgeable" if ws.deleted_at is None and is_lock_candidate(ws) else "left"


async def purge_expired_workspaces(db: AsyncSession, *, now: datetime, email: Any) -> PurgeCounts:
    """Delete the data of every workspace stamped :data:`PURGE_AFTER` ago or more.

    Due on calendar day 30 of the stamp once the final notice went out on an
    earlier calendar day (:func:`purge_due`); a workspace past day 30 without
    it is counted as ``awaiting_notice``, logged and left alone. At most
    :data:`PURGE_MAX_WORKSPACES_PER_RUN` workspaces per run, oldest stamp
    first; the rest stay stamped and go on a later run.

    Each workspace row is re-read first, then the lock: a workspace that left
    the candidate set or is within capacity now is un-stamped and left alone
    (the stamp sweep runs before this in the same run, so this is a second
    guard). The purge itself (``ContextService.purge_workspace_data``) asks
    before each context whether the row is still live and a candidate, and
    stops if not (re-subscribe / erasure mid-run): the stamp is cleared, the
    audit row says ``aborted``, and no completion mail goes out.

    Each workspace is purged and audited on its own; a failure of the purge
    or of the commit that clears the stamp and writes the audit row is logged
    and rolled back (contexts already committed stay deleted), the workspace
    stays stamped and is retried next run, and the next workspace proceeds.
    The completion mail is sent once, after that commit — best-effort, with
    no Redis claim: the cleared stamp means the workspace is not selected
    again for this lock. A purge that removed nothing (no context, memory or
    file — typically the retry after a lost audit commit) is un-stamped and
    audited but sends no mail, so the owner never gets a zero-count message.
    After a purge that was not aborted the lock is measured once more; still
    over is logged (``capacity_purge_still_over``), not retried.

    Args:
        db: Session to read and write with; committed by this function.
        now: The sweep's clock (naive UTC).
        email: An ``EmailService``.

    Returns:
        The per-outcome counts of this pass.
    """
    expired = [
        ws for ws in await _stamped_workspaces(db) if _lock_expired(ws.capacity_locked_since, now)
    ]
    awaiting = [
        ws for ws in expired if not _final_notice_given(ws.capacity_purge_final_notice_at, now)
    ]
    for ws in awaiting:
        logger.warning(
            "capacity_purge_awaiting_final_notice",
            workspace_id=str(ws.id),
            locked_since=to_utc_iso(ws.capacity_locked_since),
            final_notice_at=to_utc_iso(ws.capacity_purge_final_notice_at),
        )
    all_due = sorted(
        (ws for ws in expired if ws not in awaiting),
        key=lambda w: (w.capacity_locked_since, str(w.id)),
    )
    # Ids read now, while the rows are loaded: a rollback on an earlier
    # workspace expires every instance, and reading ``ws.id`` after it would
    # lazy-load outside any try (MissingGreenlet on AsyncSession) and end the pass.
    batch = [(ws, ws.id) for ws in all_due[:PURGE_MAX_WORKSPACES_PER_RUN]]
    purged = skipped = failed = aborted = nothing = 0
    for ws, ws_id in batch:
        # Fresh row: a rollback on an earlier workspace expired it, and it may
        # have re-subscribed or been cleaned up since the list was read.
        state = await _workspace_state(db, ws)
        if state != "purgeable":
            if state == "left" and ws.capacity_locked_since is not None:
                _clear_stamp(ws)
                await db.commit()
            skipped += 1
            logger.info(
                "capacity_purge_skipped_not_candidate", workspace_id=str(ws_id), state=state
            )
            continue
        ws_name, since = ws.name, ws.capacity_locked_since
        if since is None or not purge_due(
            since, now, final_notice_at=ws.capacity_purge_final_notice_at
        ):
            skipped += 1
            continue
        lock = await capacity_lock_state(db, ws)
        if lock is None:
            _clear_stamp(ws)
            await db.commit()
            skipped += 1
            logger.info("capacity_purge_skipped_within_capacity", workspace_id=str(ws_id))
            continue
        contact = await _owner_contact(db, ws)
        last_state = ["purgeable"]

        async def should_continue(ws: Workspace = ws, last_state: list[str] = last_state) -> bool:
            last_state[0] = await _workspace_state(db, ws)
            return last_state[0] == "purgeable"

        try:
            report = await ContextService(db).purge_workspace_data(
                ws_id, actor=CAPACITY_PURGE_ACTOR, should_continue=should_continue
            )
        except Exception:  # noqa: BLE001 — one workspace must not stop the sweep
            logger.exception("capacity_purge_failed", workspace_id=str(ws_id))
            await db.rollback()
            failed += 1
            continue
        try:
            await _clear_stamp_and_audit(
                db,
                ws,
                ws_id=ws_id,
                clear=last_state[0] != "gone",
                user_email=contact[0] if contact else "",
                since=since,
                lock=lock,
                report=report,
            )
        except Exception:  # noqa: BLE001 — one workspace must not stop the sweep
            logger.exception("capacity_purge_audit_failed", workspace_id=str(ws_id))
            await db.rollback()
            failed += 1
            continue
        if report.aborted:
            aborted += 1
            logger.warning(
                "capacity_purge_aborted",
                workspace_id=str(ws_id),
                contexts_deleted=report.contexts_deleted,
                memories_deleted=report.memories_deleted,
            )
            continue
        await _log_if_still_over(db, ws, ws_id=ws_id)
        if not (report.contexts_deleted or report.memories_deleted or report.files_deleted):
            nothing += 1
            logger.info(
                "capacity_purge_nothing_to_delete",
                workspace_id=str(ws_id),
                bytes_released=report.bytes_released,
            )
            continue
        purged += 1
        logger.info(
            "capacity_purge_completed",
            workspace_id=str(ws_id),
            contexts_deleted=report.contexts_deleted,
            memories_deleted=report.memories_deleted,
            files_deleted=report.files_deleted,
            bytes_released=report.bytes_released,
        )
        if contact is None:
            logger.warning("capacity_purge_complete_no_owner_email", workspace_id=str(ws_id))
            continue
        # Best-effort: the outcome is logged under the workspace (the provider's
        # own log carries only a recipient hash), so a lost mail can be alerted on.
        try:
            delivered = await email.send_capacity_purge_complete(
                to_email=contact[0],
                workspace_name=ws_name,
                locale=contact[1],
                purged_at=now,
                contexts_deleted=report.contexts_deleted,
                memories_deleted=report.memories_deleted,
                files_deleted=report.files_deleted,
                cleanup_url=lock.cleanup_url,
            )
        except Exception as exc:  # noqa: BLE001 — the purge is done; the mail is best-effort
            logger.warning(
                "capacity_purge_complete_send_raised",
                workspace_id=str(ws_id),
                error_type=type(exc).__name__,
            )
            continue
        if delivered:
            logger.info("capacity_purge_complete_mail_sent", workspace_id=str(ws_id))
        else:
            logger.warning("capacity_purge_complete_mail_failed", workspace_id=str(ws_id))
    counts = PurgeCounts(
        due=len(expired),
        purged=purged,
        skipped_within=skipped,
        failed=failed,
        aborted=aborted,
        nothing_to_delete=nothing,
        awaiting_notice=len(awaiting),
    )
    logger.info(
        "capacity_purge_pass",
        due=counts.due,
        awaiting_notice=counts.awaiting_notice,
        purged=purged,
        skipped_within=skipped,
        failed=failed,
        aborted=aborted,
        nothing_to_delete=nothing,
    )
    return counts


async def _log_if_still_over(db: AsyncSession, ws: Workspace, *, ws_id: UUID) -> None:
    """Measure the lock once more after a completed purge; warn if still over.

    The purge should leave nothing the lock counts. If it is still on (data
    the purge cannot reach), the next stamp run restarts the clock — this log
    is the alert for it. No second purge here: the next cycle handles it.
    """
    try:
        lock = await capacity_lock_state(db, ws)
    except Exception as exc:  # noqa: BLE001 — diagnostics only; the purge is done
        logger.warning(
            "capacity_purge_recheck_failed", workspace_id=str(ws_id), error_type=type(exc).__name__
        )
        return
    if lock is not None:
        logger.warning(
            "capacity_purge_still_over",
            workspace_id=str(ws_id),
            memory_count=lock.memory_count,
            memory_limit=lock.memory_limit,
            over_memories=lock.over_memories,
            used_bytes=lock.used_bytes,
            storage_limit_bytes=lock.storage_limit_bytes,
            over_bytes=lock.over_bytes,
        )


async def _clear_stamp_and_audit(
    db: AsyncSession,
    ws: Workspace,
    *,
    ws_id: UUID,
    clear: bool,
    user_email: str,
    since: datetime,
    lock: CapacityLock,
    report: WorkspacePurgeReport,
) -> None:
    """Clear the stamp and its final notice (unless the row is gone) and write
    the purge's audit row; commits."""
    if clear:
        _clear_stamp(ws)
    db.add(
        AuditLog(
            user_email=user_email,
            user_id=CAPACITY_PURGE_ACTOR,
            action="capacity_purge",
            resource=f"workspace:{ws_id}",
            user_metadata={
                "locked_since": since.isoformat(),
                "over_memories": lock.over_memories,
                "over_bytes": lock.over_bytes,
                "contexts_deleted": report.contexts_deleted,
                "contexts_emptied": report.contexts_emptied,
                "memories_deleted": report.memories_deleted,
                "files_deleted": report.files_deleted,
                "bytes_released": report.bytes_released,
                "aborted": report.aborted,
            },
        )
    )
    await db.commit()


@dataclass(frozen=True)
class SweepCounts:
    """One daily run: the stamp pass always; notices / purge ``None`` when flagged off."""

    stamp: StampCounts
    notices: NoticeCounts | None
    purge: PurgeCounts | None


#: ``EMAIL_PROVIDER`` values that actually deliver mail. The default
#: ``logging`` stub reports every send as delivered without sending anything.
REAL_MAIL_PROVIDERS: frozenset[str] = frozenset({"resend"})


def mail_provider_configured(settings: Any) -> bool:
    """Whether ``settings.email_provider`` delivers mail to a human.

    Fail closed: only the providers in :data:`REAL_MAIL_PROVIDERS` count, so a
    provider added later gates the notices and the purge until listed here.
    """
    return settings.email_provider in REAL_MAIL_PROVIDERS


async def run_capacity_purge_sweep(db: AsyncSession, *, now: datetime) -> SweepCounts:
    """Stamp, then notify, then purge — each later stage behind its flag.

    ``CAPACITY_PURGE_NOTICE_ENABLED`` gates the countdown mails and
    ``CAPACITY_PURGE_ENABLED`` the deletion; stamping always runs, so the clock
    is correct the day either flag is turned on.

    Both later stages also need a mail provider that really delivers
    (:func:`mail_provider_configured`). With the default logging stub every
    "send" succeeds, so the final notice would be recorded and the purge would
    follow without anyone having been warned: the run logs
    ``capacity_purge_no_mail_provider`` once and skips both stages.
    """
    from config.settings import get_settings
    from db.redis import get_redis_client
    from services.email_service import get_email_service

    settings = get_settings()
    stamp = await stamp_capacity_locks(db, now=now)
    notice_on = bool(settings.capacity_purge_notice_enabled)
    purge_on = bool(settings.capacity_purge_enabled)
    if (notice_on or purge_on) and not mail_provider_configured(settings):
        logger.warning(
            "capacity_purge_no_mail_provider",
            email_provider=str(settings.email_provider),
            notice_enabled=notice_on,
            purge_enabled=purge_on,
        )
        return SweepCounts(stamp=stamp, notices=None, purge=None)
    notices = None
    if notice_on:
        notices = await send_due_purge_notices(
            db, now=now, redis=get_redis_client(), email=get_email_service()
        )
    purge = None
    if purge_on:
        purge = await purge_expired_workspaces(db, now=now, email=get_email_service())
    return SweepCounts(stamp=stamp, notices=notices, purge=purge)
