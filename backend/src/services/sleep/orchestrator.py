"""Sleep Maintenance Orchestrator.

Issue #101/#103: Coordinates all sleep maintenance phases in order:
  Phase 1: Edge Discovery
  Phase 2: Dedup/Merge
  Phase 3: Importance Re-eval
  Phase 4: Consolidation
  Phase 5: Reindex
  Phase 6: Report

Each phase is independently recoverable — if phase N fails,
phases N+1..6 still execute. Budget is shared across all phases.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from neural.config import NeuralMemoryConfig
from services.llm_service import LLMService
from services.sleep.consolidation import ConsolidationPhase
from services.sleep.dedup_merge import DedupMergePhase
from services.sleep.edge_discovery import EdgeDiscoveryPhase
from services.sleep.importance_reeval import ImportanceReevalPhase
from services.sleep.reindex import ReindexPhase
from services.sleep.reporter import PhaseResult, SleepBudget, SleepReporter
from utils.logger import get_logger

logger = get_logger(__name__)


class SleepOrchestrator:
    """Coordinates all sleep maintenance phases for a single user/context."""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.llm_service = LLMService(db)
        self.reporter = SleepReporter(db)

    async def run(
        self,
        user_id: str,
        workspace_id: str | None = None,
        context_id: str | None = None,
        *,
        config: NeuralMemoryConfig | None = None,
        dry_run: bool = False,
    ) -> None:
        """Execute full sleep maintenance cycle for one user/context.

        Args:
            user_id: Target user
            workspace_id: Target workspace (for 3-level isolation)
            context_id: Target context (for 3-level isolation)
            config: Optional pre-loaded config (loaded from DB if None)
            dry_run: If True, skip actual mutations (for testing)
        """
        # Load config if not provided
        if config is None:
            NeuralMemoryConfig.invalidate_cache()
            config = await NeuralMemoryConfig.from_db(self.db)

        budget = SleepBudget(
            max_llm_calls=config.sleep_max_llm_calls_per_run,
            max_memories=config.sleep_max_memories_per_run,
        )

        # Create report
        report = await self.reporter.create_report(user_id, workspace_id, context_id)

        phase_results: list[PhaseResult] = []
        changed_memory_ids: set[UUID] = set()

        try:
            # Phase 1: Edge Discovery
            result = await self._run_phase(
                "edge_discovery",
                EdgeDiscoveryPhase(self.db, self.llm_service),
                config,
                user_id,
                workspace_id,
                context_id,
                budget,
            )
            phase_results.append(result)
            changed_memory_ids.update(result.changed_memory_ids)

            # Phase 2: Dedup/Merge
            result = await self._run_phase(
                "dedup_merge",
                DedupMergePhase(self.db, self.llm_service),
                config,
                user_id,
                workspace_id,
                context_id,
                budget,
            )
            phase_results.append(result)
            changed_memory_ids.update(result.changed_memory_ids)

            # Phase 3: Importance Re-eval
            result = await self._run_phase(
                "importance_reeval",
                ImportanceReevalPhase(self.db, self.llm_service),
                config,
                user_id,
                workspace_id,
                context_id,
                budget,
            )
            phase_results.append(result)
            changed_memory_ids.update(result.changed_memory_ids)

            # Phase 4: Consolidation
            result = await self._run_phase(
                "consolidation",
                ConsolidationPhase(self.db, self.llm_service),
                config,
                user_id,
                workspace_id,
                context_id,
                budget,
            )
            phase_results.append(result)
            changed_memory_ids.update(result.changed_memory_ids)

            # Phase 5: Reindex (uses accumulated changed_memory_ids)
            reindex = ReindexPhase(self.db)
            reindex_result = await self._run_reindex(
                reindex,
                changed_memory_ids,
                user_id,
                workspace_id,
                context_id,
            )
            phase_results.append(reindex_result)

            # Phase 6: Complete report
            await self.reporter.complete_report(report, phase_results)

        except Exception as e:
            logger.error(
                "sleep_orchestrator_fatal",
                user_id=user_id,
                context_id=context_id,
                error=str(e),
                exc_info=True,
            )
            await self.reporter.fail_report(report, str(e))

    async def _run_phase(
        self,
        name: str,
        phase,
        config: NeuralMemoryConfig,
        user_id: str,
        workspace_id: str | None,
        context_id: str | None,
        budget: SleepBudget,
    ) -> PhaseResult:
        """Run a single phase with error isolation and budget check."""
        if budget.exhausted:
            return PhaseResult(
                phase_name=name,
                skipped=True,
                skip_reason="budget_exhausted",
            )

        try:
            result = await phase.execute(
                config,
                user_id,
                workspace_id,
                context_id,
                budget,
            )
            logger.info(
                "sleep_phase_completed",
                phase=name,
                success=result.success,
                skipped=result.skipped,
                memories=result.memories_processed,
                llm_calls=result.llm_calls_used,
            )
            return result

        except Exception as e:
            logger.error(
                "sleep_phase_failed",
                phase=name,
                error=str(e),
                exc_info=True,
            )
            return PhaseResult(
                phase_name=name,
                success=False,
                error=str(e),
            )

    async def _run_reindex(
        self,
        reindex: ReindexPhase,
        changed_memory_ids: set[UUID],
        user_id: str,
        workspace_id: str | None,
        context_id: str | None,
    ) -> PhaseResult:
        """Run reindex phase with error isolation."""
        try:
            return await reindex.execute(
                changed_memory_ids,
                user_id,
                workspace_id,
                context_id,
            )
        except Exception as e:
            logger.error(
                "sleep_phase_failed",
                phase="reindex",
                error=str(e),
                exc_info=True,
            )
            return PhaseResult(
                phase_name="reindex",
                success=False,
                error=str(e),
            )
