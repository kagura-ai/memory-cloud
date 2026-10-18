"""Sleep Maintenance Phase 3: Importance Re-evaluation.

Issue #103: Re-evaluate memory importance using LLM judgment
combined with behavioral signals via EMA smoothing.

EMA formula: new_importance = α * llm_score + (1-α) * old_importance
Default α = 0.3 (from existing importance_ema_alpha config).

Academic notes:
- α=0.3 means a single LLM call shifts importance by max 30%.
- With daily runs, 95% convergence after ~7 runs (geometric series).
- This prevents a single bad LLM judgment from destroying importance.
- Boundary clamping ensures importance stays in [0.0, 1.0].
"""

from __future__ import annotations

import random
import string
from datetime import timedelta
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

if TYPE_CHECKING:
    from neural.config import NeuralMemoryConfig
    from services.sleep.reporter import SleepReporter

from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from db.qdrant import update_memory_payload_in_qdrant
from models.memory import Memory, not_pinned_predicate, not_tool_triggered_predicate
from services.llm_service import LLMService, billed_usage
from services.resource_indexer import owns_resource_point
from services.sleep.judge_lane import judge_platform_only
from services.sleep.prompts import (
    IMPORTANCE_REEVAL_SYSTEM,
    IMPORTANCE_REEVAL_USER,
    wrap_untrusted_content,
)
from services.sleep.reporter import (
    LLMCallBreakdown,
    LLMSpendTracker,
    PhaseResult,
    SleepBudget,
    accumulate_llm_response,
)
from utils.datetime import utcnow
from utils.logger import get_logger

logger = get_logger(__name__)

# Memories with importance in this range are candidates for re-eval.
# Extreme values (very high/low) are typically well-calibrated already.
IMPORTANCE_MIN = 0.2
IMPORTANCE_MAX = 0.8

# Minimum staleness before re-evaluation (days since last update)
STALENESS_DAYS = 7

# Max memories per LLM batch call
BATCH_SIZE = 10


