"""Sleep Maintenance Phase 1: Edge Discovery.

Issue #103: Discover missing edges between related memories using
medium-similarity Qdrant search and optional LLM judgment.

Algorithm:
1. Sample N memories (recency-weighted: newer memories have fewer edges)
2. For each, find medium-similarity neighbors (0.6-0.9) via Qdrant
3. Filter out pairs that already have edges
4. LLM batch judgment: related/not, edge_type, confidence
5. Create confirmed edges via NeuralEdgeRepository

Academic notes:
- Recency-weighted sampling improves convergence over uniform random.
  New memories are edge-poor and benefit most from discovery.
  With sample_size=30 and corpus=500, ~95% coverage in ~30 runs.
- Positional bias mitigated by shuffling batch order.
"""

from __future__ import annotations

import math
import random
import string
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from neural.config import NeuralMemoryConfig
    from services.sleep.reporter import SleepReporter

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.qdrant import search_memories_qdrant
from models.memory import Memory, NeuralMemoryEdge
from repositories.neural_edge import NeuralEdgeRepository
from services.embedding_service import EmbeddingService
from services.llm_service import LLMService
from services.sleep.prompts import (
    EDGE_DISCOVERY_PROMPT_REVISION,
    EDGE_DISCOVERY_SYSTEM,
    EDGE_DISCOVERY_USER,
)
from services.sleep.reporter import PhaseResult, SleepBudget
from utils.logger import get_logger

logger = get_logger(__name__)

# Similarity range for edge discovery candidates
# Too high (>0.9) = likely duplicates (handled by dedup phase)
# Too low (<0.6) = likely unrelated
SIMILARITY_MIN = 0.6
SIMILARITY_MAX = 0.9

# Default initial edge weight for discovered edges
DISCOVERY_EDGE_WEIGHT = 0.5

# Max pairs per LLM batch call
BATCH_SIZE = 5

# Issue #248: k-NN cold-start seeding (#224/#238) births every new memory
# with weak `semantic_similarity` edges to its 0.4-0.9 Qdrant neighbors at
# `knn_seed_weight` (default 0.3, "intentionally low — synthetic signal").
# Without an edge_type-aware filter, `_filter_existing_edges` below would
# treat those seeded pairs as "already connected" and skip LLM judgment,
# producing 0 edges per run in production. 0.5 sits comfortably above the
# default seed weight so the LLM judge sees the mid-similarity band again;
# operators who configure `knn_seed_weight` >= 0.5 are implicitly opting
# those edges back into "real connection" semantics.
SEMANTIC_SIMILARITY_SYNTHETIC_WEIGHT_THRESHOLD = 0.5


def _is_synthetic_seed_edge(edge: NeuralMemoryEdge) -> bool:
    """Return True if ``edge`` is a low-weight k-NN cold-start seed (#248).

    Cold-start seeds from #224/#238 must NOT block Sleep Edge Discovery from
    re-judging a pair. All other edge types — and high-weight
    ``semantic_similarity`` edges — represent real connections.
    """
    return (
        edge.edge_type == "semantic_similarity"
        and edge.weight < SEMANTIC_SIMILARITY_SYNTHETIC_WEIGHT_THRESHOLD
    )


# Issue #306: Confidence histogram bucket boundaries.
# Convention: right-open [a, b) for the first three buckets, last bucket [0.85, 1.0]
# (inclusive at 1.0). Boundary values 0.5/0.7/0.85 fall into the higher bucket.
# The 0.0-0.5 bucket exists because LLM returns can clamp accepted-edge confidence
# anywhere in [0, 1] — pilot #249 demonstrated bimodal distribution detection
# requires the lower-band bucket.
# Annotated as `tuple[str, ...]` (not the inferred Literal tuple) so that
# `dict.fromkeys(KEYS, 0)` widens to `dict[str, int]` for downstream consumers.
CONFIDENCE_HISTOGRAM_KEYS: tuple[str, ...] = ("0.0-0.5", "0.5-0.7", "0.7-0.85", "0.85-1.0")


@dataclass
class BatchStats:
    """Per-batch LLM Judge stats from `_llm_judge_batch` (#306).

    Aggregated across batches in `execute()` before being unpacked into
    `PhaseResult.details`. Per-batch invariant: `failures in {0, 1}` because
    each batch makes exactly one `complete_json` call.

    `confidences` holds raw confidence values for accepted edges only —
    rejected-side confidence is not retained by the parser.
    `edge_type_counts` covers accepted edges only.
    """

    accepted: int = 0
    rejected: int = 0
    failures: int = 0
    edge_type_counts: dict[str, int] = field(default_factory=dict)
    confidences: list[float] = field(default_factory=list)


