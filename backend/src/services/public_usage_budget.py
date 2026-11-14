"""The workspace-wide seven-day budget of anonymous public calls (#2805).

Contract: ``docs/public-usage-budget.md``. One row per workspace in
``public_usage_budgets``; one statement admits a call, starts a new period or
refuses it. Callers:

- ``mcp_server/public/dispatch.py`` charges every public ``tools/call`` that
  passed the per-minute buckets and the ``context_id`` check, before the
  handler runs;
- ``api/routes/public_search.py`` charges an anonymous REST search of a
  context with a live public MCP publication;
- the owner route reads it (``read_public_usage_budget``), the operator CLI
  reads and refills it (``reset_public_usage_budget``).

Invariants this module holds:

- a call is admitted only when the charge statement returned a row, i.e. the
  count was committed under the row lock; every other end refuses (exhausted)
  or raises ``PublicUsageBudgetUnavailable`` (fail closed);
- the charge runs on a session of its own and commits before the caller goes
  on, never on the caller's session or inside a longer transaction;
- every time is the database clock (``statement_timestamp()`` in UTC), so all
  processes agree on the period; ``now`` exists for tests only;
- nothing here refunds, decrements or deduplicates a charge. The only
  statement that lowers ``calls_used`` is the operator refill.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import get_settings
from db.base import isolated_session
from models.public_usage_budget import PublicUsageBudgetStatus
from utils.logger import get_logger

logger = get_logger(__name__)

PUBLIC_USAGE_BUDGET_PERIOD = timedelta(days=7)
CHARGE_LOCK_TIMEOUT_MS = 2000
CHARGE_TIMEOUT_SECONDS = 5.0
ANONYMOUS_RETRY_AFTER_CAP_SECONDS = 3600
THRESHOLD_LEVELS = (80, 95, 100)

_MIN_RETRY_AFTER_SECONDS = 60

# The statements are literal SQL. ``:now`` is NULL on every request path, so
# the clock is the database's ``statement_timestamp()`` (in UTC, one value per
# statement); a test may bind a fixed ``now`` instead. In the charge, the
# inserted row's ``period_started_at`` (``EXCLUDED``) carries that clock into
# the update. The period is ``PUBLIC_USAGE_BUDGET_PERIOD`` (a test pins the
# literal to the constant).
#
# The one statement that admits a call, starts a new period or refuses it.
# The conflicting row is locked and re-read at its latest version, so
# concurrent charges of one workspace serialize on it: none can admit past
# the limit, and exactly one of them starts the next period. No returned row
# means the period's calls are used; nothing was written then.
CHARGE_SQL = """\
INSERT INTO public_usage_budgets AS b
       (workspace_id, period_started_at, calls_used, period_seq, updated_at)
VALUES (:ws,
        COALESCE(CAST(:now AS timestamp), timezone('UTC', statement_timestamp())),
        1, 1,
        COALESCE(CAST(:now AS timestamp), timezone('UTC', statement_timestamp())))
ON CONFLICT (workspace_id) DO UPDATE SET
  period_started_at = CASE WHEN b.period_started_at + interval '7 days' <= EXCLUDED.period_started_at
                           THEN EXCLUDED.period_started_at ELSE b.period_started_at END,
  calls_used        = CASE WHEN b.period_started_at + interval '7 days' <= EXCLUDED.period_started_at
                           THEN 1 ELSE b.calls_used + 1 END,
  period_seq        = b.period_seq
                      + CASE WHEN b.period_started_at + interval '7 days' <= EXCLUDED.period_started_at
                             THEN 1 ELSE 0 END,
  updated_at        = EXCLUDED.updated_at
WHERE b.period_started_at + interval '7 days' <= EXCLUDED.period_started_at
   OR b.calls_used < :limit
