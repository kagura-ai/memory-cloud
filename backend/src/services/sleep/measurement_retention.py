"""Measurement-series retention purge (#1355) — growth control for the
HOW-MUCH lane (#1333).

The measurements table is append-only and unbounded by design (a series'
value IS its completeness), but nothing manages growth: an agent loop
recording at the workspace rate limit accrues hundreds of thousands of
rows per context per year, all RAW_EXPORTABLE. When
``sleep_measurement_retention_days > 0``, observations older than the
window are hard-deleted for the run's context, audited as one
batch-summary action — the ``forget_retention`` posture applied to a
table with no tombstones, no Qdrant points, and no undo path.

Scope discipline: the sweep runs ONLY for context-scoped sleep runs.
``measurements`` carries no user/workspace column, so a broader run has
no safe ownership predicate — skipping is fail-safe, not a limitation
in practice (sleep runs are per-context).

Default is **0 = disabled = retain forever** (the #1333 contract).
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, cast
from uuid import UUID

from sqlalchemy import delete
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from neural.config import NeuralMemoryConfig
    from services.sleep.reporter import SleepBudget, SleepReporter

from models.measurement import Measurement
from services.sleep.reporter import PhaseResult
from utils.datetime import utcnow
from utils.logger import get_logger

logger = get_logger(__name__)


class MeasurementRetentionPhase:
    """Hard-delete measurement observations past the declared window."""

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
        """Purge observations older than ``sleep_measurement_retention_days``.

        No-op (skipped) when the window is disabled (<= 0) or the run is
        not context-scoped.
        """
        result = PhaseResult(phase_name="measurement_retention")

        retention_days = int(getattr(config, "sleep_measurement_retention_days", 0) or 0)
        if retention_days <= 0:
            result.skipped = True
            result.skip_reason = "retention_disabled"
            return result
        if not context_id:
            # No ownership predicate exists on measurements beyond
            # context_id — never sweep outside an explicit context scope.
            result.skipped = True
            result.skip_reason = "no_context_scope"
            return result

        cutoff = utcnow() - timedelta(days=retention_days)
        delete_result = cast(
            CursorResult,
            await self.db.execute(
                delete(Measurement).where(
                    Measurement.context_id == UUID(context_id),
                    Measurement.measured_at < cutoff,
                )
            ),
        )
        purged = delete_result.rowcount or 0

        # Purged rows never consume the shared per-run budget (mirrors
        # merge/forget retention: a first-enable backlog purge must not
        # starve the live-memory phases). ``cutoff`` mirrors the
        # merge/forget details shape for report parity.
        result.details = {
            "purged": purged,
            "retention_days": retention_days,
            "cutoff": f"{cutoff:%Y-%m-%d %H:%M}",
        }

        if purged and reporter is not None and report_id is not None:
            # One batch-summary action — ids/counts only, never metric
            # names or values (they are user-authored free-form data).
            await reporter.add_action(
                report_id=report_id,
                phase="measurement_retention",
                action_type="purge",
                details=dict(result.details),
            )
        # Unconditional: an ENABLED run that purged 0 must be
        # distinguishable from a disabled/dead phase without a report
        # column (like forget_retention, this phase has none).
        logger.info(
            "measurement_retention_ran",
            context_id=context_id,
            purged=purged,
            retention_days=retention_days,
        )
        return result
