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
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from neural.config import NeuralMemoryConfig
    from services.sleep.reporter import SleepReporter

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from db.qdrant import update_memory_payload_in_qdrant
from models.memory import Memory
from services.llm_service import LLMService
from services.sleep.prompts import IMPORTANCE_REEVAL_SYSTEM, IMPORTANCE_REEVAL_USER
from services.sleep.reporter import PhaseResult, SleepBudget
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


class ImportanceReevalPhase:
    """Re-evaluate memory importance using LLM + EMA smoothing."""

    def __init__(
        self, db: AsyncSession, llm_service: LLMService, collection_name: str | None = None
    ):
        self.db = db
        self.llm_service = llm_service
        self.collection_name = collection_name

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
        result = PhaseResult(phase_name="importance_reeval")
        llm_calls_before = budget.llm_calls_used
        self._tokens_used = 0

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
                smoothed = alpha * new_score + (1 - alpha) * old_importance
                smoothed = max(0.0, min(1.0, smoothed))  # Clamp to [0, 1]

                # Update PostgreSQL
                await self.db.execute(
                    update(Memory)
                    .where(Memory.id == memory_id)
                    .values(importance=smoothed, updated_at=utcnow())
                )

                # Update Qdrant payload
                try:
                    await update_memory_payload_in_qdrant(
                        memory_id=memory_id,
                        payload_updates={"importance": smoothed},
                        collection_name=self.collection_name or "kagura_memories",
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
        result.tokens_used = self._tokens_used
        result.details = {
            "candidates": len(candidates),
            "updated": updated_count,
            "alpha": alpha,
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
            Memory.updated_at < cutoff,
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

        memory_lines = []
        for label, mem in items:
            memory_lines.append(
                f"[{label}] type={mem.type}, importance={mem.importance:.2f}, "
                f"access_count={mem.access_count}, scope={mem.scope}\n"
                f"    summary: {mem.summary[:300]}"
            )

        prompt = IMPORTANCE_REEVAL_USER.format(memories="\n".join(memory_lines))

        try:
            response, tokens = await self.llm_service.complete_json(
                user_id=user_id,
                prompt=prompt,
                system_prompt=IMPORTANCE_REEVAL_SYSTEM,
                context_id=context_id,
                workspace_id=workspace_id,
                model=config.sleep_llm_model,
                provider=config.sleep_llm_provider,
            )
            budget.consume(llm_calls=1)
            self._tokens_used += tokens
        except Exception as e:
            logger.warning("importance_reeval_llm_failed", error=str(e))
            return {}

        # Parse with label validation
        scores: dict[UUID, float] = {}
        for item in response.get("scores", []):
            label = item.get("label")
            importance = item.get("importance")
            if label not in label_to_id:
                continue
            if not isinstance(importance, (int, float)) or not (0.0 <= importance <= 1.0):
                continue
            scores[label_to_id[label]] = float(importance)

        return scores