def _metrics_from_agg(
    agg: BatchStats,
    auto_accepted: int,
    avg_confidence: float,
    confidence_histogram: dict[str, int],
    config: NeuralMemoryConfig,
) -> dict[str, object]:
    """Build the #306 metric dict from aggregated values.

    Single source of truth for the 9 metric key names — `_empty_metrics()` and
    `execute()`'s success-path `result.details` both go through here, so a key
    rename or addition only requires editing this one function.
    """
    return {
        "llm_accepted": agg.accepted,
        "llm_rejected": agg.rejected,
        "llm_call_failures": agg.failures,
        "auto_accepted": auto_accepted,
        "edge_type_dist": agg.edge_type_counts,
        "avg_confidence": avg_confidence,
        "confidence_histogram": confidence_histogram,
        "llm_model": config.sleep_llm_model,
        "prompt_revision": EDGE_DISCOVERY_PROMPT_REVISION,
    }


def _empty_metrics(config: NeuralMemoryConfig) -> dict[str, object]:
    """Zero-initialized #306 metric keys for early-return paths.

    Reader code (`get_sleep_report` MCP tool, admin UI) can use `.get(key, 0)`
    safely, but emitting zero values keeps every sleep_report uniform.
    `llm_model` and `prompt_revision` always reflect the config that *would*
    be used if the LLM path ran, so historical reports stay comparable.
    """
    return _metrics_from_agg(
        agg=BatchStats(),
        auto_accepted=0,
        avg_confidence=0.0,
        confidence_histogram=dict.fromkeys(CONFIDENCE_HISTOGRAM_KEYS, 0),
        config=config,
    )


def _build_confidence_histogram(confidences: list[float]) -> dict[str, int]:
    """Bucket accepted-edge confidence values per CONFIDENCE_HISTOGRAM_KEYS."""
    histogram: dict[str, int] = dict.fromkeys(CONFIDENCE_HISTOGRAM_KEYS, 0)
    for c in confidences:
        if c < 0.5:
            histogram["0.0-0.5"] += 1
        elif c < 0.7:
            histogram["0.5-0.7"] += 1
        elif c < 0.85:
            histogram["0.7-0.85"] += 1
        else:
            histogram["0.85-1.0"] += 1
    return histogram


