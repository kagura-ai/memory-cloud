"""Sleep Maintenance Orchestrator.

Issue #101/#103: Coordinates all sleep maintenance phases in order:
  Phase 1: Edge Discovery
  Phase 2: Dedup/Merge
  Phase 3: Importance Re-eval
  Phase 4: Consolidation
  Phase 5: Reindex
  Phase 6: Report

Each phase is independently recoverable — if phase N fails,
phases N+1..6 still execute. #3019: each phase has its own budget
(``SleepBudgetPlan``): the full memory allowance, and a share of the run's
LLM calls among the phases that call a model, so one phase cannot starve
another; a phase that calls no model is never skipped for budget.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.graph_learning_lock import lock_graph_learning
from models.auth import Context, Workspace
from models.sleep import SleepReport
from neural.config import NeuralMemoryConfig
from services.deferred_point_deletes import PointDelete
from services.llm_service import LLMService
from services.plan_suspension import sleep_suspended
from services.sleep.consolidation import ConsolidationPhase
from services.sleep.coverage import (
    CoverageState,
    DedupCoverage,
    JudgeCoverage,
    advance_dedup,
    advance_judge,
    current_state,
    dump_state,
)
from services.sleep.dedup_merge import DedupMergePhase
from services.sleep.edge_discovery import EdgeDiscoveryPhase
from services.sleep.forget_retention import ForgetRetentionPhase
from services.sleep.importance_reeval import ImportanceReevalPhase
from services.sleep.measurement_retention import MeasurementRetentionPhase
from services.sleep.merge_retention import MergeRetentionPhase
from services.sleep.reindex import ReindexPhase
from services.sleep.related_index import RelatedIndexPhase, index_enabled
from services.sleep.reporter import (
    LLMSpendTracker,
    PhaseResult,
    SleepBudget,
    SleepBudgetPlan,
    SleepReporter,
)
from services.workspace_slot_gate import slot_suspended
from utils.datetime import utcnow
from utils.logger import get_logger

logger = get_logger(__name__)

# Phase sets for each sleep mode
# #1209: merge_retention runs right after dedup_merge in full mode — it is
# the declared (default-disabled) purge window for merge losers.
FULL_PHASES = {
    "edge_discovery",
    "dedup_merge",
    "merge_retention",
    "forget_retention",
    "measurement_retention",
    "importance_reeval",
    "consolidation",
    "related_index",
}
EDGES_ONLY_PHASES = {"edge_discovery"}
# #1939: the non-LLM housekeeping (retention / privacy purges) of full mode.
# A workspace whose plan suspends Sleep still runs these — only the paid LLM
# passes pause.
RETENTION_PHASES = {"merge_retention", "forget_retention", "measurement_retention"}


def phase_calls_model(phase_name: str, config: NeuralMemoryConfig) -> bool:
    """Whether ``phase_name`` calls a model in a run with ``config`` (#3019).

    Edge discovery and dedup call one only with a Sleep LLM configured (they
    have a rule path without it); importance re-evaluation always does;
    consolidation only for its judge (its rule pass needs none). The
    retention phases and the related index never do.
    """
    has_llm = config.sleep_llm_provider != ""
    if phase_name == "edge_discovery":
        return bool(config.sleep_edge_discovery_enabled) and has_llm
    if phase_name == "dedup_merge":
        return bool(config.sleep_dedup_enabled) and has_llm
    if phase_name == "importance_reeval":
        return bool(config.sleep_importance_reeval_enabled)
    if phase_name == "consolidation":
        return bool(config.sleep_consolidation_judge_enabled) and has_llm
    return False


# #3019: consolidation shares the run's LLM calls for its judge but is never
# skipped at entry: its rule pass needs no model.
_NEVER_GATED_LLM_PHASES = {"consolidation"}

# #3019: the phases that work through the partition from a stored position.
COVERAGE_PHASES = {"dedup_merge", "consolidation"}


class SleepOrchestrator:
    """Coordinates all sleep maintenance phases for a single user/context."""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.llm_service = LLMService(db)
        self.reporter = SleepReporter(db)
        # #2565: the points the completed phases queued in this run's
        # transaction, for ``delete_committed_points`` after the caller's commit.
        self.pending_point_deletes: list[PointDelete] = []

    async def run(
        self,
        user_id: str,
        workspace_id: str | None = None,
        context_id: str | None = None,
        *,
        config: NeuralMemoryConfig | None = None,
        report: SleepReport | None = None,
    ) -> None:
        """Execute full sleep maintenance cycle for one user/context.

        Args:
            user_id: Target user
            workspace_id: Target workspace (for 3-level isolation)
            context_id: Target context (for 3-level isolation)
            config: Optional pre-loaded config (loaded from DB if None)
            report: Optional pre-created SleepReport. When provided (e.g. by
                the manual-trigger endpoint in issue #247), the orchestrator
                runs phases against the existing report instead of creating
                a new one, so the caller can return the report_id to the
                client before phase execution completes.
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
            # If a report was passed in, mark it completed so it does not
            # stay in 'running' state forever.
            if report is not None:
                report.status = "completed"
                report.completed_at = utcnow()
            return

        # Determine which phases to run based on sleep_mode
        from services.sync_managed_guard import assert_context_mutable

        await assert_context_mutable(self.db, context_id)
        if sleep_mode == "edges_only":
            allowed_phases = EDGES_ONLY_PHASES
        elif sleep_mode == "suspended":
            allowed_phases = RETENTION_PHASES
        else:
            allowed_phases = FULL_PHASES

        # #1985: no learning while asleep. The phases share this transaction
        # (the caller commits after run()), so the graph learning lock taken
        # here holds until then: a live recall learning pass for this user is
        # skipped, and this waits only for a pass already running.
        await lock_graph_learning(self.db, user_id)

        # #3019: the positions the last run of this partition left, read under
        # the lock (it serialises the runs of one user, so that run's commit
        # is visible), and one clock for the whole run.
        now = utcnow()
        coverage = CoverageState()
        if allowed_phases & COVERAGE_PHASES:
            coverage = await current_state(self.db, user_id, context_id) or CoverageState()

        # Get context's embedding model and Qdrant collection name
        embedding_model, collection_name = await self._get_context_embedding_info(context_id)

        # Create report if caller did not supply one
        if report is None:
            report = await self.reporter.create_report(user_id, workspace_id, context_id)

        phase_results: list[PhaseResult] = []
        changed_memory_ids: set[UUID] = set()
        # #2015 D5: the subset whose content changed (a phase that does not
        # classify contributes all of its changed ids).
        content_changed_memory_ids: set[UUID] = set()

        # Phase definitions: (name, factory)
        em = embedding_model
        cn = collection_name
        phases = [
            ("edge_discovery", lambda: EdgeDiscoveryPhase(self.db, self.llm_service, em, cn)),
            (
                "dedup_merge",
                lambda: DedupMergePhase(self.db, self.llm_service, em, cn, coverage=coverage.dedup),
            ),
            ("merge_retention", lambda: MergeRetentionPhase(self.db)),
            ("forget_retention", lambda: ForgetRetentionPhase(self.db)),
            ("measurement_retention", lambda: MeasurementRetentionPhase(self.db)),
            ("importance_reeval", lambda: ImportanceReevalPhase(self.db, self.llm_service, cn)),
            (
                "consolidation",
                lambda: ConsolidationPhase(self.db, self.llm_service, cn, coverage=coverage.judge),
            ),
        ]
        if index_enabled():
            # Capture discovery's evidence before the dedup merge stamps a
            # winner's updated_at (importance re-evaluation no longer stamps
            # it, #3019). Later source edits still fail the fingerprint.
            phases.insert(1, ("related_index", lambda: RelatedIndexPhase(self.db)))

        # #3019: one budget per phase, the LLM calls shared among the phases
        # of this run that call a model.
        plan = SleepBudgetPlan(
            max_llm_calls=config.sleep_max_llm_calls_per_run,
            max_memories=config.sleep_max_memories_per_run,
            llm_phases=[
                name
                for name, _ in phases
                if name in allowed_phases and phase_calls_model(name, config)
            ],
        )

        # #3019: the phase instances, for their coverage outcome.
        instances: dict[str, object] = {}
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

                budget = plan.for_phase(phase_name)
                phase = instances[phase_name] = phase_factory()
                result = await self._run_phase(
                    phase_name,
                    phase,
                    config,
                    user_id,
                    workspace_id,
                    context_id,
                    budget,
                    report_id=report.id,
                    gate_on_budget=(
                        plan.calls_model(phase_name) and phase_name not in _NEVER_GATED_LLM_PHASES
                    ),
                )
                plan.settle(phase_name, budget)
                phase_results.append(result)
                changed_memory_ids.update(result.changed_memory_ids)
                content_changed_memory_ids.update(
                    result.changed_memory_ids
                    if result.content_changed_memory_ids is None
                    else result.content_changed_memory_ids
                )

            # Phase 5: Reindex always runs if there are changes
            reindex = ReindexPhase(self.db, em, cn)
            reindex_result = await self._run_reindex(
                reindex,
                changed_memory_ids,
                user_id,
                workspace_id,
                context_id,
                content_changed_memory_ids=content_changed_memory_ids,
            )
            phase_results.append(reindex_result)

            # #3019: the positions this run leaves for the next one, assigned
            # inside the run (a run that fails is rolled back with them).
            self._write_coverage(report, coverage, instances, phase_results, now=now)

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
            raise

    @staticmethod
    def _write_coverage(
        report: SleepReport,
        coverage: CoverageState,
        instances: dict[str, object],
        phase_results: list[PhaseResult],
        *,
        now: datetime,
    ) -> None:
        """Set ``report.coverage_state`` from the phases' outcomes (#3019).

        Only a phase that read its window has an outcome (also when it raised
        after the read: every row is then a failure). A phase that was skipped
        or raised before its read carries its entry unchanged. A run in which
        neither phase produced an outcome leaves the column NULL, and the next
        run reads the state of the run before. The whole state is written: the
        entries this run advanced and the others as they were. Each phase
        with an outcome gets ``details["coverage"]``, saying what moved and how
        many rows the stall rule passed; a phase that raised has no details of
        its own, so its details become that one key.
        """
        results = {r.phase_name: r for r in phase_results}

        def returned(name: str) -> bool:
            result = results.get(name)
            return result is not None and result.success

        def report_on(name: str, report_details: dict | None) -> None:
            result = results.get(name)
            if report_details is None or result is None:
                return
            if not isinstance(result.details, dict):
                result.details = {}
            result.details["coverage"] = report_details

        dedup_window = getattr(instances.get("dedup_merge"), "coverage_outcome", None)
        if not isinstance(dedup_window, DedupCoverage):
            dedup_window = None
        judge_window = getattr(instances.get("consolidation"), "coverage_outcome", None)
        if not isinstance(judge_window, JudgeCoverage):
            judge_window = None
        if dedup_window is None and judge_window is None:
            return
        lanes, dedup_report = advance_dedup(
            coverage.dedup, dedup_window, returned=returned("dedup_merge"), now=now
        )
        judge, judge_report = advance_judge(
            coverage.judge, judge_window, returned=returned("consolidation")
        )
        report.coverage_state = dump_state(CoverageState(dedup=lanes, judge=judge))
        report_on("dedup_merge", dedup_report)
        report_on("consolidation", judge_report)

    async def delete_committed_points(self) -> None:
        """Delete the points this run queued; call only after the caller's commit.

        #2565: the merge step queues each point in
        ``deferred_point_deletes`` beside the tombstone, inside the phase's
        savepoint (``pending_point_deletes``), instead of deleting it before
        the commit — a run that rolled back left live rows without their
        vectors. This is ``forget``'s post-commit step
        (``MemoryService.delete_forgotten_points``): under the point-writer
        lock, only for points no live row names, bounded, and it never raises;
        what it leaves stays queued for the drain job. A caller that rolled
        back does not call it, and its queue rows are gone with the run.
        """
        entries, self.pending_point_deletes = self.pending_point_deletes, []
        if not entries:
            return
        from services.memory_service import MemoryService

        await MemoryService(self.db).delete_forgotten_points(entries)

    async def _run_phase(
        self,
        name: str,
        phase,
        config: NeuralMemoryConfig,
        user_id: str,
        workspace_id: str | None,
        context_id: str | None,
        budget: SleepBudget,
        report_id: UUID,
        *,
        gate_on_budget: bool = False,
    ) -> PhaseResult:
        """Run a single phase with error isolation.

        ``budget`` is the phase's own (#3019). ``gate_on_budget`` skips the
        phase with ``skip_reason="budget_exhausted"`` when that budget is
        already used up at entry; the orchestrator sets it only for a phase
        that calls a model and cannot run without one, so the retention
        phases, the related index and consolidation's rule pass always run.
        """
        if gate_on_budget and budget.exhausted:
            return PhaseResult(
                phase_name=name,
                skipped=True,
                skip_reason="budget_exhausted",
            )

        # #2060: the phase's spend, for a phase that raises.
        llm_calls_before = budget.llm_calls_used
        try:
            # #1229: every phase runs inside its own SAVEPOINT. A phase that
            # dies mid-flush used to invalidate the WHOLE shared transaction
            # — every later phase and the report write then failed with
            # PendingRollbackError, so one crashed phase silently killed the
            # rest of the run. Rolling back to the savepoint (which the
            # context manager does on exception) discards ONLY the failed
            # phase's pending work: the SleepReport row, earlier phases'
            # work, and the outer transaction all stay intact — including
            # for direct statement errors that abort the DB transaction
            # without tripping a flush (savepoints restore those too).
            async with self.db.begin_nested():
                result = await phase.execute(
                    config,
                    user_id,
                    workspace_id,
                    context_id,
                    budget,
                    reporter=self.reporter,
                    report_id=report_id,
                )
            # #2565: only a phase whose savepoint held; a failed one's queue
            # rows went with it.
            self.pending_point_deletes.extend(getattr(phase, "pending_point_deletes", ()))
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
            result = PhaseResult(
                phase_name=name,
                success=False,
                error=str(e),
            )
            # #2060: the savepoint rolled back the phase's DB work, but the
            # LLM calls it made before raising were billed. Carry that spend
            # so it reaches sleep_reports and sleep_report_llm_usage.
            if isinstance(phase, LLMSpendTracker):
                phase.record_spend(result)
                result.llm_calls_used = budget.llm_calls_used - llm_calls_before
            return result

    async def _get_sleep_mode(self, context_id: str | None) -> str:
        """Get the effective sleep_mode for a context.

        Defaults to 'skip' if the context is not found. When the context's
        workspace has Sleep suspended — it fell back to a plan with no Sleep
        allowance (#1939) — a stored 'full' becomes 'suspended' (only the
        non-LLM retention phases run, so purges stay on schedule) and a
        stored 'edges_only' becomes 'skip' (it never ran retention). The
        stored ``Context.sleep_mode`` is left as it is, so re-subscribing
        resumes Sleep with no further action. Every run (nightly sweep, admin
        trigger) reads its mode here. #2918: a slot-suspended workspace is
        'skip' (nothing runs until it is resumed; the stored mode is kept).
        """
        if not context_id:
            return "skip"
        try:
            stmt = select(Context).where(Context.id == UUID(context_id))
            result = await self.db.execute(stmt)
            context = result.scalar_one_or_none()
            if context and context.sleep_mode:
                if context.sleep_mode != "skip" and await slot_suspended(
                    self.db, context.workspace_id
                ):
                    logger.info(
                        "sleep_skipped_workspace_slot_suspended",
                        context_id=context_id,
                        workspace_id=str(context.workspace_id),
                    )
                    return "skip"
                if context.sleep_mode != "skip":
                    workspace = await self.db.get(Workspace, context.workspace_id)
                    if workspace is not None and sleep_suspended(workspace):
                        logger.info(
                            "sleep_suspended_by_plan",
                            context_id=context_id,
                            workspace_id=str(context.workspace_id),
                        )
                        return "suspended" if context.sleep_mode == "full" else "skip"
                return context.sleep_mode
        except Exception as e:
            logger.warning(
                "sleep_mode_lookup_failed",
                context_id=context_id,
                error=str(e),
            )
        return "skip"

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
        *,
        content_changed_memory_ids: set[UUID] | None = None,
    ) -> PhaseResult:
        """Run reindex phase with error isolation."""
        try:
            return await reindex.execute(
                changed_memory_ids,
                user_id,
                workspace_id,
                context_id,
                content_changed_memory_ids=content_changed_memory_ids,
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
