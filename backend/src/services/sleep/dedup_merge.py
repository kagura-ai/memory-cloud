"""Sleep Maintenance Phase 2: Dedup/Merge.

Issue #101: Detect and merge duplicate memories using cosine similarity
clustering and optional LLM judgment.

Algorithm:
1. For each memory, find high-similarity neighbors in Qdrant (>= threshold)
2. Build candidate pairs, cluster with Union-Find (cap cluster size at 5)
3. LLM on: batch judgment (merge/keep_both)
4. LLM off: similarity >= 0.98 → auto-merge, else → flag
5. Merge: keep winner, soft-delete losers, transfer edges, merge tags

Academic notes:
- Union-Find transitivity: A~B + B~C clusters all three even if A~C is low.
  Cluster size cap (5) + LLM second gate mitigate runaway merges.
- Positional bias: batch order is shuffled before LLM calls.
- ID hallucination: short labels (A, B, C) used in prompts, mapped back to UUIDs.
"""

from __future__ import annotations

import random
import string
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from neural.config import NeuralMemoryConfig
    from services.sleep.reporter import SleepReporter

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from db.qdrant import delete_memory_from_qdrant, search_memories_qdrant
from models.memory import Memory
from repositories.neural_edge import NeuralEdgeRepository
from services.embedding_service import EmbeddingService
from services.llm_service import LLMService
from services.sleep.prompts import DEDUP_JUDGE_SYSTEM, DEDUP_JUDGE_USER
from services.sleep.reporter import (
    LLMCallBreakdown,
    PhaseResult,
    SleepBudget,
    accumulate_llm_response,
)
from utils.datetime import utcnow
from utils.logger import get_logger

logger = get_logger(__name__)

# Maximum cluster size to process in one run.
# Larger clusters are deferred to the next sleep cycle.
MAX_CLUSTER_SIZE = 5

# Similarity threshold for auto-merge without LLM (LLM-off mode)
AUTO_MERGE_THRESHOLD = 0.98


class UnionFind:
    """Union-Find (disjoint set) with path compression and union by rank.

    Used to cluster memories that are pairwise similar above threshold.
    """

    def __init__(self) -> None:
        self.parent: dict[UUID, UUID] = {}
        self.rank: dict[UUID, int] = {}

    def find(self, x: UUID) -> UUID:
        """Find root with path compression."""
        if x not in self.parent:
            self.parent[x] = x
            self.rank[x] = 0
        if self.parent[x] != x:
            self.parent[x] = self.find(self.parent[x])
        return self.parent[x]

    def union(self, x: UUID, y: UUID) -> None:
        """Union by rank."""
        rx, ry = self.find(x), self.find(y)
        if rx == ry:
            return
        if self.rank[rx] < self.rank[ry]:
            rx, ry = ry, rx
        self.parent[ry] = rx
        if self.rank[rx] == self.rank[ry]:
            self.rank[rx] += 1

    def clusters(self) -> list[set[UUID]]:
        """Return all clusters as list of sets."""
        groups: dict[UUID, set[UUID]] = {}
        for x in self.parent:
            root = self.find(x)
            groups.setdefault(root, set()).add(x)
        return list(groups.values())


