"""Periodic work of verified custom domains (#2809).

One run of the scheduler job ``public_mcp_domain_maintenance``
(``tasks.public_mcp_domain_tasks``), within a budget of
``RUN_BUDGET_SECONDS`` = ``min(interval / 2, 120 s)``:

1. Housekeeping, set-based, at most ``HOUSEKEEPING_LIMIT`` rows per
   statement, each statement in its own short transaction (rows locked by
   another transaction are skipped, not waited for):

   - ``pending`` rows older than ``PENDING_TTL_DAYS`` are deleted;
   - ``released`` rows whose cooldown has passed are deleted;
   - rows that are not ``released`` whose workspace row is gone, or whose
     publication is gone, revoked or of another workspace: ``pending`` rows
     are deleted, holders are ``released`` with the cooldown.

2. Up to ``MAINTENANCE_BATCH`` held rows (``active`` / ``suspended``) whose
   last check is older than ``RECHECK_AGE_SECONDS`` (or that were never
   checked) are read, rows whose last lookup was indeterminate after all
   others, oldest check first; then the connection is released.

3. Each due row, ``MAINTENANCE_CONCURRENCY`` at a time: the DNS lookup runs
   outside any session, then its result is applied by the one writer
   (``services.public_mcp_domains.apply_check``) in a session of its own.
   Every completed lookup is applied, an indeterminate one included: it
   records the check (so the row leaves the head of the next run) and, under
   an active right, renews the lease of an ``active`` row. A row whose
   lookup would start after the budget is counted ``budget_skipped`` and
   stays due.

4. While edge certificate reports are configured (#2941), reports whose
   ``valid_until`` has passed are deleted (``public_mcp_domain_tls_reports``,
   one bounded statement in a session of its own). A failure there is logged
   (``public_mcp_domain_tls_report_purge_failed``) and never fails the run;
   nothing touches that table while the feature is not configured.

The run returns integer counts only. Every time is the database clock; no
host name, token or DNS answer is logged. Audit rows of the job carry the
actor ``system:public-mcp-domains``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass
from uuid import UUID

from sqlalchemy import TextClause, func, literal_column, or_, select, text
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import get_settings
from db.base import _get_session_factory
from models.public_mcp_domain import (
    HELD_STATES,
    MAINTENANCE_BATCH,
    MAINTENANCE_CONCURRENCY,
    MAINTENANCE_INTERVAL_SECONDS,
    PENDING_TTL_DAYS,
    RECHECK_AGE_SECONDS,
    PublicMcpDomain,
)
from services import public_mcp_domains as domain_service
from services.domain_tls_observations import reporting_configured
from services.public_mcp_domains import SYSTEM_ACTOR, _audit
from utils.logger import get_logger

logger = get_logger(__name__)

__all__ = [
    "HOUSEKEEPING_LIMIT",
    "RUN_BUDGET_SECONDS",
    "DueDomain",
    "MaintenanceCounts",
    "due_domains",
    "housekeeping",
    "purge_expired_reports",
    "run_maintenance",
]

# Wall-clock budget of one run (seconds); tests shorten it.
RUN_BUDGET_SECONDS: float = min(MAINTENANCE_INTERVAL_SECONDS / 2, 120.0)
# Rows one housekeeping statement touches at most.
HOUSEKEEPING_LIMIT = 500

# The run's own clock for the budget (monotonic); tests replace it.
_clock: Callable[[], float] = time.monotonic

SessionFactory = Callable[[], AsyncSession]

_DB_NOW = "timezone('UTC', statement_timestamp())"

# Housekeeping statements. Each selects its rows with FOR UPDATE SKIP LOCKED:
# a row another transaction holds (an owner action, an overlapping run) is
# left for the next run instead of being waited for.
_EXPIRE_PENDING = text(
    "DELETE FROM public_mcp_domains WHERE id IN ("
    "  SELECT id FROM public_mcp_domains"
    "  WHERE state = 'pending'"
    "  AND created_at < timezone('UTC', statement_timestamp())"
    "    - make_interval(days => :ttl_days)"
    "  ORDER BY created_at LIMIT :limit FOR UPDATE SKIP LOCKED"
    ") RETURNING id, workspace_id, endpoint_id, hostname"
)

_PURGE_RELEASED = text(
    "DELETE FROM public_mcp_domains WHERE id IN ("
    "  SELECT id FROM public_mcp_domains"
    "  WHERE state = 'released'"
    "  AND cooldown_until <= timezone('UTC', statement_timestamp())"
    "  ORDER BY cooldown_until LIMIT :limit FOR UPDATE SKIP LOCKED"
    ") RETURNING id"
)

# Orphans: rows whose workspace row is gone, or whose endpoint id names no
# live publication of their own workspace (gone, revoked, or another
# workspace's).
_DELETE_ORPHAN_CLAIMS = text(
    "DELETE FROM public_mcp_domains WHERE id IN ("
    "  SELECT d.id FROM public_mcp_domains d"
    "  WHERE d.state = 'pending'"
    "  AND (NOT EXISTS (SELECT 1 FROM workspaces w WHERE w.id = d.workspace_id)"
    "   OR NOT EXISTS (SELECT 1 FROM public_mcp_endpoints e"
    "    WHERE e.id = d.endpoint_id AND e.workspace_id = d.workspace_id"
    "    AND e.revoked_at IS NULL))"
    "  ORDER BY d.created_at LIMIT :limit FOR UPDATE OF d SKIP LOCKED"
    ") RETURNING id, workspace_id, endpoint_id, hostname"
)

_RELEASE_ORPHAN_HOLDERS = text(
    "WITH target AS ("
    "  SELECT d.id, d.state FROM public_mcp_domains d"
    "  WHERE d.state IN ('active', 'suspended')"
    "  AND (NOT EXISTS (SELECT 1 FROM workspaces w WHERE w.id = d.workspace_id)"
    "   OR NOT EXISTS (SELECT 1 FROM public_mcp_endpoints e"
    "    WHERE e.id = d.endpoint_id AND e.workspace_id = d.workspace_id"
    "    AND e.revoked_at IS NULL))"
    "  ORDER BY d.created_at LIMIT :limit FOR UPDATE OF d SKIP LOCKED"
    ") "
    "UPDATE public_mcp_domains AS p SET"
    "  state = 'released',"
    "  suspended_reason = NULL,"
    "  released_at = timezone('UTC', statement_timestamp()),"
    "  cooldown_until = timezone('UTC', statement_timestamp())"
    "    + make_interval(hours => :cooldown_hours),"
    "  check_seq = p.check_seq + 1"
    " FROM target WHERE p.id = target.id"
    " RETURNING p.id, p.workspace_id, p.endpoint_id, p.hostname, target.state"
)


# Edge certificate reports past their own valid_until (#2941): never shown
# again, so deleted. A report row locked by a reporter's write is skipped.
_PURGE_TLS_REPORTS = text(
    "DELETE FROM public_mcp_domain_tls_reports WHERE domain_id IN ("
    "  SELECT domain_id FROM public_mcp_domain_tls_reports"
    "  WHERE valid_until <= now()"
    "  ORDER BY valid_until LIMIT :limit FOR UPDATE SKIP LOCKED"
    ") RETURNING domain_id"
)


@dataclass(frozen=True, slots=True)
class DueDomain:
    """A held row to check: the fence (``check_seq``) and what the lookup needs."""

    id: UUID
    check_seq: int
    hostname: str
    token: str


@dataclass(slots=True)
class MaintenanceCounts:
    """What one run did; integers only (published by the job status route)."""

    checked: int = 0
    proof_ok: int = 0
    proof_missing: int = 0
    dns_unavailable: int = 0
    suspended: int = 0
    reinstated: int = 0
    released: int = 0
    pending_expired: int = 0
    orphans_released: int = 0
    purged: int = 0
    fenced: int = 0
    budget_skipped: int = 0

    def as_dict(self) -> dict[str, int]:
        """The counts as a mapping, in declaration order."""
        return asdict(self)


# ----------------------------------------------------------------------------
# Housekeeping
# ----------------------------------------------------------------------------


async def _committed(db: AsyncSession, statement: TextClause, **params: object) -> list[Row]:
    """Run one housekeeping statement in its own transaction; its rows."""
    try:
        rows = list((await db.execute(statement, params)).all())
        await db.commit()
    except BaseException:
        await db.rollback()
        raise
    return rows


async def housekeeping(db: AsyncSession, counts: MaintenanceCounts) -> None:
    """Expire claims, purge cooled-down rows and release orphans (one batch each).

    Each step commits on its own; the audit row of a removed or released row
    is written in the step's transaction (``public_mcp_domain.released``,
    reason ``expired`` or ``orphaned``). Deleting a row whose cooldown has
    passed ends no hold and writes no audit row (its release was audited).
    """
    limit = HOUSEKEEPING_LIMIT

    expired = await _audited(
        db,
        _EXPIRE_PENDING,
        reason="expired",
        from_state="pending",
        params={"ttl_days": PENDING_TTL_DAYS, "limit": limit},
    )
    counts.pending_expired += expired

    purged = await _committed(db, _PURGE_RELEASED, limit=limit)
    counts.purged += len(purged)

    claims = await _audited(
        db,
        _DELETE_ORPHAN_CLAIMS,
        reason="orphaned",
        from_state="pending",
        params={"limit": limit},
    )
    holders = await _audited(
        db,
        _RELEASE_ORPHAN_HOLDERS,
        reason="orphaned",
        from_state=None,
        params={
            "limit": limit,
            "cooldown_hours": get_settings().mcp_public_domains_reassign_cooldown_hours,
        },
    )
    counts.orphans_released += claims + holders


async def _audited(
    db: AsyncSession,
    statement: TextClause,
    *,
    reason: str,
    from_state: str | None,
    params: dict[str, object],
) -> int:
    """Run a statement that deletes or releases rows, auditing each row in its transaction.

    The statement returns ``id, workspace_id, endpoint_id, hostname`` and, for
    a release, the state before it as a fifth column (``from_state`` None).
    """
    try:
        rows = (await db.execute(statement, params)).all()
        for row in rows:
            before = from_state if from_state is not None else row[4]
            _audit(
                db,
                SYSTEM_ACTOR,
                "released",
                domain_id=row[0],
                workspace_id=row[1],
                endpoint_id=row[2],
                hostname=row[3],
                from_state=before,
                to_state=None if before == "pending" else "released",
                reason=reason,
            )
        await db.commit()
    except BaseException:
        await db.rollback()
        raise
    return len(rows)


# ----------------------------------------------------------------------------
# Due rows
# ----------------------------------------------------------------------------


async def due_domains(db: AsyncSession, limit: int = MAINTENANCE_BATCH) -> list[DueDomain]:
    """The held rows to check now, in the order a run checks them.

    Rows whose last lookup was indeterminate come after all others, so that a
    resolver that never answers for some names cannot hold up the rest; then
    the oldest check first (never checked first of all).
    """
    db_now = literal_column(_DB_NOW)
    rows = (
        await db.execute(
            select(
                PublicMcpDomain.id,
                PublicMcpDomain.check_seq,
                PublicMcpDomain.hostname,
                PublicMcpDomain.verification_token,
            )
            .where(
                PublicMcpDomain.state.in_(HELD_STATES),
                or_(
                    PublicMcpDomain.last_checked_at.is_(None),
                    PublicMcpDomain.last_checked_at
                    < db_now - func.make_interval(0, 0, 0, 0, 0, 0, RECHECK_AGE_SECONDS),
                ),
            )
            .order_by(
                PublicMcpDomain.last_proof_result.is_not_distinct_from("unavailable"),
                PublicMcpDomain.last_checked_at.asc().nulls_first(),
            )
            .limit(limit)
        )
    ).all()
    return [DueDomain(row[0], int(row[1]), row[2], row[3]) for row in rows]


# ----------------------------------------------------------------------------
# One run
# ----------------------------------------------------------------------------


async def _check_one(sessions: SessionFactory, item: DueDomain, counts: MaintenanceCounts) -> None:
    """Look one row up (no session open), then apply the result in a new session."""
    check = await domain_service.lookup(item.hostname, item.token)
    counts.checked += 1
    if check.proof == "ok":
        counts.proof_ok += 1
    elif check.proof == "missing":
        counts.proof_missing += 1
    else:
        counts.dns_unavailable += 1
    async with sessions() as db:
        applied = await domain_service.apply_check(db, item.id, item.check_seq, check, SYSTEM_ACTOR)
    if applied.outcome == "fenced":
        counts.fenced += 1
    elif applied.audit_action == "suspended":
        counts.suspended += 1
    elif applied.audit_action == "reinstated":
        counts.reinstated += 1
    elif applied.audit_action == "released":
        counts.released += 1


async def _check_all(
    sessions: SessionFactory, due: list[DueDomain], deadline: float, counts: MaintenanceCounts
) -> None:
    """Check ``due`` with ``MAINTENANCE_CONCURRENCY`` workers; the first error ends the run."""
    queue: Iterator[DueDomain] = iter(due)
    failures: list[Exception] = []

    async def worker() -> None:
        for item in queue:
            if failures:
                return
            if _clock() >= deadline:
                counts.budget_skipped += 1
                continue
            try:
                await _check_one(sessions, item, counts)
            except Exception as exc:  # noqa: BLE001 - re-raised after the workers stop
                failures.append(exc)
                return

    workers = min(MAINTENANCE_CONCURRENCY, len(due))
    await asyncio.gather(*(worker() for _ in range(workers)))
    if failures:
        raise failures[0]


async def purge_expired_reports(sessions: SessionFactory) -> None:
    """Delete certificate reports past their ``valid_until`` (#2941); never fails the run.

    Only while reporting is configured. One bounded statement in a session of
    its own, after the checks: a failure (the table missing, say) is logged
    and the run goes on.
    """
    if not reporting_configured(get_settings()):
        return
    try:
        async with sessions() as db:
            await _committed(db, _PURGE_TLS_REPORTS, limit=HOUSEKEEPING_LIMIT)
    except Exception as exc:  # noqa: BLE001 - the optional table never stops the checks
        logger.error("public_mcp_domain_tls_report_purge_failed", error_type=type(exc).__name__)


async def run_maintenance(session_factory: SessionFactory | None = None) -> MaintenanceCounts:
    """One maintenance run: housekeeping, then the due checks within the budget.

    Args:
        session_factory: Opens the run's sessions (default: the application's).
            Each check opens its own short session after its lookup.

    Returns:
        The integer counts of the run.

    Raises:
        Exception: The first database error; the job records it as failed.
    """
    sessions = session_factory or _get_session_factory()
    deadline = _clock() + RUN_BUDGET_SECONDS
    counts = MaintenanceCounts()
    async with sessions() as db:
        await housekeeping(db, counts)
        due = await due_domains(db)
        await db.commit()  # I3: no connection is held during the lookups
    await _check_all(sessions, due, deadline, counts)
    await purge_expired_reports(sessions)
    logger.info("public_mcp_domain_maintenance_run", **counts.as_dict())
    return counts
