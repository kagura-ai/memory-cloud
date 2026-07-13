"""Sleep Maintenance Phase 4: Consolidation.

Issue #103: Replace rule-based consolidation with LLM-augmented version.

Migrates logic from neural_tasks.py:consolidation_task (lines 92-225):
- Fast path: existing rules for clear-cut cases (no LLM needed)
- Borderline: LLM decides promote/keep/archive
- LLM off: identical behavior to legacy consolidation_task
- Bridge node protection: never delete memories with high centrality

Backward compatibility:
- When sleep_enabled=false, the legacy consolidation_task runs unchanged
- When sleep_enabled=true, this phase replaces it entirely
"""

from __future__ import annotations

import os
import random
import string
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from neural.config import NeuralMemoryConfig
    from services.sleep.reporter import SleepReporter

from sqlalchemy.ext.asyncio import AsyncSession

from db.qdrant import delete_memory_from_qdrant
from models.memory import Memory
from repositories.memory import MemoryRepository
from services.graph_service import GraphService
from services.llm_service import LLMService
from services.sleep.prompts import (
    CONSOLIDATION_JUDGE_SYSTEM,
    CONSOLIDATION_JUDGE_USER,
    wrap_untrusted_content,
)
from services.sleep.reporter import (
    LLMCallBreakdown,
    PhaseResult,
    SleepBudget,
    accumulate_llm_response,
)
from utils.datetime import utcnow
from utils.logger import get_logger

logger = get_logger(__name__)

# Batch size for LLM consolidation judgment
BATCH_SIZE = 5

# === Issue #1049: adoption-based consolidation thresholds (single source of truth) ===
# The promotion/archival gate now reads ``reference_count`` — the #1046 *adoption*
# signal, bumped only by a deliberate ``reference()`` — instead of the
# surfacing-inflated ``access_count`` (every recall top-k return + explore spread
# bumped that). Adoption is far sparser, so the old surfacing thresholds
# (access_count >= 3 / >= 5 / >= 1) are re-tuned DOWN to the adoption scale.
# Tests import these names directly (contract, not a copied literal).
ADOPTION_PROMOTE_MIN = 2  # adoption alone, importance-agnostic (was access_count >= 5)
ADOPTION_PROMOTE_WITH_IMPORTANCE = 1  # adoption + importance floor (was access_count >= 3)
PROMOTE_IMPORTANCE_FLOOR = 0.5
PROMOTE_HIGH_IMPORTANCE = 0.8
PROMOTE_HIGH_IMPORTANCE_MIN_AGE_DAYS = 3
AGED_PROMOTE_MIN_AGE_DAYS = 30
AGED_PROMOTE_ADOPTION_MIN = 1  # aged + any adoption (was access_count >= 1)
ARCHIVE_MIN_AGE_DAYS = 30


def _adoption_delete_cutoff() -> datetime | None:
    """Issue #1049 RELEASE BLOCKER — grandfather pre-adoption memories from archival.

    Historical adoption cannot be backfilled (``reference_count`` starts at 0 for
    every pre-#1046 row), so an ``age>=30 and adoption==0`` delete would wrongly
    archive every old memory. The adoption-based archival path therefore applies
    ONLY to memories created AT/AFTER this cutoff (``CONSOLIDATION_ADOPTION_DELETE_CUTOFF``,
    a naive-UTC ISO datetime — set it to the #1046/#1049 deploy date).

    Unset (default) → ``None`` → adoption-based deletion is **fully disabled**: the
    safest default that guarantees no pre-migration memory is archived. An operator
    opts in only once enough post-deploy adoption data has accrued. An unparseable
    value also returns None (fail-safe).
    """
    raw = os.getenv("CONSOLIDATION_ADOPTION_DELETE_CUTOFF")
    if not raw:
        return None
    try:
        cutoff = datetime.fromisoformat(raw)
    except ValueError:
        logger.warning("invalid_consolidation_adoption_delete_cutoff", value=raw)
        return None
    # Normalize to naive UTC to compare against the naive-UTC ``created_at`` column.
    if cutoff.tzinfo is not None:
        cutoff = cutoff.astimezone(UTC).replace(tzinfo=None)
    return cutoff