class DedupMergePhase:
    """Detect and merge duplicate memories."""

    def __init__(
        self,
        db: AsyncSession,
        llm_service: LLMService,
        embedding_model: str | None = None,
        collection_name: str | None = None,
    ):
        self.db = db
        self.llm_service = llm_service
        self.edge_repo = NeuralEdgeRepository(db)
        self.embedding_service = EmbeddingService(db, model=embedding_model)
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
        """Run dedup/merge phase.

        Args:
            config: NeuralMemoryConfig with sleep params
            user_id: Target user
            workspace_id: Target workspace
            context_id: Target context
            budget: Shared budget tracker

        Returns:
            PhaseResult with merge statistics and changed memory IDs
        """
        result = PhaseResult(phase_name="dedup_merge")
        llm_calls_before = budget.llm_calls_used
        self._tokens_used = 0
        # #471: per-(provider, model) accumulator (lazy-init).
        self._llm_breakdown: LLMCallBreakdown | None = None

        if not config.sleep_dedup_enabled:
            result.skipped = True
            result.skip_reason = "dedup_disabled"
            return result

        llm_enabled = config.sleep_llm_provider != ""
        threshold = config.sleep_dedup_similarity_threshold

        # Step 1: Fetch active memories
        memories = await self._fetch_active_memories(
            user_id, workspace_id, context_id, config.sleep_max_memories_per_run
        )
        if len(memories) < 2:
            result.details = {"message": "not_enough_memories", "count": len(memories)}
            return result

        # Step 2: Find similar pairs via Qdrant
        pairs = await self._find_similar_pairs(
            memories, user_id, workspace_id, context_id, threshold
        )
        if not pairs:
            result.details = {"message": "no_duplicate_candidates"}
            return result

        # Step 3: Cluster with Union-Find
        uf = UnionFind()
        for id_a, id_b, _score in pairs:
            uf.union(id_a, id_b)

        clusters = uf.clusters()
        # Filter to clusters with 2+ members, cap at MAX_CLUSTER_SIZE
        processable = [c for c in clusters if 2 <= len(c) <= MAX_CLUSTER_SIZE]
        deferred = [c for c in clusters if len(c) > MAX_CLUSTER_SIZE]

        if deferred:
            logger.info(
                "dedup_clusters_deferred",
                count=len(deferred),
                sizes=[len(c) for c in deferred],
            )

        # Step 4: Process each cluster
        memory_map = {m.id: m for m in memories}
        pair_scores = {tuple(sorted([a, b], key=str)): s for a, b, s in pairs}
        merged_count = 0

        for cluster in processable:
            if not budget.can_afford(llm_calls=1 if llm_enabled else 0):
                break

            cluster_memories = [memory_map[mid] for mid in cluster if mid in memory_map]
            if len(cluster_memories) < 2:
                continue

            merge_decisions = await self._judge_cluster(
                cluster_memories,
                pair_scores,
                llm_enabled,
                user_id,
                context_id,
                workspace_id,
                budget,
                config,
            )

            for winner_id, loser_id in merge_decisions:
                winner = memory_map.get(winner_id)
                loser = memory_map.get(loser_id)
                await self._execute_merge(
                    winner,
                    loser,
                    user_id,
                    workspace_id,
                    context_id,
                )
                result.changed_memory_ids.add(winner_id)
                merged_count += 1
                if reporter and report_id and winner and loser:
                    pair_key = tuple(sorted([winner_id, loser_id], key=str))
                    await reporter.add_action(
                        report_id=report_id,
                        phase="dedup_merge",
                        action_type="merge",
                        memory_id=winner_id,
                        target_id=loser_id,
                        details={
                            "similarity": pair_scores.get(pair_key, 0.0),
                            "winner_tags": list(winner.tags or []),
                            "loser_tags": list(loser.tags or []),
                            "loser_summary": (loser.summary or "")[:200],
                        },
                    )

            result.memories_processed += len(cluster_memories)

        result.details = {
            "candidates": len(pairs),
            "clusters": len(processable),
            "deferred_clusters": len(deferred),
            "merged": merged_count,
        }

        result.llm_calls_used = budget.llm_calls_used - llm_calls_before
        result.tokens_used = self._tokens_used
        # #471: attach per-(provider, model) breakdown for child-row write.
        if self._llm_breakdown is not None:
            result.llm_breakdown = [self._llm_breakdown]

        logger.info(
            "dedup_merge_phase_completed",
            candidates=len(pairs),
            merged=merged_count,
            llm_calls=result.llm_calls_used,
        )

        return result

    async def _fetch_active_memories(
        self,
        user_id: str,
        workspace_id: str | None,
        context_id: str | None,
        limit: int = 500,
    ) -> list[Memory]:
        """Fetch active (non-deleted) memories, capped by limit."""
        stmt = (
            select(Memory)
            .where(
                Memory.user_id == user_id,
                Memory.deleted_at.is_(None),
            )
            .order_by(Memory.updated_at.desc())
            .limit(limit)
        )
        if workspace_id:
            stmt = stmt.where(Memory.workspace_id == UUID(workspace_id))
        if context_id:
            stmt = stmt.where(Memory.context_id == UUID(context_id))

        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def _find_similar_pairs(
        self,
        memories: list[Memory],
        user_id: str,
        workspace_id: str | None,
        context_id: str | None,
        threshold: float,
    ) -> list[tuple[UUID, UUID, float]]:
        """Find pairs of memories with cosine similarity >= threshold.

        For each memory, embed its summary and search Qdrant for
        high-similarity neighbors. Deduplicates pairs by sorted ID tuple.
        """
        pairs: list[tuple[UUID, UUID, float]] = []
        seen: set[tuple[UUID, UUID]] = set()
        memory_ids = {m.id for m in memories}

        for memory in memories:
            try:
                # Get embedding for this memory's summary
                vector = await self.embedding_service.embed(
                    memory.summary,
                    user_id=user_id,
                    context_id=context_id,
                    workspace_id=workspace_id,
                )

                results = await search_memories_qdrant(
                    user_id=user_id,
                    query_vector=vector,
                    workspace_id=workspace_id or "",
                    context_id=context_id or "",
                    limit=10,
                    filters={"score_threshold": threshold},
                    collection_name=self.collection_name or "kagura_memories",
                )
                for hit in results:
                    hit_id = UUID(str(hit["id"]))
                    if hit_id == memory.id:
                        continue
                    if hit_id not in memory_ids:
                        continue
                    # Canonical pair key (order-independent)
                    a, b = sorted([memory.id, hit_id], key=str)
                    pair_key = (a, b)
                    if pair_key in seen:
                        continue
                    seen.add(pair_key)
                    pairs.append((memory.id, hit_id, hit["score"]))
            except Exception as e:
                logger.warning(
                    "dedup_search_failed",
                    memory_id=str(memory.id),
                    error=str(e),
                )

        return pairs

    async def _judge_cluster(
        self,
        cluster_memories: list[Memory],
        pair_scores: dict[tuple[UUID, UUID], float],
        llm_enabled: bool,
        user_id: str,
        context_id: str | None,
        workspace_id: str | None,
        budget: SleepBudget,
        config: NeuralMemoryConfig,
    ) -> list[tuple[UUID, UUID]]:
        """Judge a cluster and return merge decisions as (winner_id, loser_id) pairs."""
        decisions: list[tuple[UUID, UUID]] = []

        if llm_enabled and budget.can_afford(llm_calls=1):
            decisions = await self._llm_judge(
                cluster_memories,
                pair_scores,
                user_id,
                context_id,
                workspace_id,
                budget,
                config,
            )
        else:
            # Rule-based fallback
            decisions = self._rule_based_judge(cluster_memories, pair_scores)

        return decisions

    async def _llm_judge(
        self,
        cluster_memories: list[Memory],
        pair_scores: dict[tuple[UUID, UUID], float],
        user_id: str,
        context_id: str | None,
        workspace_id: str | None,
        budget: SleepBudget,
        config: NeuralMemoryConfig,
    ) -> list[tuple[UUID, UUID]]:
        """Use LLM to judge duplicates in a cluster."""
        # Build label map (A, B, C...) — mitigates ID hallucination
        labels = list(string.ascii_uppercase[: len(cluster_memories)])
        label_to_id = {label: mem.id for label, mem in zip(labels, cluster_memories, strict=True)}
        id_to_label = {mem.id: label for label, mem in zip(labels, cluster_memories, strict=True)}

        # Shuffle to mitigate positional bias
        shuffled = list(zip(labels, cluster_memories, strict=True))
        random.shuffle(shuffled)
        labels_shuffled = [s[0] for s in shuffled]
        mems_shuffled = [s[1] for s in shuffled]

        # Format memories
        memory_lines = []
        for label, mem in zip(labels_shuffled, mems_shuffled, strict=True):
            memory_lines.append(
                f"[{label}] type={mem.type}, importance={mem.importance:.2f}\n"
                f"    summary: {mem.summary[:300]}"
            )

        # Format pairs
        pair_lines = []
        ids = [m.id for m in cluster_memories]
        for i, id_a in enumerate(ids):
            for id_b in ids[i + 1 :]:
                key = tuple(sorted([id_a, id_b], key=str))
                score = pair_scores.get(key, 0.0)
                pair_lines.append(
                    f"  ({id_to_label[id_a]}, {id_to_label[id_b]}): similarity={score:.3f}"
                )

        prompt = DEDUP_JUDGE_USER.format(
            memories="\n".join(memory_lines),
            pairs="\n".join(pair_lines),
        )

        try:
            llm_resp = await self.llm_service.complete_json(
                user_id=user_id,
                prompt=prompt,
                system_prompt=DEDUP_JUDGE_SYSTEM,
                context_id=context_id,
                workspace_id=workspace_id,
                model=config.sleep_llm_model,
                provider=config.sleep_llm_provider,
            )
            budget.consume(llm_calls=1)
            self._tokens_used += llm_resp.total_tokens
            self._llm_breakdown = accumulate_llm_response(self._llm_breakdown, llm_resp)

            return self._parse_dedup_response(llm_resp.parsed, label_to_id)

        except Exception as e:
            logger.warning("dedup_llm_judge_failed", error=str(e))
            return []

    def _parse_dedup_response(
        self,
        response: dict,
        label_to_id: dict[str, UUID],
    ) -> list[tuple[UUID, UUID]]:
        """Parse LLM dedup response, validating all labels."""
        decisions: list[tuple[UUID, UUID]] = []
        judgments = response.get("judgments", [])

        for j in judgments:
            if j.get("verdict") != "merge":
                continue

            winner_label = j.get("winner")
            pair = j.get("pair", [])

            if len(pair) != 2:
                continue

            # Validate labels exist
            if pair[0] not in label_to_id or pair[1] not in label_to_id:
                logger.warning(
                    "dedup_invalid_label",
                    pair=pair,
                    valid_labels=list(label_to_id.keys()),
                )
                continue

            if winner_label not in label_to_id:
                continue

            loser_label = pair[0] if pair[1] == winner_label else pair[1]
            decisions.append((label_to_id[winner_label], label_to_id[loser_label]))

        return decisions

    def _rule_based_judge(
        self,
        cluster_memories: list[Memory],
        pair_scores: dict[tuple[UUID, UUID], float],
    ) -> list[tuple[UUID, UUID]]:
        """Rule-based dedup: auto-merge only at very high similarity."""
        decisions: list[tuple[UUID, UUID]] = []
        ids = [m.id for m in cluster_memories]
        id_to_mem = {m.id: m for m in cluster_memories}

        for i, id_a in enumerate(ids):
            for id_b in ids[i + 1 :]:
                key = tuple(sorted([id_a, id_b], key=str))
                score = pair_scores.get(key, 0.0)
                if score >= AUTO_MERGE_THRESHOLD:
                    # Keep the one with higher importance or more content
                    mem_a = id_to_mem[id_a]
                    mem_b = id_to_mem[id_b]
                    if mem_a.importance >= mem_b.importance:
                        decisions.append((id_a, id_b))
                    else:
                        decisions.append((id_b, id_a))

        return decisions

    async def _execute_merge(
        self,
        winner: Memory | None,
        loser: Memory | None,
        user_id: str,
        workspace_id: str | None,
        context_id: str | None,
    ) -> None:
        """Execute a merge: soft-delete loser, transfer edges and tags to winner."""
        if not winner or not loser:
            return

        winner_tags = set(winner.tags or [])
        loser_tags = set(loser.tags or [])
        merged_tags = list(winner_tags | loser_tags)

        await self.db.execute(
            update(Memory)
            .where(Memory.id == winner.id)
            .values(tags=merged_tags, updated_at=utcnow())
        )

        # Soft-delete loser in PostgreSQL
        await self.db.execute(
            update(Memory)
            .where(Memory.id == loser.id)
            .values(
                deleted_at=utcnow(),
                deleted_by="sleep_maintenance",
            )
        )

        # Delete loser from Qdrant to prevent orphan vectors (cf. BUG FIX #83-10)
        try:
            await delete_memory_from_qdrant(
                user_id, loser.id, self.collection_name or "kagura_memories"
            )
        except Exception as e:
            logger.warning(
                "dedup_qdrant_delete_failed",
                loser_id=str(loser.id),
                error=str(e),
            )

        # Transfer edges from loser to winner
        await self.edge_repo.transfer_edges(
            from_node_id=loser.id,
            to_node_id=winner.id,
            user_id=user_id,
            workspace_id=workspace_id,
            context_id=context_id,
        )

        logger.info(
            "dedup_merge_executed",
            winner_id=str(winner.id),
            loser_id=str(loser.id),
            merged_tags=len(merged_tags),
        )