RETURNING calls_used, period_started_at, period_seq"""
_CHARGE = text(CHARGE_SQL)

# After a refusal, on the same session: what the anonymous retry hint needs.
_REFUSED = text(
    "SELECT calls_used, period_started_at, period_seq, "
    "COALESCE(CAST(:now AS timestamp), timezone('UTC', statement_timestamp())) AS now "
    "FROM public_usage_budgets WHERE workspace_id = :ws"
)

_READ = text(
    "SELECT b.calls_used, b.period_started_at, b.period_seq, "
    "timezone('UTC', statement_timestamp()) AS now "
    "FROM (SELECT 1) AS one "
    "LEFT JOIN public_usage_budgets AS b ON b.workspace_id = :ws"
)
_DB_NOW = text("SELECT timezone('UTC', statement_timestamp())")

_RESET_PLAN = text(
    "SELECT calls_used, period_started_at, period_seq "
    "FROM public_usage_budgets WHERE workspace_id = :ws"
)
# The operator refill: the running period's count goes back to 0, its start,
# number and reset time stay. The only statement that lowers ``calls_used``.
_RESET_APPLY = text(
    "UPDATE public_usage_budgets AS b "
    "SET calls_used = 0, updated_at = timezone('UTC', statement_timestamp()) "
    "FROM (SELECT workspace_id, calls_used FROM public_usage_budgets "
    "WHERE workspace_id = :ws FOR UPDATE) AS old "
    "WHERE b.workspace_id = old.workspace_id "
    "RETURNING old.calls_used AS calls_used_before, b.period_started_at, b.period_seq"
)


def public_usage_budget_limit() -> int | None:
    """The budget's limit, or ``None`` when the operator switch is off.

    Callers read it once per request and decide once.
    """
    settings = get_settings()
    if not settings.mcp_public_usage_budget_enabled:
        return None
    return int(settings.mcp_public_usage_budget_calls)


class PublicUsageBudgetUnavailable(Exception):
    """The charge could not be completed; the caller refuses the call."""


@dataclass(frozen=True)
class BudgetCharge:
    """The result of one charge.

    Attributes:
        admitted: The call was counted and may run.
        calls_used: The period's count (after this call when admitted).
        period_started_at: Start of the period (naive UTC).
        period_seq: Number of the period.
        retry_after_seconds: Only on a refusal: the anonymous retry hint,
            already capped (60–3600).
    """

    admitted: bool
    calls_used: int
    period_started_at: datetime
    period_seq: int
    retry_after_seconds: int | None


class _BudgetRow(Protocol):
    calls_used: int
    period_started_at: datetime
    period_seq: int


def anonymous_retry_after(period_started_at: datetime, now: datetime) -> int:
    """Seconds an anonymous caller is told to wait: to the period end,
    rounded up to a minute, at least 60, at most 3600."""
    remaining = (period_started_at + PUBLIC_USAGE_BUDGET_PERIOD - now).total_seconds()
    rounded = math.ceil(remaining / 60) * 60
    return min(ANONYMOUS_RETRY_AFTER_CAP_SECONDS, max(_MIN_RETRY_AFTER_SECONDS, rounded))


def threshold_level(calls_used: int, limit: int) -> int | None:
    """The threshold level an admitted ``calls_used`` reaches exactly, if any.

    A level is reached at ``ceil(limit * level / 100)``; when several levels
    fall on the same count (a small limit), the highest one only.
    """
    reached = [level for level in THRESHOLD_LEVELS if calls_used == -(-limit * level // 100)]
    return max(reached) if reached else None


def budget_status(
    row: _BudgetRow | None, *, limit: int | None, now: datetime
) -> PublicUsageBudgetStatus:
    """The owner's view of a stored row at ``now`` (pure)."""
    period_days = PUBLIC_USAGE_BUDGET_PERIOD.days
    if limit is None:
        # Nulls, never 0: a 0 would read as exhausted.
        return PublicUsageBudgetStatus(
            state="disabled",
            limit=None,
            period_days=period_days,
            used=None,
            remaining=None,
            period_started_at=None,
            resets_at=None,
            period_seq=None,
            as_of=now,
        )
    if row is None or row.period_started_at + PUBLIC_USAGE_BUDGET_PERIOD <= now:
        return PublicUsageBudgetStatus(
            state="idle",
            limit=limit,
            period_days=period_days,
            used=0,
            remaining=limit,
            period_started_at=None,
            resets_at=None,
            period_seq=None,
            as_of=now,
        )
    used = int(row.calls_used)
    return PublicUsageBudgetStatus(
        state="exhausted" if used >= limit else "active",
        limit=limit,
        period_days=period_days,
        used=used,
        remaining=max(limit - used, 0),
        period_started_at=row.period_started_at,
        resets_at=row.period_started_at + PUBLIC_USAGE_BUDGET_PERIOD,
        period_seq=int(row.period_seq),
        as_of=now,
    )


def _log_admitted(workspace_id: UUID, charge: BudgetCharge, limit: int) -> None:
    if charge.calls_used == 1:
        logger.info(
            "public_usage_budget_period_started",
            workspace_id=str(workspace_id),
            period_started_at=charge.period_started_at.isoformat(),
            period_seq=charge.period_seq,
            limit=limit,
        )
    level = threshold_level(charge.calls_used, limit)
    if level is not None:
        logger.warning(
            "public_usage_budget_threshold",
            workspace_id=str(workspace_id),
            level=level,
            calls_used=charge.calls_used,
            limit=limit,
            period_seq=charge.period_seq,
        )