class ImportanceReevalPhase(LLMSpendTracker):
    """Re-evaluate memory importance using LLM + EMA smoothing."""

    def __init__(
        self, db: AsyncSession, llm_service: LLMService, collection_name: str | None = None
    ):
        self.db = db
        self.llm_service = llm_service
        self.collection_name = collection_name
        # #1183: judge-failure counter (init here so ``_evaluate_batch`` can
        # be unit-tested without execute()).
        self._llm_failures: int = 0
        # #2015: per-context "does a payload change reach chunk points".
        self._cascade_by_context: dict[str | None, bool] = {}

    async def _chunk_cascade(self, context_id: str | None) -> bool:
        """Whether the importance sync must also reach content chunk points.

        A context with ``content_chunks_enabled`` off has none, so the extra
        Qdrant call is skipped; an unknown context cascades (the safe side).
        """
        if context_id not in self._cascade_by_context:
            if context_id is None:
                self._cascade_by_context[context_id] = True
            else:
                from services.content_chunk_writer import chunk_cascade_needed

                self._cascade_by_context[context_id] = await chunk_cascade_needed(
                    self.db, UUID(context_id)
                )
        return self._cascade_by_context[context_id]

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
        """Run importance re-evaluation phase."""
        # #2015 D5: importance only, no content change (its chunk points
        # keep their vectors; the reindex syncs their payload).
        result = PhaseResult(phase_name="importance_reeval", content_changed_memory_ids=set())
        llm_calls_before = budget.llm_calls_used
        self._tokens_used = 0
        # #1183: judge calls that raised (feeds run-status grading).
        self._llm_failures = 0
        # #471: per-(provider, model) accumulator (lazy-init).
        self._llm_breakdown: LLMCallBreakdown | None = None

        if not config.sleep_importance_reeval_enabled:
            result.skipped = True
            result.skip_reason = "importance_reeval_disabled"
            return result

        alpha = config.importance_ema_alpha  # Reuse existing config param

        # Fetch stale memories with mid-range importance
        candidates = await self._fetch_candidates(user_id, workspace_id, context_id)
        if not candidates:
            result.details = {"message": "no_stale_memories"}
            return result

        updated_count = 0

        # Process in batches
        for batch_start in range(0, len(candidates), BATCH_SIZE):
            if not budget.can_afford(llm_calls=1):
                break

            batch = candidates[batch_start : batch_start + BATCH_SIZE]

            scores = await self._evaluate_batch(
                batch, user_id, context_id, workspace_id, budget, config
            )

            for memory_id, new_score in scores.items():
                old_memory = next((m for m in batch if m.id == memory_id), None)
                if not old_memory:
                    continue

                # EMA smoothing: new = α * llm + (1-α) * old
                old_importance = old_memory.importance
                # Read before the UPDATE below: it expires the row's computed
                # resource columns, which an async session cannot load lazily.
                resource_row = owns_resource_point(old_memory)
                smoothed = alpha * new_score + (1 - alpha) * old_importance
                smoothed = max(0.0, min(1.0, smoothed))  # Clamp to [0, 1]

                # Update PostgreSQL. #1523: the candidate fetch excluded pinned
                # rows, but the LLM batch runs in between — a row pinned since
                # keeps the importance its owner set (load_pinned() orders by
                # it), and a forgotten row is left alone. 0 rows → skipped:
                # no Qdrant patch, no action row, not counted.
                written = await self.db.execute(
                    update(Memory)
                    .where(
                        Memory.id == memory_id,
                        Memory.deleted_at.is_(None),
                        not_pinned_predicate(),
                        # Tool guardrails: load_guardrails orders by the
                        # importance the author set, so it is never rescored.
                        not_tool_triggered_predicate(),
                    )
                    .values(importance=smoothed, updated_at=utcnow())
                )
                if cast(CursorResult[Any], written).rowcount == 0:
                    logger.info("importance_reeval_skipped_since_fetch", memory_id=str(memory_id))
                    continue

                # Update Qdrant payload. #2065: not for a resource-indexer row.
                # Its point is the indexer's (``Memory.point_id``), whose
                # payload carries no importance — and the reindex below
                # rebuilds it whole from the row. Its id names no point of its
                # own (or a stray label one, #2053), so a write there was lost.
                try:
                    if not resource_row:
                        await update_memory_payload_in_qdrant(
                            memory_id=memory_id,
                            payload_updates={"importance": smoothed},
                            collection_name=self.collection_name or "kagura_memories",
                            cascade_chunks=await self._chunk_cascade(context_id),
                        )
                except Exception as e:
                    logger.warning(
                        "importance_qdrant_update_failed",
                        memory_id=str(memory_id),
                        error=str(e),
                    )

                result.changed_memory_ids.add(memory_id)
                updated_count += 1
                if reporter and report_id:
                    await reporter.add_action(
                        report_id=report_id,
                        phase="importance_reeval",
                        action_type="update_importance",
                        memory_id=memory_id,
                        details={
                            "old_importance": old_importance,
                            "new_importance": smoothed,
                            "llm_score": new_score,
                            "alpha": alpha,
                        },
                    )

        result.memories_processed = updated_count
        result.llm_calls_used = budget.llm_calls_used - llm_calls_before
        self.record_spend(result)  # tokens, #1183 failures, #471 breakdown
        result.details = {
            "candidates": len(candidates),
            "updated": updated_count,
            "alpha": alpha,
            "llm_call_failures": self._llm_failures,  # #1183
        }

        logger.info(
            "importance_reeval_completed",
            candidates=len(candidates),
            updated=updated_count,
        )

        return result

    async def _fetch_candidates(
        self,
        user_id: str,
        workspace_id: str | None,
        context_id: str | None,
    ) -> list[Memory]:
        """Fetch memories with stale, mid-range importance."""
        cutoff = utcnow() - timedelta(days=STALENESS_DAYS)

        stmt = select(Memory).where(
            Memory.user_id == user_id,
            Memory.deleted_at.is_(None),
            Memory.importance >= IMPORTANCE_MIN,
            Memory.importance <= IMPORTANCE_MAX,
            # #1868: ``updated_at`` is NULL until a memory is edited (#1852
            # moved the embedding stamp off it), and ``NULL < cutoff`` is never
            # true — a never-edited row ages from its ``created_at``.
            func.coalesce(Memory.updated_at, Memory.created_at) < cutoff,
            # #1523: pinned rows keep the importance their owner set; the
            # deterministic load_pinned() lane orders by it.
            not_pinned_predicate(),
            # Tool guardrails: same rule for the load_guardrails lane.
            not_tool_triggered_predicate(),
        )
        if workspace_id:
            stmt = stmt.where(Memory.workspace_id == UUID(workspace_id))
        if context_id:
            stmt = stmt.where(Memory.context_id == UUID(context_id))

        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def _evaluate_batch(
        self,
        batch: list[Memory],
        user_id: str,
        context_id: str | None,
        workspace_id: str | None,
        budget: SleepBudget,
        config: NeuralMemoryConfig,
    ) -> dict[UUID, float]:
        """Evaluate a batch of memories via LLM. Returns {memory_id: score}."""
        # Assign short labels and shuffle for positional bias mitigation
        labels = list(string.ascii_uppercase[: len(batch)])
        label_to_id = dict(zip(labels, [m.id for m in batch], strict=True))

        items = list(zip(labels, batch, strict=True))
        random.shuffle(items)

        # The summary is untrusted (issue #919) — wrap it so an embedded
        # instruction cannot steer the importance re-evaluation.
        memory_lines = []
        for label, mem in items:
            memory_lines.append(
                f"[{label}] type={mem.type}, importance={mem.importance:.2f}, "
                f"access_count={mem.access_count}, scope={mem.scope}\n"
                f"    summary:\n{wrap_untrusted_content(mem.summary)}"
            )

        prompt = IMPORTANCE_REEVAL_USER.format(memories="\n".join(memory_lines))

        try:
            llm_resp = await self.llm_service.complete_json(
                user_id=user_id,
                prompt=prompt,
                system_prompt=IMPORTANCE_REEVAL_SYSTEM,
                context_id=context_id,
                workspace_id=workspace_id,
                model=config.sleep_llm_model,
                provider=config.sleep_llm_provider,
                platform_only=judge_platform_only(config),  # #1569
            )
            budget.consume(llm_calls=1)
            self._tokens_used += llm_resp.total_tokens
            self._llm_breakdown = accumulate_llm_response(self._llm_breakdown, llm_resp)
        except Exception as e:
            self._llm_failures += 1  # #1183
            logger.warning("importance_reeval_llm_failed", error=str(e))
            if (usage := billed_usage(e)) is not None:
                # #2038: the failed call still spent these tokens — count them in the
                # run's totals and budget, but not as a successful call.
                budget.consume(llm_calls=1)
                self._tokens_used += usage.total_tokens
                self._llm_breakdown = accumulate_llm_response(
                    self._llm_breakdown, usage, count_call=False
                )
            return {}

        # Parse with label validation
        scores: dict[UUID, float] = {}
        for item in llm_resp.parsed.get("scores", []):
            label = item.get("label")
            importance = item.get("importance")
            if label not in label_to_id:
                continue
            if not isinstance(importance, (int, float)) or not (0.0 <= importance <= 1.0):
                continue
            scores[label_to_id[label]] = float(importance)

        return scores
