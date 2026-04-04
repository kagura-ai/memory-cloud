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
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from db.qdrant import delete_memory_from_qdrant
from models.memory import Memory
from repositories.memory import MemoryRepository
from services.graph_service import GraphService
from services.llm_service import LLMService
from services.sleep.prompts import CONSOLIDATION_JUDGE_SYSTEM, CONSOLIDATION_JUDGE_USER
from services.sleep.reporter import PhaseResult, SleepBudget
from utils.datetime import utcnow
from utils.logger import get_logger

logger = get_logger(__name__)

# Batch size for LLM consolidation judgment
BATCH_SIZE = 5


class ConsolidationPhase:
    """Consolidate working memories with optional LLM judgment."""

    def __init__(self, db: AsyncSession, llm_service: LLMService):
        self.db = db
        self.llm_service = llm_service
        self.memory_repo = MemoryRepository(db)

    async def execute(
        self,
        config,
        user_id: str,
        workspace_id: str | None,
        context_id: str | None,
        budget: SleepBudget,
    ) -> PhaseResult:
        """Run consolidation phase."""
        result = PhaseResult(phase_name="consolidation")
        llm_calls_before = budget.llm_calls_used

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

        promoted = 0
        deleted = 0
        borderline: list[Memory] = []

        for memory in working:
            age_days = (utcnow() - memory.created_at).days

            # Get neural metrics if graph exists
            neural_metrics = None
            if has_graph:
                neural_metrics = graph_service.get_node_metrics(str(memory.id))

            # === Fast path: rule-based (identical to legacy consolidation_task) ===
            should_promote = (
                (memory.access_count >= 3 and memory.importance >= 0.5)
                or (memory.access_count >= 5)
                or (memory.importance >= 0.8 and age_days >= 3)
                or (age_days >= 30 and memory.access_count >= 1)
                or (neural_metrics and neural_metrics["centrality"] >= centrality_threshold)
                or (neural_metrics and neural_metrics["edge_count"] >= hub_threshold)
                or (neural_metrics and neural_metrics["avg_edge_weight"] >= weight_threshold)
            )

            should_delete = (
                age_days >= 30
                and memory.access_count == 0
                and (not neural_metrics or neural_metrics["is_isolated"])
            )

            if should_promote:
                await self.memory_repo.promote_to_persistent(memory.id)
                promoted += 1
                result.changed_memory_ids.add(memory.id)
            elif should_delete:
                try:
                    await delete_memory_from_qdrant(user_id, memory.id)
                    await self.memory_repo.delete(memory.id)
                    deleted += 1
                except Exception as e:
                    logger.warning(
                        "consolidation_delete_failed",
                        memory_id=str(memory.id),
                        error=str(e),
                    )
            else:
                # Borderline: candidate for LLM judgment
                borderline.append(memory)

        # === LLM path for borderline cases ===
        llm_promoted = 0
        llm_archived = 0
        llm_enabled = config.sleep_llm_provider != ""

        if borderline and llm_enabled:
            for batch_start in range(0, len(borderline), BATCH_SIZE):
                if not budget.can_afford(llm_calls=1):
                    break

                batch = borderline[batch_start : batch_start + BATCH_SIZE]
                decisions = await self._llm_judge_batch(
                    batch, user_id, context_id, workspace_id, budget, config
                )

                for memory_id, action in decisions.items():
                    if action == "promote":
                        await self.memory_repo.promote_to_persistent(memory_id)
                        llm_promoted += 1
                        result.changed_memory_ids.add(memory_id)
                    elif action == "archive":
                        mem = next((m for m in batch if m.id == memory_id), None)
                        if mem:
                            # Only archive truly isolated memories
                            neural = None
                            if has_graph:
                                neural = graph_service.get_node_metrics(str(memory_id))
                            if not neural or neural["is_isolated"]:
                                await delete_memory_from_qdrant(user_id, memory_id)
                                await self.memory_repo.delete(memory_id)
                                llm_archived += 1

        result.memories_processed = len(working)
        result.llm_calls_used = budget.llm_calls_used - llm_calls_before
        result.details = {
            "working_count": len(working),
            "rule_promoted": promoted,
            "rule_deleted": deleted,
            "borderline": len(borderline),
            "llm_promoted": llm_promoted,
            "llm_archived": llm_archived,
        }

        logger.info(
            "consolidation_completed",
            working=len(working),
            promoted=promoted + llm_promoted,
            deleted=deleted + llm_archived,
        )

        return result

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
        config,
    ) -> dict[UUID, str]:
        """Use LLM to judge borderline working memories.

        Returns {memory_id: "promote"|"keep"|"archive"}.
        """
        labels = list(string.ascii_uppercase[: len(batch)])
        label_to_id = dict(zip(labels, [m.id for m in batch], strict=True))

        items = list(zip(labels, batch, strict=True))
        random.shuffle(items)

        memory_lines = []
        for label, mem in items:
            age_days = (utcnow() - mem.created_at).days
            memory_lines.append(
                f"[{label}] type={mem.type}, importance={mem.importance:.2f}, "
                f"access_count={mem.access_count}, age_days={age_days}, scope={mem.scope}\n"
                f"    summary: {mem.summary[:300]}"
            )

        prompt = CONSOLIDATION_JUDGE_USER.format(memories="\n".join(memory_lines))

        try:
            response, _tokens = await self.llm_service.complete_json(
                user_id=user_id,
                prompt=prompt,
                system_prompt=CONSOLIDATION_JUDGE_SYSTEM,
                context_id=context_id,
                workspace_id=workspace_id,
                model=config.sleep_llm_model,
                provider=config.sleep_llm_provider,
            )
            budget.consume(llm_calls=1)
        except Exception as e:
            logger.warning("consolidation_llm_failed", error=str(e))
            return {}

        # Parse with label validation
        decisions: dict[UUID, str] = {}
        for item in response.get("decisions", []):
            label = item.get("label")
            action = item.get("action")
            if label not in label_to_id:
                continue
            if action not in ("promote", "keep", "archive"):
                continue
            decisions[label_to_id[label]] = action

        return decisions
