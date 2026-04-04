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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import Context
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

# Phase sets for each sleep mode
FULL_PHASES = {"edge_discovery", "dedup_merge", "importance_reeval", "consolidation"}
EDGES_ONLY_PHASES = {"edge_discovery"}


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

        # Determine sleep mode for this context
        sleep_mode = await self._get_sleep_mode(context_id)
        if sleep_mode == "skip":
            logger.info(
                "sleep_orchestrator_skipped",
                user_id=user_id,
                context_id=context_id,
                reason="context_sleep_mode_skip",
            )
            return

        # Determine which phases to run based on sleep_mode
        if sleep_mode == "edges_only":
            allowed_phases = EDGES_ONLY_PHASES
        else:
            allowed_phases = FULL_PHASES

        # Get context's embedding model and Qdrant collection name
        embedding_model, collection_name = await self._get_context_embedding_info(context_id)

        budget = SleepBudget(
            max_llm_calls=config.sleep_max_llm_calls_per_run,
            max_memories=config.sleep_max_memories_per_run,
        )

        # Create report
        report = await self.reporter.create_report(user_id, workspace_id, context_id)

        phase_results: list[PhaseResult] = []
        changed_memory_ids: set[UUID] = set()

        # Phase definitions: (name, factory)
        em = embedding_model
        cn = collection_name
        phases = [
            ("edge_discovery", lambda: EdgeDiscoveryPhase(self.db, self.llm_service, em, cn)),
            ("dedup_merge", lambda: DedupMergePhase(self.db, self.llm_service, em, cn)),
            ("importance_reeval", lambda: ImportanceReevalPhase(self.db, self.llm_service, cn)),
            ("consolidation", lambda: ConsolidationPhase(self.db, self.llm_service, cn)),
        ]

        try:
            # Run phases 1-4 based on sleep_mode
            for phase_name, phase_factory in phases:
                if phase_name not in allowed_phases:
                    phase_results.append(
                        PhaseResult(
                            phase_name=phase_name,
                            skipped=True,
                            skip_reason=f"sleep_mode_{sleep_mode}",
                        )
                    )
                    continue

                result = await self._run_phase(
                    phase_name,
                    phase_factory(),
                    config,
                    user_id,
                    workspace_id,
                    context_id,
                    budget,
                )
                phase_results.append(result)
                changed_memory_ids.update(result.changed_memory_ids)

            # Phase 5: Reindex always runs if there are changes
            reindex = ReindexPhase(self.db, em, cn)
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

    async def _get_sleep_mode(self, context_id: str | None) -> str:
        """Get sleep_mode for a context. Defaults to 'full' if not found."""
        if not context_id:
            return "full"
        try:
            stmt = select(Context).where(Context.id == UUID(context_id))
            result = await self.db.execute(stmt)
            context = result.scalar_one_or_none()
            if context and context.sleep_mode:
                return context.sleep_mode
        except Exception as e:
            logger.warning(
                "sleep_mode_lookup_failed",
                context_id=context_id,
                error=str(e),
            )
        return "full"

    async def _get_context_embedding_info(self, context_id: str | None) -> tuple[str | None, str]:
        """Get embedding model and Qdrant collection name for a context.

        Returns:
            Tuple of (embedding_model, collection_name)
        """
        from db.qdrant import KAGURA_MEMORIES_COLLECTION, get_collection_name

        if not context_id:
            return None, KAGURA_MEMORIES_COLLECTION
        try:
            from models.config import ContextSearchConfig

            stmt = select(ContextSearchConfig).where(
                ContextSearchConfig.context_id == UUID(context_id)
            )
            result = await self.db.execute(stmt)
            search_config = result.scalar_one_or_none()
            if search_config and search_config.embedding_model:
                collection = get_collection_name(
                    search_config.embedding_model,
                    search_config.embedding_dimensions,
                )
                return search_config.embedding_model, collection
        except Exception as e:
            logger.warning(
                "embedding_info_lookup_failed",
                context_id=context_id,
                error=str(e),
            )
        return None, KAGURA_MEMORIES_COLLECTION

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
