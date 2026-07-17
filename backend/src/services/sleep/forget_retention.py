"""User-forget tombstone retention purge (#1336) — the merge_retention mirror
for rows soft-deleted by ``forget()``.

``forget()`` soft-deletes (``deleted_at`` + ``deleted_by=<user sub>``) and
already hard-deletes the Qdrant point and neural edges — but nothing ever
reaped the residual PG row, so a "forgotten" memory's ``details`` (home
coordinates included, via the WHERE axis) persisted at rest forever. When
``sleep_forget_retention_days > 0``, user-forgotten tombstones older than
the window are hard-deleted, audited as one batch-summary action.

Selection is the complement of merge_retention's: ``deleted_by`` is the
forgetting user's sub (or NULL on legacy rows), never the
``sleep_maintenance`` merge sentinel — merge losers keep their own window
(``sleep_merge_retention_days``) and undo path. The sweep mechanics
(TOCTOU-guarded DELETE, budget exemption, batch audit) are shared with
merge_retention via ``purge_tombstones`` so the two phases cannot drift.

Default is **0 = disabled = retain forever** (pre-#1336 behavior).
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from neural.config import NeuralMemoryConfig
    from services.sleep.reporter import SleepBudget, SleepReporter

from models.auth import Context
from models.memory import Memory
from services.sleep.merge_retention import _MERGE_DELETED_BY, purge_tombstones
from services.sleep.reporter import PhaseResult


class ForgetRetentionPhase:
    """Hard-delete user-forgotten tombstones past the declared window."""

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
        """Purge user-forgotten rows older than ``sleep_forget_retention_days``.

        No-op (skipped) when the retention window is disabled (<= 0).
        """
        # NULL deleted_by = legacy user-forget rows that predate the column
        # being written; the sentinel exclusion keeps merge losers on their
        # own retention window. deleted_by is deliberately NOT pinned to
        # this run's user sub: forget() records the ACTOR, so a teammate's
        # forget on this user's memory writes the teammate's sub — pinning
        # would strand those tombstones forever (no sleep run ever matches
        # them). With the live-context guard below, every remaining
        # non-merge tombstone is a forget() row whose point and edges were
        # already deleted at soft-delete time.
        not_merge_loser = or_(
            Memory.deleted_by.is_(None),
            Memory.deleted_by != _MERGE_DELETED_BY,
        )
        # Context soft-delete (#84 recovery design) tombstones its memories
        # with deleted_by=<user sub> too — but deliberately KEEPS their
        # Qdrant points for recovery, and they are indistinguishable from
        # forget() rows by deleted_by alone. Purging them would orphan live
        # vectors (full payloads immortal at rest, no PG row left to find
        # them by) and silently destroy context recovery. Restrict the sweep
        # to memories whose context is alive: forget() tombstones in live
        # contexts had their points and edges deleted at soft-delete time,
        # so the PG row is the only residue.
        in_live_context = Memory.context_id.in_(
            select(Context.id).where(Context.deleted_at.is_(None))
        )
        return await purge_tombstones(
            self.db,
            phase_name="forget_retention",
            deleted_by_predicate=and_(not_merge_loser, in_live_context),
            retention_days=int(getattr(config, "sleep_forget_retention_days", 0) or 0),
            user_id=user_id,
            workspace_id=workspace_id,
            context_id=context_id,
            reporter=reporter,
            report_id=report_id,
        )
