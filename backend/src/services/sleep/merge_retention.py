"""Merge-loser retention purge (#1209) — destructive deletion as an explicit,
telemetered second step.

Dedup merges soft-delete their losers (``deleted_by='sleep_maintenance'``)
and, since #1520, consolidation soft-deletes its archives
(``deleted_by='sleep_consolidation'``); both keep the action reversible via
``rollback_sleep_run`` — and grow storage forever. This phase
implements the declared retention window: when
``sleep_merge_retention_days > 0``, merge losers whose soft-deletion is older
than the window are hard-deleted, and the run's audit log records a batch
summary (one action, not one row per purge — a large backlog must not explode
``sleep_actions``).

Default is **0 = disabled = retain forever** (the pre-#1209 behavior). The
undo path (`services.sleep.undo`) names this setting in its error message
when a purged merge can no longer be restored — the rollback bound is
declared, not silent.

Merge losers' Qdrant vectors were already hard-deleted at merge time
(orphan-vector prevention), so the purge only removes Postgres rows.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import ColumnElement, Update, delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from neural.config import NeuralMemoryConfig
    from services.sleep.reporter import SleepBudget, SleepReporter

from models.auth import Context
from models.memory import SLEEP_TOMBSTONE_DELETED_BY, Memory
from services.sleep.reporter import PhaseResult
from utils.datetime import utcnow
from utils.logger import get_logger

logger = get_logger(__name__)


def sleep_tombstone_predicate() -> ColumnElement[bool]:
    """Rows Sleep maintenance tombstoned: merge losers AND consolidation archives
    (#1520). Both are restorable via ``rollback_sleep_run`` and both are purged
    by ``sleep_merge_retention_days``."""
    return Memory.deleted_by.in_(sorted(SLEEP_TOMBSTONE_DELETED_BY))


def user_tombstone_predicate() -> ColumnElement[bool]:
    """The complement: forget() rows (``deleted_by`` = actor sub) and legacy
    NULL rows that predate the column. Derived from the same set so a new sleep
    tombstone class can never fall into the user-forget window by omission."""
    return or_(
        Memory.deleted_by.is_(None),
        Memory.deleted_by.not_in(sorted(SLEEP_TOMBSTONE_DELETED_BY)),
    )


def restore_sleep_tombstone_stmt(memory_id: UUID, user_id: str, *, deleted_by: str) -> Update:
    """The one UPDATE that un-tombstones a sleep-maintained row (#1520 review).

    ``rowcount == 1`` must mean "a sleep tombstone of exactly this class was
    restored", never "some row for this user exists": the WHERE requires the
    row to be deleted, to carry the named ``deleted_by`` (a user's forget()
    tombstone under a stale archive/merge action is refused, mirroring the
    per-merge undo's ``not_merge_deleted``), and to live in a context that is
    not soft-deleted (a rollback must not plant a live row + vector inside a
    deleted context, where no retention sweep can reach it).
    """
    live_context = or_(
        Memory.context_id.is_(None),
        Memory.context_id.in_(select(Context.id).where(Context.deleted_at.is_(None))),
    )
    return (
        update(Memory)
        .where(
            Memory.id == memory_id,
            Memory.user_id == user_id,
            Memory.deleted_at.is_not(None),
            Memory.deleted_by == deleted_by,
            live_context,
        )
        .values(deleted_at=None, deleted_by=None)
    )


async def purge_tombstones(
    db: AsyncSession,
    *,
    phase_name: str,
    deleted_by_predicate,
    retention_days: int,
    user_id: str,
    workspace_id: str | None,
    context_id: str | None,
    reporter: SleepReporter | None = None,
    report_id: UUID | None = None,
) -> PhaseResult:
    """Shared retention sweep for soft-deleted memory rows (#1209 / #1336).

    One implementation for both tombstone classes (merge losers /
    user-forgotten rows) so the TOCTOU guard, budget exemption, and
    batch-audit contract cannot drift between the two phases — only the
    ``deleted_by_predicate`` and config key differ.
    """
    result = PhaseResult(phase_name=phase_name)

    if retention_days <= 0:
        result.skipped = True
        result.skip_reason = "retention_disabled"
        return result

    cutoff = utcnow() - timedelta(days=retention_days)

    stmt = select(Memory.id).where(
        Memory.user_id == user_id,
        deleted_by_predicate,
        Memory.deleted_at.is_not(None),
        Memory.deleted_at < cutoff,
    )
    if workspace_id:
        stmt = stmt.where(Memory.workspace_id == UUID(workspace_id))
    if context_id:
        stmt = stmt.where(Memory.context_id == UUID(context_id))

    ids = [row[0] for row in (await db.execute(stmt)).all()]
    if not ids:
        result.details = {
            "purged": 0,
            "retention_days": retention_days,
            "cutoff": f"{cutoff:%Y-%m-%d %H:%M}",
        }
        return result

    # Re-assert the full purge predicate in the DELETE itself (not just
    # the ids): a concurrent restore commits in its own session, and a
    # bare id-DELETE under READ COMMITTED would hard-delete the memory
    # that was just restored (TOCTOU between our SELECT and this DELETE).
    # With the predicate repeated, a restored row (deleted_at IS NULL) no
    # longer matches and survives.
    await db.execute(
        delete(Memory).where(
            Memory.id.in_(ids),
            deleted_by_predicate,
            Memory.deleted_at.is_not(None),
            Memory.deleted_at < cutoff,
        )
    )

    # Deliberately NOT counted into memories_processed: the orchestrator
    # consumes the shared per-run budget from that field, and purging an
    # unbounded historical backlog of dead rows must not starve the
    # live-memory phases (importance_reeval / consolidation) that run
    # after this one. The purge volume is reported in details + the
    # batch audit action instead.
    result.memories_processed = 0
    result.details = {
        "purged": len(ids),
        "retention_days": retention_days,
        "cutoff": f"{cutoff:%Y-%m-%d %H:%M}",
    }

    # Batch-summary audit action (deliberately NOT per-row): the purge is
    # the moment reversibility ends, so it must be visible in the same
    # audit log the deletions live in.
    if reporter and report_id:
        await reporter.add_action(
            report_id=report_id,
            phase=phase_name,
            action_type="purge",
            details={
                "purged": len(ids),
                "retention_days": retention_days,
                "cutoff": f"{cutoff:%Y-%m-%d %H:%M}",
            },
        )

    logger.info(
        f"{phase_name}_purged",
        purged=len(ids),
        retention_days=retention_days,
        user_id=user_id,
        context_id=context_id,
    )
    return result


class MergeRetentionPhase:
    """Hard-delete merge losers past the declared retention window."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def execute(
        self,
        config: NeuralMemoryConfig,
        user_id: str,
        workspace_id: str | None,
        context_id: str | None,
        budget: SleepBudget,
        *,
        reporter: SleepReporter | None = None,
        report_id: UUID | None = None,
    ) -> PhaseResult:
        """Purge merge losers older than ``sleep_merge_retention_days``.

        No-op (skipped) when the retention window is disabled (<= 0).
        """
        return await purge_tombstones(
            self.db,
            phase_name="merge_retention",
            deleted_by_predicate=sleep_tombstone_predicate(),
            retention_days=int(getattr(config, "sleep_merge_retention_days", 0) or 0),
            user_id=user_id,
            workspace_id=workspace_id,
            context_id=context_id,
            reporter=reporter,
            report_id=report_id,
        )