class EdgeDiscoveryPhase:
    """Discover missing edges between semantically related memories."""

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
        self.collection_name = collection_name
        self.embedding_service = EmbeddingService(db, model=embedding_model)
        # Reset on every execute(); init here so _llm_judge_batch can be called
        # in isolation (e.g. from unit tests) without AttributeError (#306).
        self._tokens_used = 0

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
        """Run edge discovery phase."""
        result = PhaseResult(phase_name="edge_discovery")
        llm_calls_before = budget.llm_calls_used
        self._tokens_used = 0

        if not config.sleep_edge_discovery_enabled:
            result.skipped = True
            result.skip_reason = "edge_discovery_disabled"
            return result

        sample_size = config.sleep_edge_discovery_sample_size
        llm_enabled = config.sleep_llm_provider != ""

        # Step 1: Sample memories (recency-weighted)
        sampled = await self._sample_memories(user_id, workspace_id, context_id, sample_size)
        if not sampled:
            result.details = {
                "message": "no_memories_to_sample",
                "sampled": 0,
                "candidates": 0,
                "filtered": 0,
                "edges_created": 0,
                **_empty_metrics(config),
            }
            return result

        # Step 2: Find medium-similarity candidates
        candidates = await self._find_candidates(sampled, user_id, workspace_id, context_id)
        if not candidates:
            result.details = {
                "message": "no_edge_candidates",
                "sampled": len(sampled),
                "candidates": 0,
                "filtered": 0,
                "edges_created": 0,
                **_empty_metrics(config),
            }
            return result

        # Step 3: Filter out existing edges
        filtered = await self._filter_existing_edges(candidates, user_id, workspace_id, context_id)
        if not filtered:
            result.details = {
                "message": "all_candidates_already_connected",
                "sampled": len(sampled),
                "candidates": len(candidates),
                "filtered": 0,
                "edges_created": 0,
                **_empty_metrics(config),
            }
            return result

        # Step 4: Judge candidates (LLM or auto-accept)
        edges_created = 0
        memory_map = {m.id: m for m in sampled}
        agg = BatchStats()
        auto_accepted = 0

        # Process in batches
        for batch_start in range(0, len(filtered), BATCH_SIZE):
            if not budget.can_afford(llm_calls=1 if llm_enabled else 0):
                break

            batch = filtered[batch_start : batch_start + BATCH_SIZE]

            if llm_enabled:
                confirmed, batch_stats = await self._llm_judge_batch(
                    batch,
                    memory_map,
                    user_id,
                    context_id,
                    workspace_id,
                    budget,
                    config,
                )
                agg.accepted += batch_stats.accepted
                agg.rejected += batch_stats.rejected
                agg.failures += batch_stats.failures
                for k, v in batch_stats.edge_type_counts.items():
                    agg.edge_type_counts[k] = agg.edge_type_counts.get(k, 0) + v
                agg.confidences.extend(batch_stats.confidences)
            else:
                # Without LLM, accept all candidates with default confidence.
                # Tracked separately as `auto_accepted` so it does NOT pollute
                # avg_confidence / confidence_histogram / edge_type_dist (#306).
                confirmed = [(src, dst, "related_to", 0.5) for src, dst, _score in batch]
                auto_accepted += len(confirmed)

            for src_id, dst_id, edge_type, confidence in confirmed:
                try:
                    await self.edge_repo.create_or_update_edge(
                        user_id=user_id,
                        src_id=src_id,
                        dst_id=dst_id,
                        edge_type=edge_type,
                        weight=DISCOVERY_EDGE_WEIGHT,
                        confidence=confidence,
                        workspace_id=workspace_id,
                        context_id=context_id,
                        edge_metadata={"source": "sleep_edge_discovery"},
                    )
                    edges_created += 1
                    if reporter and report_id:
                        await reporter.add_action(
                            report_id=report_id,
                            phase="edge_discovery",
                            action_type="create_edge",
                            memory_id=src_id,
                            target_id=dst_id,
                            details={
                                "edge_type": edge_type,
                                "confidence": confidence,
                                "weight": DISCOVERY_EDGE_WEIGHT,
                            },
                        )
                except Exception as e:
                    logger.warning(
                        "edge_creation_failed",
                        src=str(src_id),
                        dst=str(dst_id),
                        error=str(e),
                    )

        avg_confidence = sum(agg.confidences) / len(agg.confidences) if agg.confidences else 0.0
        confidence_histogram = _build_confidence_histogram(agg.confidences)

        result.memories_processed = len(sampled)
        result.llm_calls_used = budget.llm_calls_used - llm_calls_before
        result.tokens_used = self._tokens_used
        result.details = {
            "sampled": len(sampled),
            "candidates": len(candidates),
            "filtered": len(filtered),
            "edges_created": edges_created,
            **_metrics_from_agg(agg, auto_accepted, avg_confidence, confidence_histogram, config),
        }

        logger.info(
            "edge_discovery_phase_completed",
            sampled=len(sampled),
            candidates=len(candidates),
            edges_created=edges_created,
            llm_accepted=agg.accepted,
            llm_rejected=agg.rejected,
            llm_call_failures=agg.failures,
            auto_accepted=auto_accepted,
            edge_type_dist=agg.edge_type_counts,
            avg_confidence=avg_confidence,
            confidence_histogram=confidence_histogram,
            prompt_revision=EDGE_DISCOVERY_PROMPT_REVISION,
        )

        return result

    async def _sample_memories(
        self,
        user_id: str,
        workspace_id: str | None,
        context_id: str | None,
        sample_size: int,
    ) -> list[Memory]:
        """Sample memories via random sampling.

        Uses SQL RANDOM() for efficient uniform sampling.
        Recency-weighted sampling is a future improvement.
        """
        stmt = (
            select(Memory)
            .where(
                Memory.user_id == user_id,
                Memory.deleted_at.is_(None),
                Memory.embedding_status == "success",
            )
            .order_by(func.random())
            .limit(sample_size)
        )
        if workspace_id:
            stmt = stmt.where(Memory.workspace_id == UUID(workspace_id))
        if context_id:
            stmt = stmt.where(Memory.context_id == UUID(context_id))

        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def _find_candidates(
        self,
        memories: list[Memory],
        user_id: str,
        workspace_id: str | None,
        context_id: str | None,
    ) -> list[tuple[UUID, UUID, float]]:
        """Find medium-similarity pairs (0.6-0.9) via Qdrant search."""
        candidates: list[tuple[UUID, UUID, float]] = []
        seen: set[tuple[UUID, UUID]] = set()

        for memory in memories:
            try:
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
                    filters={"score_threshold": SIMILARITY_MIN},
                    collection_name=self.collection_name or "kagura_memories",
                )

                for hit in results:
                    score = hit["score"]
                    if score > SIMILARITY_MAX or score < SIMILARITY_MIN:
                        continue
                    hit_id = UUID(str(hit["id"]))
                    if hit_id == memory.id:
                        continue

                    a, b = sorted([memory.id, hit_id], key=str)
                    pair_key = (a, b)
                    if pair_key in seen:
                        continue
                    seen.add(pair_key)
                    candidates.append((memory.id, hit_id, score))

            except Exception as e:
                logger.warning(
                    "edge_discovery_search_failed",
                    memory_id=str(memory.id),
                    error=str(e),
                )

        return candidates

    async def _filter_existing_edges(
        self,
        candidates: list[tuple[UUID, UUID, float]],
        user_id: str,
        workspace_id: str | None,
        context_id: str | None,
    ) -> list[tuple[UUID, UUID, float]]:
        """Remove pairs that already have a meaningful edge.

        Issue #248: Low-weight ``semantic_similarity`` edges (weight <
        ``SEMANTIC_SIMILARITY_SYNTHETIC_WEIGHT_THRESHOLD``) are synthetic
        k-NN cold-start seeds from #224/#238 and do NOT block the pair from
        being re-judged by Sleep Edge Discovery. All other edge types — and
        high-weight ``semantic_similarity`` edges — are treated as real
        connections and cause the pair to be filtered out.
        """
        # TODO: N+1 query per candidate — batch fetch edges for all src_ids in one query
        filtered = []
        for src_id, dst_id, score in candidates:
            existing = await self.edge_repo.get_outgoing_edges(
                user_id=user_id,
                src_id=src_id,
                workspace_id=workspace_id,
                context_id=context_id,
            )
            connected_ids = {e.dst_id for e in existing if not _is_synthetic_seed_edge(e)}
            if dst_id not in connected_ids:
                filtered.append((src_id, dst_id, score))
        return filtered

    async def _llm_judge_batch(
        self,
        batch: list[tuple[UUID, UUID, float]],
        memory_map: dict[UUID, Memory],
        user_id: str,
        context_id: str | None,
        workspace_id: str | None,
        budget: SleepBudget,
        config: NeuralMemoryConfig,
    ) -> tuple[list[tuple[UUID, UUID, str, float]], BatchStats]:
        """Use LLM to judge edge candidates.

        Returns:
            Tuple of (confirmed_edges, stats):
              - confirmed_edges: list of (src_id, dst_id, edge_type, confidence)
                for accepted edges.
              - stats: BatchStats with per-batch counts. `failures` is 0 or 1
                (one LLM call per batch).
        """
        stats = BatchStats()

        # Collect all unique memories in batch, skip IDs not in memory_map
        all_ids: set[UUID] = set()
        for src, dst, _ in batch:
            if src in memory_map:
                all_ids.add(src)
            if dst in memory_map:
                all_ids.add(dst)

        if not all_ids:
            return [], stats

        # Assign short labels deterministically. `all_ids` is a set, so
        # `list(all_ids)` would have non-reproducible iteration order across
        # runs — sort by str(uuid) so the same batch always produces the same
        # label↔UUID mapping. Positional bias for the LLM is then handled by
        # `random.shuffle(shuffled_items)` below, which acts on the *display*
        # order independently of label assignment (#306, addresses Copilot
        # review #371 finding).
        id_list = sorted(all_ids, key=str)
        labels = list(string.ascii_uppercase[: len(id_list)])
        id_to_label = dict(zip(id_list, labels, strict=True))
        label_to_id = dict(zip(labels, id_list, strict=True))

        # Shuffle memories for positional bias mitigation
        shuffled_items = list(zip(id_list, labels, strict=True))
        random.shuffle(shuffled_items)

        memory_lines = []
        for mid, label in shuffled_items:
            mem = memory_map.get(mid)
            if mem:
                memory_lines.append(
                    f"[{label}] type={mem.type}, importance={mem.importance:.2f}\n"
                    f"    summary: {mem.summary[:300]}"
                )

        # Build pair_lines, skipping pairs where either end is not in
        # id_to_label. The all_ids collection above silently skips IDs not in
        # memory_map; mirroring that filter here prevents a KeyError when
        # `_find_candidates` returns a `dst` outside the sampled batch (the
        # normal case in production with sample_size=30 and corpus≥100).
        # Unguarded, this raised KeyError → caught by orchestrator try/except
        # → entire phase silently failed (closes #369). Skipped pairs are
        # NOT counted toward accepted/rejected/failures because they were
        # never judged by the LLM at all.
        #
        # Also build `requested_pairs` (orientation-agnostic) so the response
        # parser can reject hallucinated/unrequested pairs the LLM may invent
        # — the prompt only asks about specific pairs, but a misbehaving model
        # could return arbitrary (label_a, label_b) combinations and the
        # parser would otherwise create edges for them, inflating
        # `edges_created` and skewing observability metrics. Addresses
        # Copilot review #371 finding (loop 4).
        pair_lines = []
        requested_pairs: set[frozenset[str]] = set()
        for src, dst, score in batch:
            if src not in id_to_label or dst not in id_to_label:
                continue
            pair_lines.append(f"  ({id_to_label[src]}, {id_to_label[dst]}): similarity={score:.3f}")
            requested_pairs.add(frozenset({id_to_label[src], id_to_label[dst]}))

        if not pair_lines:
            # All pairs in this batch had at least one end outside memory_map.
            # Skip the LLM call entirely — there is nothing to ask.
            return [], stats

        prompt = EDGE_DISCOVERY_USER.format(
            memories="\n".join(memory_lines),
            pairs="\n".join(pair_lines),
        )

        # Consume the budget BEFORE the call so failed attempts are still
        # counted. Otherwise a failing LLM provider could trigger many more
        # batches than `max_llm_calls` permits — `execute()`'s budget check
        # would never see the consumption from the failure path. Tokens are
        # still only added on success (no tokens consumed when the call
        # failed). Addresses Copilot review #371 finding (loop 2).
        budget.consume(llm_calls=1)
        try:
            response, tokens = await self.llm_service.complete_json(
                user_id=user_id,
                prompt=prompt,
                system_prompt=EDGE_DISCOVERY_SYSTEM,
                context_id=context_id,
                workspace_id=workspace_id,
                model=config.sleep_llm_model,
                provider=config.sleep_llm_provider,
            )
            self._tokens_used += tokens

        except Exception as e:
            logger.warning("edge_discovery_llm_failed", error=str(e))
            stats.failures = 1
            return [], stats

        # Parse response with label validation
        confirmed: list[tuple[UUID, UUID, str, float]] = []
        valid_edge_types = {"related_to", "depends_on", "learned_from"}
        for edge in response.get("edges", []):
            pair = edge.get("pair", [])
            if len(pair) != 2 or pair[0] not in label_to_id or pair[1] not in label_to_id:
                # Malformed pair → not counted toward accepted/rejected.
                continue

            # Reject hallucinated/unrequested pairs: the LLM may invent pair
            # combinations that were never in `pair_lines`. Match
            # orientation-agnostic via frozenset to allow the model to flip
            # the pair order (the relationship is undirected at this stage).
            if frozenset(pair) not in requested_pairs:
                continue

            if not edge.get("related", False):
                stats.rejected += 1
                continue

            # Validate edge_type against DB CHECK constraint
            raw_type = edge.get("edge_type", "related_to")
            edge_type = raw_type if raw_type in valid_edge_types else "related_to"

            raw_conf = edge.get("confidence", 0.5)
            # Guard against non-finite values (NaN/Inf): a NaN here would
            # propagate to avg_confidence (also NaN, breaking JSON-strict
            # serialization) and silently land in the "0.85-1.0" histogram
            # bucket because all NaN comparisons return False (#306).
            if not isinstance(raw_conf, (int, float)) or not math.isfinite(raw_conf):
                raw_conf = 0.5
            confidence = max(0.0, min(1.0, float(raw_conf)))

            stats.accepted += 1
            stats.edge_type_counts[edge_type] = stats.edge_type_counts.get(edge_type, 0) + 1
            stats.confidences.append(confidence)

            confirmed.append(
                (
                    label_to_id[pair[0]],
                    label_to_id[pair[1]],
                    edge_type,
                    confidence,
                )
            )

        # Per-batch invariant: one LLM call → failures is 0 here. The except
        # branch above already returned `BatchStats(failures=1)` for the failure
        # case, so by construction `stats.failures == 0` at this point. The
        # invariant is documented on `BatchStats`; no runtime assert needed.
        return confirmed, stats