def _archival_eligible(memory: Memory, age_days: int, cutoff: datetime | None) -> bool:
    """Deterministic archival eligibility (#1049/#1229), defined once.

    The rule path ANDs this with the isolation check; the LLM path refuses
    any archive verdict that fails it — eligibility is never the judge's
    call, so the two paths cannot drift apart (#1229: the LLM path used to
    bypass all three gates, archiving memories written minutes earlier and
    violating the #1049 cutoff-unset RELEASE BLOCKER guarantee).
    """
    return (
        age_days >= ARCHIVE_MIN_AGE_DAYS
        and (memory.reference_count or 0) == 0
        and cutoff is not None
        and memory.created_at >= cutoff
    )


class ConsolidationPhase:
    """Consolidate working memories with optional LLM judgment."""

    def __init__(
        self, db: AsyncSession, llm_service: LLMService, collection_name: str | None = None
    ):
        self.db = db
        self.llm_service = llm_service
        self.memory_repo = MemoryRepository(db)
        self.collection_name = collection_name or "kagura_memories"
        # #1183: judge-failure counter (init here so the LLM judge helper can
        # be unit-tested without execute()).
        self._llm_failures: int = 0

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
        """Run consolidation phase."""
        result = PhaseResult(phase_name="consolidation")
        llm_calls_before = budget.llm_calls_used
        self._tokens_used = 0
        # #1183: judge calls that raised (feeds run-status grading).
        self._llm_failures = 0
        # #471: per-(provider, model) accumulator (lazy-init).
        self._llm_breakdown: LLMCallBreakdown | None = None

        # Fetch working memories
        working = await self._fetch_working_memories(user_id, workspace_id, context_id)
        if not working:
            result.details = {"message": "no_working_memories"}
            return result

        # Get neural metrics
        graph_service = GraphService(user_id, self.db, workspace_id, context_id)
        graph_stats = await graph_service.stats()
        has_graph = graph_stats["total_edges"] > 0

        # Thresholds from env (same as legacy consolidation_task)
        centrality_threshold = float(os.getenv("NEURAL_CENTRALITY_THRESHOLD", "0.7"))
        hub_threshold = int(os.getenv("NEURAL_HUB_NODE_THRESHOLD", "5"))
        weight_threshold = float(os.getenv("NEURAL_EDGE_WEIGHT_THRESHOLD", "0.8"))
        # Issue #1049: grandfather cutoff for the adoption==0 archival path (read once).
        adoption_delete_cutoff = _adoption_delete_cutoff()

        promoted = 0
        deleted = 0
        borderline: list[Memory] = []

        for memory in working:
            age_days = (utcnow() - memory.created_at).days

            # Get neural metrics if graph exists
            neural_metrics = None
            if has_graph:
                neural_metrics = await graph_service.get_node_metrics(str(memory.id))

            # === Fast path: rule-based, gated on ADOPTION (#1049) ===
            # ``reference_count`` (adoption) replaces the surfacing-inflated
            # ``access_count``. Thresholds are the named module constants above,
            # re-tuned for the sparser adoption scale. Neural-metric criteria are
            # unchanged. importance-only promotion (high importance + aged) is
            # access-agnostic and stays as-is.
            adoption = memory.reference_count or 0
            should_promote = (
                (
                    adoption >= ADOPTION_PROMOTE_WITH_IMPORTANCE
                    and memory.importance >= PROMOTE_IMPORTANCE_FLOOR
                )
                or (adoption >= ADOPTION_PROMOTE_MIN)
                or (
                    memory.importance >= PROMOTE_HIGH_IMPORTANCE
                    and age_days >= PROMOTE_HIGH_IMPORTANCE_MIN_AGE_DAYS
                )
                or (age_days >= AGED_PROMOTE_MIN_AGE_DAYS and adoption >= AGED_PROMOTE_ADOPTION_MIN)
                or (neural_metrics and neural_metrics["centrality"] >= centrality_threshold)
                or (neural_metrics and neural_metrics["edge_count"] >= hub_threshold)
                or (neural_metrics and neural_metrics["avg_edge_weight"] >= weight_threshold)
            )

            # Archival now gates on adoption==0, AND is grandfathered: only memories
            # created at/after the cutoff are eligible (RELEASE BLOCKER — see
            # ``_adoption_delete_cutoff``). cutoff=None → no adoption-based deletion.
            should_delete = _archival_eligible(memory, age_days, adoption_delete_cutoff) and (
                not neural_metrics or neural_metrics["is_isolated"]
            )

            if should_promote:
                await self.memory_repo.promote_to_persistent(memory.id)
                promoted += 1
                result.changed_memory_ids.add(memory.id)
                await self._record_action(
                    reporter,
                    report_id,
                    "promote",
                    memory.id,
                    "rule",
                    memory.importance,
                    memory.access_count,
                    age_days,
                    memory.reference_count or 0,
                )
            elif should_delete:
                try:
                    await delete_memory_from_qdrant(user_id, memory.id, self.collection_name)
                    await self.memory_repo.delete(memory.id)
                    deleted += 1
                    await self._record_action(
                        reporter,
                        report_id,
                        "archive",
                        memory.id,
                        "rule",
                        memory.importance,
                        memory.access_count,
                        age_days,
                        memory.reference_count or 0,
                    )
                except Exception as e:
                    logger.warning(
                        "consolidation_delete_failed",
                        memory_id=str(memory.id),
                        error=str(e),
                    )
            else:
                borderline.append(memory)

        # === LLM path for borderline cases ===
        llm_promoted = 0
        llm_archived = 0
        # #1229: archive verdicts refused because the memory was not
        # deterministically archival-eligible (visibility — never silent).
        llm_archive_guarded = 0
        llm_enabled = config.sleep_llm_provider != ""

        if borderline and llm_enabled:
            for batch_start in range(0, len(borderline), BATCH_SIZE):
                if not budget.can_afford(llm_calls=1):
                    break

                batch = borderline[batch_start : batch_start + BATCH_SIZE]
                decisions = await self._llm_judge_batch(
                    batch, user_id, context_id, workspace_id, budget, config
                )

                batch_map = {m.id: m for m in batch}
                for memory_id, action in decisions.items():
                    mem = batch_map.get(memory_id)
                    if not mem:
                        logger.warning(
                            "consolidation_llm_unknown_memory",
                            memory_id=str(memory_id),
                        )
                        continue
                    mem_age_days = (utcnow() - mem.created_at).days
                    if action == "promote":
                        await self.memory_repo.promote_to_persistent(memory_id)
                        llm_promoted += 1
                        result.changed_memory_ids.add(memory_id)
                        await self._record_action(
                            reporter,
                            report_id,
                            "promote",
                            memory_id,
                            "llm",
                            mem.importance,
                            mem.access_count,
                            mem_age_days,
                            mem.reference_count or 0,
                        )
                    elif action == "archive":
                        # #1229: the LLM only chooses AMONG deterministically
                        # archival-eligible candidates (shared predicate with
                        # the rule path above — see _archival_eligible).
                        if not _archival_eligible(mem, mem_age_days, adoption_delete_cutoff):
                            llm_archive_guarded += 1
                            logger.info(
                                "consolidation_llm_archive_guarded",
                                memory_id=str(memory_id),
                                age_days=mem_age_days,
                                adoption=mem.reference_count or 0,
                                cutoff_set=adoption_delete_cutoff is not None,
                            )
                            continue
                        neural = None
                        if has_graph:
                            neural = await graph_service.get_node_metrics(str(memory_id))
                        if not neural or neural["is_isolated"]:
                            await delete_memory_from_qdrant(
                                user_id, memory_id, self.collection_name
                            )
                            await self.memory_repo.delete(memory_id)
                            llm_archived += 1
                            await self._record_action(
                                reporter,
                                report_id,
                                "archive",
                                memory_id,
                                "llm",
                                mem.importance,
                                mem.access_count,
                                mem_age_days,
                                mem.reference_count or 0,
                            )

        result.memories_processed = len(working)
        result.llm_calls_used = budget.llm_calls_used - llm_calls_before
        result.tokens_used = self._tokens_used
        result.llm_call_failures = self._llm_failures  # #1183
        # #471: attach per-(provider, model) breakdown.
        if self._llm_breakdown is not None:
            result.llm_breakdown = [self._llm_breakdown]
        result.details = {
            "working_count": len(working),
            "rule_promoted": promoted,
            "rule_deleted": deleted,
            "borderline": len(borderline),
            "llm_promoted": llm_promoted,
            "llm_archived": llm_archived,
            # #1229: LLM archive verdicts blocked by the eligibility guard.
            "llm_archive_guarded": llm_archive_guarded,
            "llm_call_failures": self._llm_failures,  # #1183
        }

        logger.info(
            "consolidation_completed",
            working=len(working),
            promoted=promoted + llm_promoted,
            deleted=deleted + llm_archived,
        )

        return result

    @staticmethod
    async def _record_action(
        reporter: SleepReporter | None,
        report_id: UUID | None,
        action_type: str,
        memory_id: UUID,
        reason: str,
        importance: float,
        access_count: int,
        age_days: int,
        reference_count: int = 0,
    ) -> None:
        """Record a consolidation action if reporter is available.

        Issue #1049: records ``reference_count`` (the adoption signal the gate now
        keys on) alongside ``access_count`` (surfacing, kept for comparison) so
        operators can tune thresholds against the actual signal.
        """
        if reporter and report_id:
            await reporter.add_action(
                report_id=report_id,
                phase="consolidation",
                action_type=action_type,
                memory_id=memory_id,
                details={
                    "reason": reason,
                    "importance": importance,
                    "access_count": access_count,
                    "reference_count": reference_count,
                    "age_days": age_days,
                },
            )

    async def _fetch_working_memories(
        self,
        user_id: str,
        workspace_id: str | None,
        context_id: str | None,
    ) -> list[Memory]:
        """Fetch working-scope memories with workspace/context isolation."""
        from sqlalchemy import select

        stmt = select(Memory).where(
            Memory.user_id == user_id,
            Memory.scope == "working",
            Memory.deleted_at.is_(None),
        )
        if workspace_id:
            stmt = stmt.where(Memory.workspace_id == UUID(workspace_id))
        if context_id:
            stmt = stmt.where(Memory.context_id == UUID(context_id))

        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def _llm_judge_batch(
        self,
        batch: list[Memory],
        user_id: str,
        context_id: str | None,
        workspace_id: str | None,
        budget: SleepBudget,
        config: NeuralMemoryConfig,
    ) -> dict[UUID, str]:
        """Use LLM to judge borderline working memories.

        Returns {memory_id: "promote"|"keep"|"archive"}.
        """
        labels = list(string.ascii_uppercase[: len(batch)])
        label_to_id = dict(zip(labels, [m.id for m in batch], strict=True))

        items = list(zip(labels, batch, strict=True))
        random.shuffle(items)

        # The summary is untrusted (issue #919) — wrap it so an embedded
        # instruction cannot steer the consolidation (promote/keep/archive) judgment.
        memory_lines = []
        for label, mem in items:
            age_days = (utcnow() - mem.created_at).days
            memory_lines.append(
                f"[{label}] type={mem.type}, importance={mem.importance:.2f}, "
                f"access_count={mem.access_count}, age_days={age_days}, scope={mem.scope}\n"
                f"    summary:\n{wrap_untrusted_content(mem.summary)}"
            )

        prompt = CONSOLIDATION_JUDGE_USER.format(memories="\n".join(memory_lines))

        try:
            llm_resp = await self.llm_service.complete_json(
                user_id=user_id,
                prompt=prompt,
                system_prompt=CONSOLIDATION_JUDGE_SYSTEM,
                context_id=context_id,
                workspace_id=workspace_id,
                model=config.sleep_llm_model,
                provider=config.sleep_llm_provider,
            )
            budget.consume(llm_calls=1)
            self._tokens_used += llm_resp.total_tokens
            self._llm_breakdown = accumulate_llm_response(self._llm_breakdown, llm_resp)
        except Exception as e:
            self._llm_failures += 1  # #1183
            logger.warning("consolidation_llm_failed", error=str(e))
            return {}

        # Parse with label validation
        decisions: dict[UUID, str] = {}
        for item in llm_resp.parsed.get("decisions", []):
            label = item.get("label")
            action = item.get("action")
            if label not in label_to_id:
                continue
            if action not in ("promote", "keep", "archive"):
                continue
            decisions[label_to_id[label]] = action

        return decisions