async def _charge(
    workspace_id: UUID, *, limit: int, db: AsyncSession | None, now: datetime | None
) -> BudgetCharge:
    params: dict[str, Any] = {"ws": workspace_id, "limit": limit, "now": now}
    async with isolated_session(db) as session:
        await session.execute(
            text("SELECT set_config('lock_timeout', :value, true)"),
            {"value": f"{int(CHARGE_LOCK_TIMEOUT_MS)}ms"},
        )
        admitted = (await session.execute(_CHARGE, params)).one_or_none()
        if admitted is not None:
            await session.commit()
            return BudgetCharge(
                admitted=True,
                calls_used=int(admitted.calls_used),
                period_started_at=admitted.period_started_at,
                period_seq=int(admitted.period_seq),
                retry_after_seconds=None,
            )
        refused = (await session.execute(_REFUSED, params)).one_or_none()
        await session.rollback()
        if refused is None:
            # The workspace (and its row by cascade) went away meanwhile.
            raise PublicUsageBudgetUnavailable("no budget row after a refused charge")
        return BudgetCharge(
            admitted=False,
            calls_used=int(refused.calls_used),
            period_started_at=refused.period_started_at,
            period_seq=int(refused.period_seq),
            retry_after_seconds=anonymous_retry_after(refused.period_started_at, refused.now),
        )


async def charge_public_usage(
    workspace_id: UUID,
    *,
    limit: int,
    db: AsyncSession | None = None,
    now: datetime | None = None,
) -> BudgetCharge:
    """Charge one public call to the workspace's budget, or refuse it.

    Args:
        workspace_id: The workspace of the endpoint the server loaded (never
            anything the client sent).
        limit: ``public_usage_budget_limit()`` as the caller read it.
        db: Only used to find the engine; the charge runs on a session of
            its own (``None``: the app's session factory).
        now: Tests only: a fixed clock instead of the database's.

    Returns:
        ``admitted=True`` when the call was counted and committed;
        ``admitted=False`` (nothing written) when the period's calls are used.

    Raises:
        PublicUsageBudgetUnavailable: The charge could not be completed (store
            failure, lock wait over ``CHARGE_LOCK_TIMEOUT_MS``, the whole
            charge over ``CHARGE_TIMEOUT_SECONDS``). The caller refuses the call.
    """
    try:
        async with asyncio.timeout(CHARGE_TIMEOUT_SECONDS):
            charge = await _charge(workspace_id, limit=limit, db=db, now=now)
    except Exception as exc:  # noqa: BLE001 - fail closed; CancelledError is not an Exception
        logger.warning(
            "public_usage_budget_unavailable",
            workspace_id=str(workspace_id),
            error_type=type(exc).__name__,
        )
        if isinstance(exc, PublicUsageBudgetUnavailable):
            raise
        raise PublicUsageBudgetUnavailable(type(exc).__name__) from exc
    if charge.admitted:
        _log_admitted(workspace_id, charge, limit)
    return charge


async def read_public_usage_budget(db: AsyncSession, workspace_id: UUID) -> PublicUsageBudgetStatus:
    """The owner's view of the workspace's budget: one SELECT, never a write.

    It never starts a period, never creates a row and never commits. With
    the switch off it does not touch the budget table at all.
    """
    limit = public_usage_budget_limit()
    if limit is None:
        now = (await db.execute(_DB_NOW)).scalar_one()
        return budget_status(None, limit=None, now=now)
    row = (await db.execute(_READ, {"ws": workspace_id})).one()
    stored = row if row.period_started_at is not None else None
    return budget_status(stored, limit=limit, now=row.now)


@dataclass(frozen=True)
class BudgetReset:
    """The operator refill of one workspace's running period.

    Attributes:
        workspace_id: The workspace.
        calls_used_before: The count before the refill.
        period_started_at: Start of the period (kept).
        period_seq: Number of the period (kept).
        applied: Whether the refill was written (``False`` on a dry run).
    """

    workspace_id: UUID
    calls_used_before: int
    period_started_at: datetime
    period_seq: int
    applied: bool


async def reset_public_usage_budget(
    db: AsyncSession, workspace_id: UUID, *, dry_run: bool
) -> BudgetReset:
    """Refill a workspace's running period (operator CLI).

    Sets ``calls_used`` to 0 and keeps the period's start, number and reset
    time. Commits on its own when applied.

    Raises:
        LookupError: The workspace has no stored period; nothing is created.
    """
    if dry_run:
        row = (await db.execute(_RESET_PLAN, {"ws": workspace_id})).one_or_none()
        if row is None:
            raise LookupError(f"no public usage budget is stored for workspace {workspace_id}")
        return BudgetReset(
            workspace_id=workspace_id,
            calls_used_before=int(row.calls_used),
            period_started_at=row.period_started_at,
            period_seq=int(row.period_seq),
            applied=False,
        )
    row = (await db.execute(_RESET_APPLY, {"ws": workspace_id})).one_or_none()
    if row is None:
        await db.rollback()
        raise LookupError(f"no public usage budget is stored for workspace {workspace_id}")
    await db.commit()
    logger.warning(
        "public_usage_budget_reset",
        workspace_id=str(workspace_id),
        period_seq=int(row.period_seq),
        calls_used_before=int(row.calls_used_before),
    )
    return BudgetReset(
        workspace_id=workspace_id,
        calls_used_before=int(row.calls_used_before),
        period_started_at=row.period_started_at,
        period_seq=int(row.period_seq),
        applied=True,
    )
