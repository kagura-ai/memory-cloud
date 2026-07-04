"""Sleep Maintenance Phase 6: Report.

Issue #101: Tracks sleep maintenance execution in sleep_reports table.
Issue #471: Per-(provider, model) cost-grade breakdown via the
``LLMCallBreakdown`` dataclass and ``embedding_*`` fields, persisted to
``sleep_report_llm_usage`` / ``sleep_reports`` by ``complete_report()``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from models.sleep import (
    SLEEP_REPORT_PAID_BY_VALUES,
    SLEEP_REPORT_SOURCES,
    SleepAction,
    SleepReport,
    SleepReportLLMUsage,
)
from utils.datetime import utcnow
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class LLMCallBreakdown:
    """Per-(provider, model) token usage from one or more LLM calls (#471).

    Phases append one entry per (provider, model) actually used during the
    phase. With today's single-model architecture (every phase reads
    ``config.sleep_llm_model``), each phase produces exactly one entry,
    and a typical sleep run produces 4 entries (one per LLM-using phase).
    The schema and reporter are designed to scale to per-call multi-model
    use without changing the call-site contract.

    ``tokenizer_version`` is audit-only — never used as a price-lookup
    key. See ``models/llm_pricing.py`` and #471 design references.
    """

    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    calls: int = 0
    tokenizer_version: str | None = None

    @property
    def total_tokens(self) -> int:
        """Reconstruct ``prompt_tokens + completion_tokens`` for the back-compat roll-up.

        ``input_tokens`` here is the *standard-rate* portion
        (``prompt_tokens - cached_input_tokens``) — see ``LLMResponse``
        docstring. Adding the three back together rebuilds
        ``prompt_tokens + completion_tokens``, which is what the
        pre-#471 ``sleep_reports.llm_tokens_used`` column carried. This
        is intentionally NOT a sum of "independent token classes" —
        ``cached_input_tokens`` is a sub-component of ``prompt_tokens``,
        not an additional axis. Use the per-class fields directly when
        computing cost.
        """
        return self.input_tokens + self.output_tokens + self.cached_input_tokens

    def add_call(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        cached_input_tokens: int,
    ) -> None:
        """Accumulate one LLM call's tokens into this breakdown.

        Used by phases that make multiple LLM calls against the same
        (provider, model) within a single phase (e.g. edge_discovery
        running N batches of pair judgments).
        """
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cached_input_tokens += cached_input_tokens
        self.calls += 1


def accumulate_llm_response(
    breakdown: LLMCallBreakdown | None,
    resp,  # services.llm_service.LLMResponse — late-bound to avoid import cycle
) -> LLMCallBreakdown:
    """Lazy-init + add_call combined for the four sleep phases (#471).

    Phases all share the same single-(provider, model) accumulation
    pattern — null-check, construct on first call, add tokens. Wrapping
    that in a helper means a future field rename on ``LLMResponse``
    only needs to be applied here, not in four phase files.
    """
    if breakdown is None:
        breakdown = LLMCallBreakdown(
            provider=resp.provider,
            model=resp.model,
            tokenizer_version=resp.tokenizer_version,
        )
    breakdown.add_call(
        input_tokens=resp.input_tokens,
        output_tokens=resp.output_tokens,
        cached_input_tokens=resp.cached_input_tokens,
    )
    return breakdown


@dataclass
class PhaseResult:
    """Result from a single sleep phase execution.

    Used by all phases to report back to the orchestrator.

    Issue #471 added the per-(provider, model) ``llm_breakdown`` list and
    the ``embedding_*`` cost-grade fields. The legacy scalar counters
    (``llm_calls_used`` / ``tokens_used`` / ``embedding_calls_used``)
    remain the source of the back-compat roll-up columns on
    ``sleep_reports``; reporter writes both. New code should consume
    ``llm_breakdown`` directly for cost computation.
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
    # #1183: judge-LLM calls that raised (complete_json failed). First-class so
    # the reporter can grade the RUN (completed/degraded/failed) without
    # spelunking per-phase details dicts. Counts failures only — successful
    # calls are ``sum(b.calls for b in llm_breakdown)`` (breakdown accumulates
    # inside the try block, after the response parsed).
    llm_call_failures: int = 0
    # #471: cost-grade fields.
    llm_breakdown: list[LLMCallBreakdown] = field(default_factory=list)
    embedding_provider: str | None = None
    embedding_model: str | None = None
    embedding_tokens: int = 0


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
        *,
        source: str = "sleep",
        paid_by: str = "platform",
    ) -> SleepReport:
        """Create a new sleep report with status='running'.

        The ``source`` and ``paid_by`` keyword arguments default to the
        scheduler-driven sleep run shape so existing callers receive the
        same behavior. Issue #495 broadlistening overrides them at its
        own call site with ``source='analysis'`` / ``paid_by='byok'``.

        Raises:
            ValueError: ``source`` not in ``SLEEP_REPORT_SOURCES`` or
                ``paid_by`` not in ``SLEEP_REPORT_PAID_BY_VALUES``.
                Service-layer validation surfaces a clear error rather
                than letting the call reach the DB and fail with
                ``IntegrityError`` from the CHECK constraint added by
                #523 (see ``models/sleep.py:35-36`` for the canonical
                tuples).
        """
        if source not in SLEEP_REPORT_SOURCES:
            raise ValueError(f"invalid source {source!r}; must be one of {SLEEP_REPORT_SOURCES}")
        if paid_by not in SLEEP_REPORT_PAID_BY_VALUES:
            raise ValueError(
                f"invalid paid_by {paid_by!r}; must be one of {SLEEP_REPORT_PAID_BY_VALUES}"
            )
        report = SleepReport(
            user_id=user_id,
            workspace_id=UUID(workspace_id) if workspace_id else None,
            context_id=UUID(context_id) if context_id else None,
            status="running",
            source=source,
            paid_by=paid_by,
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
        """Finalize a report with aggregated phase results.

        Writes three layers of cost telemetry (#471):

        1. ``sleep_report_llm_usage`` child rows — one per
           (phase, provider, model) actually used. Drives #472's
           ``GROUP BY`` cost-aggregation queries.
        2. Embedding scalar columns on ``sleep_reports`` — captures
           the (instance-global) embedding provider/model + token total,
           sourced from any phase that populated them
           (currently only reindex; #475 closes the phase 1/2 gap).
        3. Legacy roll-up columns (``llm_calls_made``, ``llm_tokens_used``,
           ``embedding_calls_made``) — populated as the sum of child rows
           for back-compat with existing dashboards / log analyzers.
        """
        # #1183: grade the run by judge-LLM health instead of blanket
        # "completed". A fully non-functional judge (every call raised — e.g.
        # a stale neural_config row pointing at a dead endpoint, see #1182)
        # used to be indistinguishable from a healthy run at the status level;
        # week1-derisk Day-5 shipped llm_call_failures=5/5 under ok=true.
        #   failed    — judge calls were attempted and ALL of them raised.
        #   degraded  — some judge calls raised, some succeeded.
        #   completed — no judge failures (including runs with no LLM work).
        # The tallies are RUN-WIDE, which is sound because all phases share
        # one (sleep_llm_provider, sleep_llm_model) today — a dead judge
        # config fails every phase uniformly (the Day-5 shape). If per-phase
        # LLM configs ever land, a single-phase total outage would grade only
        # 'degraded' here; per-phase counts stay visible in each phase blob's
        # details.llm_call_failures.
        judge_failures = sum(r.llm_call_failures for r in phase_results)
        judge_successes = sum(b.calls for r in phase_results for b in r.llm_breakdown)
        if judge_failures > 0 and judge_successes == 0:
            report.status = "failed"
            report.error_message = (
                f"llm_judge_total_failure: {judge_failures} judge call(s) attempted, 0 succeeded"
            )
        elif judge_failures > 0:
            report.status = "degraded"
        else:
            report.status = "completed"
        report.llm_call_failures = judge_failures
        report.completed_at = utcnow()

        # Single pass over phase_results: write child rows + per-phase
        # JSON blob + accumulate roll-ups + embedding scalars + the
        # divergence sanity check, all colocated. Phases that didn't
        # call the LLM (today: only reindex) contribute no child rows.
        # Embedding is instance-global per
        # ``backend/src/config/settings.py:86-93`` — one provider/model
        # per process, so we take the first non-empty pair and sum
        # tokens (today only reindex contributes; phase 1/2 gap is #475).
        # Legacy roll-up columns aggregate from ``result.llm_calls_used``
        # / ``result.tokens_used`` (post-#471 phases populate both the
        # legacy fields AND ``llm_breakdown`` — the divergence check
        # below catches the case where they disagree).
        total_llm_calls = 0
        total_tokens = 0
        total_embedding_calls = 0
        total_memories = 0
        embedding_tokens_total = 0

        for result in phase_results:
            for breakdown in result.llm_breakdown:
                self.db.add(
                    SleepReportLLMUsage(
                        report_id=report.id,
                        phase=result.phase_name,
                        provider=breakdown.provider,
                        model=breakdown.model,
                        input_tokens=breakdown.input_tokens,
                        output_tokens=breakdown.output_tokens,
                        cached_input_tokens=breakdown.cached_input_tokens,
                        calls=breakdown.calls,
                        tokenizer_version=breakdown.tokenizer_version,
                    )
                )

            embedding_tokens_total += result.embedding_tokens
            if report.embedding_provider is None and result.embedding_provider is not None:
                report.embedding_provider = result.embedding_provider
                report.embedding_model = result.embedding_model

            total_llm_calls += result.llm_calls_used
            total_tokens += result.tokens_used
            total_embedding_calls += result.embedding_calls_used
            total_memories += result.memories_processed

            # Sanity invariant: ``breakdown`` counts only successful
            # LLM calls (the accumulate_llm_response() call is inside
            # the ``try`` block, after the response is parsed).
            # ``llm_calls_used`` / ``tokens_used`` are populated from the
            # ``budget`` tracker, which in edge_discovery's pre-consume
            # pattern (``edge_discovery.py`` line 768) counts FAILED
            # calls too. So ``breakdown <= legacy`` is invariant; only
            # ``breakdown > legacy`` is a real bug (would mean the
            # phase is incorrectly accumulating breakdown for a call
            # the budget didn't track — a future-arch regression).
            breakdown_calls = sum(b.calls for b in result.llm_breakdown)
            breakdown_tokens = sum(b.total_tokens for b in result.llm_breakdown)
            if breakdown_calls > result.llm_calls_used or breakdown_tokens > result.tokens_used:
                logger.warning(
                    "phase_breakdown_exceeds_legacy",
                    phase=result.phase_name,
                    legacy_llm_calls=result.llm_calls_used,
                    breakdown_calls=breakdown_calls,
                    legacy_tokens=result.tokens_used,
                    breakdown_tokens=breakdown_tokens,
                )

            phase_data = {
                "success": result.success,
                "skipped": result.skipped,
                "skip_reason": result.skip_reason,
                "error": result.error,
                "llm_calls": result.llm_calls_used,
                "llm_call_failures": result.llm_call_failures,  # #1183
                "memories_processed": result.memories_processed,
                "details": result.details,
                "llm_breakdown": [
                    {
                        "provider": b.provider,
                        "model": b.model,
                        "input_tokens": b.input_tokens,
                        "output_tokens": b.output_tokens,
                        "cached_input_tokens": b.cached_input_tokens,
                        "calls": b.calls,
                    }
                    for b in result.llm_breakdown
                ],
                # #475: per-phase embedding usage (audit trail in JSONB
                # blob, no sleep_report_llm_usage schema change).
                "embedding_calls": result.embedding_calls_used,
                "embedding_tokens": result.embedding_tokens,
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

        report.embedding_tokens = embedding_tokens_total
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
            status=report.status,
            llm_calls=total_llm_calls,
            llm_call_failures=judge_failures,
            tokens=total_tokens,
            embedding_tokens=embedding_tokens_total,
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
