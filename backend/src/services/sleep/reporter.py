"""Sleep Maintenance Phase 6: Report.

Issue #101: Tracks sleep maintenance execution in sleep_reports table.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from models.sleep import SleepAction, SleepReport
from utils.datetime import utcnow
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class PhaseResult:
    """Result from a single sleep phase execution.

    Used by all phases to report back to the orchestrator.
    """

    phase_name: str
    success: bool = True
    skipped: bool = False
    skip_reason: str | None = None
    error: str | None = None
    llm_calls_used: int = 0
    tokens_used: int = 0
    embedding_calls_used: int = 0
    memories_processed: int = 0
    changed_memory_ids: set[UUID] = field(default_factory=set)
    details: dict | None = None


@dataclass
class SleepBudget:
    """Tracks LLM call and memory processing budget across phases.

    Shared across all phases in a single sleep run.
    Phases check can_afford() before each LLM batch call.
    """

    max_llm_calls: int = 50
    max_memories: int = 200
    llm_calls_used: int = 0
    memories_used: int = 0

    def can_afford(self, llm_calls: int = 0, memories: int = 0) -> bool:
        """Check if budget allows the requested operation."""
        return (
            self.llm_calls_used + llm_calls <= self.max_llm_calls
            and self.memories_used + memories <= self.max_memories
        )

    def consume(self, llm_calls: int = 0, memories: int = 0) -> None:
        """Record resource consumption."""
        self.llm_calls_used += llm_calls
        self.memories_used += memories

    @property
    def exhausted(self) -> bool:
        """Check if budget is fully exhausted."""
        return self.llm_calls_used >= self.max_llm_calls or self.memories_used >= self.max_memories


class SleepReporter:
    """Manages sleep_reports and sleep_actions lifecycle."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def create_report(
        self,
        user_id: str,
        workspace_id: str | None = None,
        context_id: str | None = None,
    ) -> SleepReport:
        """Create a new sleep report with status='running'."""
        report = SleepReport(
            user_id=user_id,
            workspace_id=UUID(workspace_id) if workspace_id else None,
            context_id=UUID(context_id) if context_id else None,
            status="running",
        )
        self.db.add(report)
        await self.db.flush()
        logger.info(
            "sleep_report_created",
            report_id=str(report.id),
            user_id=user_id,
            context_id=context_id,
        )
        return report

    async def add_action(
        self,
        report_id: UUID,
        phase: str,
        action_type: str,
        memory_id: UUID | None = None,
        target_id: UUID | None = None,
        details: dict | None = None,
    ) -> SleepAction:
        """Record an individual action in the audit log."""
        action = SleepAction(
            report_id=report_id,
            phase=phase,
            action_type=action_type,
            memory_id=memory_id,
            target_id=target_id,
            details=details,
        )
        self.db.add(action)
        return action

    async def complete_report(
        self,
        report: SleepReport,
        phase_results: list[PhaseResult],
    ) -> None:
        """Finalize a report with aggregated phase results."""
        report.status = "completed"
        report.completed_at = utcnow()

        # Aggregate stats from all phases
        total_llm_calls = 0
        total_tokens = 0
        total_embedding_calls = 0
        total_memories = 0

        for result in phase_results:
            total_llm_calls += result.llm_calls_used
            total_tokens += result.tokens_used
            total_embedding_calls += result.embedding_calls_used
            total_memories += result.memories_processed

            # Store per-phase results as JSON
            phase_data = {
                "success": result.success,
                "skipped": result.skipped,
                "skip_reason": result.skip_reason,
                "error": result.error,
                "llm_calls": result.llm_calls_used,
                "memories_processed": result.memories_processed,
                "details": result.details,
            }

            if result.phase_name == "edge_discovery":
                report.edge_discovery_result = phase_data
            elif result.phase_name == "dedup_merge":
                report.dedup_result = phase_data
            elif result.phase_name == "importance_reeval":
                report.importance_result = phase_data
            elif result.phase_name == "consolidation":
                report.consolidation_result = phase_data
            elif result.phase_name == "reindex":
                report.reindex_result = phase_data

        report.llm_calls_made = total_llm_calls
        report.llm_tokens_used = total_tokens
        report.embedding_calls_made = total_embedding_calls
        report.memories_processed = total_memories

        # Extract per-phase counters from details
        for result in phase_results:
            if not result.details:
                continue
            if result.phase_name == "edge_discovery":
                report.edges_created = result.details.get("edges_created", 0)
            elif result.phase_name == "dedup_merge":
                report.memories_merged = result.details.get("merged", 0)
            elif result.phase_name == "consolidation":
                report.memories_promoted = result.details.get(
                    "rule_promoted", 0
                ) + result.details.get("llm_promoted", 0)
                report.memories_flagged = result.details.get("borderline", 0)

        logger.info(
            "sleep_report_completed",
            report_id=str(report.id),
            llm_calls=total_llm_calls,
            tokens=total_tokens,
            memories=total_memories,
        )

    async def fail_report(self, report: SleepReport, error: str) -> None:
        """Mark a report as failed."""
        report.status = "failed"
        report.completed_at = utcnow()
        report.error_message = error
        logger.error(
            "sleep_report_failed",
            report_id=str(report.id),
            error=error,
        )
