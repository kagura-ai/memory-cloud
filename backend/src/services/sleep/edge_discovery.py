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
import statistics
import string
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, cast, get_args
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
    """Return True if ``edge`` is a synthetic cold-start seed (#248, #223).

    Cold-start seeds must NOT block Sleep Edge Discovery from re-judging a
    pair. Two seed types qualify:

    - **k-NN ``semantic_similarity`` (#221/#224/#238)**: synthetic only when
      ``weight < SEMANTIC_SIMILARITY_SYNTHETIC_WEIGHT_THRESHOLD`` (0.5).
      Operators who set ``knn_seed_weight`` >= 0.5 are implicitly opting
      those edges back into "real connection" semantics.
    - **``tag_cooccurrence`` (#223)**: synthetic at *any* weight — the type
      is purely seeding, weight 0.25–0.40 by spec, and there is no operator
      knob to "promote" tag-cooccurrence to a real connection. If a pair
      becomes truly related, Sleep Discovery should overwrite the
      tag_cooccurrence edge with a confirmed ``related_to`` (or similar).

    All other edge types are treated as real connections regardless of weight.
    """
    if edge.edge_type == "semantic_similarity":
        return edge.weight < SEMANTIC_SIMILARITY_SYNTHETIC_WEIGHT_THRESHOLD
    if edge.edge_type == "tag_cooccurrence":
        return True
    return False


# Issue #306: Confidence histogram bucket boundaries.
# Convention: right-open [a, b) for the first three buckets, last bucket [0.85, 1.0]
# (inclusive at 1.0). Boundary values 0.5/0.7/0.85 fall into the higher bucket.
# The 0.0-0.5 bucket exists because LLM returns can clamp accepted-edge confidence
# anywhere in [0, 1] — pilot #249 demonstrated bimodal distribution detection
# requires the lower-band bucket.
# Annotated as `tuple[str, ...]` (not the inferred Literal tuple) so that
# `dict.fromkeys(KEYS, 0)` widens to `dict[str, int]` for downstream consumers.
CONFIDENCE_HISTOGRAM_KEYS: tuple[str, ...] = ("0.0-0.5", "0.5-0.7", "0.7-0.85", "0.85-1.0")

# Issue #374: `EdgeType` is the type-level source of truth for valid
# `NeuralMemoryEdge.edge_type` values emitted by the LLM judge (a deliberate
# subset of the DB CHECK constraint in `models/memory.py` — the DB accepts
# additional non-LLM types like `tag_cooccurrence`). `VALID_EDGE_TYPES` is
# derived via `get_args` so the runtime membership check and the type
# annotation cannot drift. Adding a new LLM-emittable edge type only requires
# editing the Literal.
EdgeType = Literal["related_to", "depends_on", "learned_from"]
VALID_EDGE_TYPES: frozenset[EdgeType] = frozenset(get_args(EdgeType))

# Issue #373: edge_type directionality semantics.
# `related_to` is undirected (A related_to B ⇔ B related_to A); the parser
# accepts the LLM's pair order as-is. `depends_on` and `learned_from` are
# directed (A depends_on B ≠ B depends_on A); the parser MUST reject pairs
# where the LLM flipped the input order, otherwise edges are silently stored
# in the wrong direction (PR #371 PhD-pl review finding). Module-level so
# tests can import the same source of truth. Typed as `frozenset[EdgeType]`
# so adding a typo'd member is caught by pyright (#374).
DIRECTED_EDGE_TYPES: frozenset[EdgeType] = frozenset({"depends_on", "learned_from"})


@dataclass(frozen=True, slots=True)
class ConfirmedEdge:
    """A single edge accepted by `_llm_judge_batch` (#374).

    Replaces the prior positional 4-tuple `(src_id, dst_id, edge_type,
    confidence)`. `frozen=True` prevents post-construction mutation;
    `slots=True` shaves per-edge memory on the batch hot path. Named access
    eliminates src/dst swap bugs and is robust to future field additions.

    `confidence` is constrained to `[0.0, 1.0]` by the parser's clamp (and by
    the auto-accept path's literal `0.5`); Python's type system cannot express
    the range, so the invariant lives in the runtime clamp and this docstring.
    """

    src_id: UUID
    dst_id: UUID
    edge_type: EdgeType
    confidence: float


@dataclass
class BatchStats:
    """Per-batch LLM Judge stats from `_llm_judge_batch` (#306, #372).

    Aggregated across batches in `execute()` before being unpacked into
    `PhaseResult.details`. Per-batch invariant: `failures in {0, 1}` because
    each batch makes exactly one `complete_json` call.

    `confidences` / `rejected_confidences` hold raw confidence values for
    accepted / rejected edges respectively (#372). Retaining both sides
    enables decision-boundary analysis: comparing `P(confidence | accept)`
    against `P(confidence | reject)` exposes bimodality at the judgment
    boundary (pilot #249 evidence-light vs evidence-strict split), which
    the accept-only histogram alone could not reveal.

    `edge_type_counts` covers accepted edges only.

    `confidence_imputed` / `confidence_imputed_rejected` count parsed edges
    whose confidence field was replaced with the 0.5 default because the
    LLM returned a non-finite value (NaN, Inf) or a non-numeric type.
    Separated per-side (#372) so an operator can tell whether malformed
    confidence clusters on the accept path, the reject path, or both —
    the skew itself is a prompt/model signal independent of bimodality.
    """

    accepted: int = 0
    rejected: int = 0
    failures: int = 0
    confidence_imputed: int = 0
    confidence_imputed_rejected: int = 0
    edge_type_counts: dict[str, int] = field(default_factory=dict)
    confidences: list[float] = field(default_factory=list)
    rejected_confidences: list[float] = field(default_factory=list)


@dataclass(frozen=True)
class ConfidenceSummary:
    """5-number summary for edge-confidence distributions (#306, #372).

    Computed independently for accepted-edge and rejected-edge confidences;
    the shape is identical so the same dataclass serves both sides. `avg`
    (mean) is reported but is structurally inadequate for bimodal data
    (textbook example: bimodal mean = midpoint of valley). `median`, `p25`,
    `p75` give a robust picture of the distribution shape. Pair with the
    matching `confidence_histogram` / `confidence_histogram_rejected` for
    visual inspection. `n` lets readers gauge sample-size noise (per-run N
    is typically small ≤ 30 — formal bimodality detection requires
    cross-run aggregation, deferred to a separate follow-up).
    """

    avg: float
    median: float
    p25: float
    p75: float
    n: int


def _summarize_confidences(confidences: list[float]) -> ConfidenceSummary:
    """Compute mean + median + IQR + n from a list of confidences.

    Handles small-N edge cases: n=0 → all zeros (caller convention),
    n=1 → quartiles collapse to the single value (no IQR information).
    """
    n = len(confidences)
    if n == 0:
        return ConfidenceSummary(avg=0.0, median=0.0, p25=0.0, p75=0.0, n=0)
    avg = sum(confidences) / n
    median = statistics.median(confidences)
    if n >= 2:
        # Use inclusive quartiles so small samples (n=2, n=3) do not
        # extrapolate outside the observed confidence range. The default
        # `exclusive` (Tukey) method can return values < min(data) or
        # > max(data), which makes p25/p75 fall outside [0.0, 1.0] even
        # though the inputs are clamped — uninterpretable for a confidence
        # metric. statistics.quantiles(n=4) returns [Q1, Q2, Q3].
        quartiles = statistics.quantiles(confidences, n=4, method="inclusive")
        p25, p75 = quartiles[0], quartiles[2]
    else:
        # Single sample — IQR is undefined; collapse to the lone value.
        p25 = p75 = median
    return ConfidenceSummary(avg=avg, median=median, p25=p25, p75=p75, n=n)


def _metrics_from_agg(
    agg: BatchStats,
    auto_accepted: int,
    confidence_summary: ConfidenceSummary,
    confidence_histogram: dict[str, int],
    rejected_confidence_summary: ConfidenceSummary,
    rejected_confidence_histogram: dict[str, int],
    config: NeuralMemoryConfig,
) -> dict[str, object]:
    """Build the #306/#372 metric dict from aggregated values.

    Single source of truth for the metric key names — `_empty_metrics()` and
    `execute()`'s success-path `result.details` both go through here, so a key
    rename or addition only requires editing this one function.

    Accept-side and reject-side metrics (#372) share the same shape. Accept-
    side keys keep the bare `confidence` names (`avg_confidence`, etc.) for
    backward compatibility with pre-#372 sleep_reports and reader code;
    reject-side keys carry the `_rejected` suffix so alphabetical ordering
    places them adjacent to their accept-side counterparts in admin UIs and
    simplifies side-by-side comparison.

    Mutable fields (`edge_type_counts`, `confidence_histogram`,
    `confidence_histogram_rejected`) are **defensive-copied** so that the
    returned dict is decoupled from the caller's `agg` and any local working
    state. Without this, the result dict would alias the caller's mutable
    structures and any post-emit mutation would silently corrupt
    `result.details`. The copy cost is negligible (O(3) for edge_type,
    O(4) per histogram).

    Directional semantics (#373): `edge_type_dist` only counts edges that
    survived the parser's directional canonicalization in `_llm_judge_batch`.
    Undirected `related_to` matches the LLM response orientation-agnostically
    (frozenset). Directed `depends_on` / `learned_from` require the LLM to
    preserve the input pair order (`requested_ordered_pairs` ordered-tuple
    match); flipped directed pairs are silently dropped — they do NOT
    contribute to `llm_accepted`, `llm_rejected`, or `llm_call_failures`.
    `prompt_revision` distinguishes pre/post-#373 prompts so historical
    counts can be interpreted under the correct semantics.
    """
    return {
        "llm_accepted": agg.accepted,
        "llm_rejected": agg.rejected,
        "llm_call_failures": agg.failures,
        "auto_accepted": auto_accepted,
        "edge_type_dist": dict(agg.edge_type_counts),  # defensive copy
        "avg_confidence": confidence_summary.avg,
        "median_confidence": confidence_summary.median,
        "p25_confidence": confidence_summary.p25,
        "p75_confidence": confidence_summary.p75,
        "confidence_n": confidence_summary.n,
        "confidence_imputed": agg.confidence_imputed,
        "confidence_histogram": dict(confidence_histogram),  # defensive copy
        "avg_confidence_rejected": rejected_confidence_summary.avg,
        "median_confidence_rejected": rejected_confidence_summary.median,
        "p25_confidence_rejected": rejected_confidence_summary.p25,
        "p75_confidence_rejected": rejected_confidence_summary.p75,
        "confidence_n_rejected": rejected_confidence_summary.n,
        "confidence_imputed_rejected": agg.confidence_imputed_rejected,
        "confidence_histogram_rejected": dict(rejected_confidence_histogram),  # defensive copy
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
        confidence_summary=_summarize_confidences([]),
        confidence_histogram=dict.fromkeys(CONFIDENCE_HISTOGRAM_KEYS, 0),
        rejected_confidence_summary=_summarize_confidences([]),
        rejected_confidence_histogram=dict.fromkeys(CONFIDENCE_HISTOGRAM_KEYS, 0),
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
                agg.confidence_imputed += batch_stats.confidence_imputed
                agg.confidence_imputed_rejected += batch_stats.confidence_imputed_rejected
                for k, v in batch_stats.edge_type_counts.items():
                    agg.edge_type_counts[k] = agg.edge_type_counts.get(k, 0) + v
                agg.confidences.extend(batch_stats.confidences)
                agg.rejected_confidences.extend(batch_stats.rejected_confidences)
            else:
                # Without LLM, accept all candidates with default confidence.
                # Tracked separately as `auto_accepted` so it does NOT pollute
                # avg_confidence / confidence_histogram / edge_type_dist (#306).
                confirmed = [
                    ConfirmedEdge(src_id=src, dst_id=dst, edge_type="related_to", confidence=0.5)
                    for src, dst, _score in batch
                ]
                auto_accepted += len(confirmed)

            for edge in confirmed:
                try:
                    await self.edge_repo.create_or_update_edge(
                        user_id=user_id,
                        src_id=edge.src_id,
                        dst_id=edge.dst_id,
                        edge_type=edge.edge_type,
                        weight=DISCOVERY_EDGE_WEIGHT,
                        confidence=edge.confidence,
                        workspace_id=workspace_id,
                        context_id=context_id,
                        edge_metadata={"source": "sleep_edge_discovery"},
                        # Issue #457: automated writer; preserve declared_link.
                        protect_declared_link=True,
                        # Sleep edge_discovery discards the return; skip the
                        # post-upsert SELECT for the same reason as Hebbian.
                        return_fresh_edge=False,
                    )
                    edges_created += 1
                    if reporter and report_id:
                        await reporter.add_action(
                            report_id=report_id,
                            phase="edge_discovery",
                            action_type="create_edge",
                            memory_id=edge.src_id,
                            target_id=edge.dst_id,
                            details={
                                "edge_type": edge.edge_type,
                                "confidence": edge.confidence,
                                "weight": DISCOVERY_EDGE_WEIGHT,
                            },
                        )
                except Exception as e:
                    logger.warning(
                        "edge_creation_failed",
                        src=str(edge.src_id),
                        dst=str(edge.dst_id),
                        error=str(e),
                    )

        confidence_summary = _summarize_confidences(agg.confidences)
        confidence_histogram = _build_confidence_histogram(agg.confidences)
        rejected_confidence_summary = _summarize_confidences(agg.rejected_confidences)
        rejected_confidence_histogram = _build_confidence_histogram(agg.rejected_confidences)

        result.memories_processed = len(sampled)
        result.llm_calls_used = budget.llm_calls_used - llm_calls_before
        result.tokens_used = self._tokens_used
        result.details = {
            "sampled": len(sampled),
            "candidates": len(candidates),
            "filtered": len(filtered),
            "edges_created": edges_created,
            **_metrics_from_agg(
                agg,
                auto_accepted,
                confidence_summary,
                confidence_histogram,
                rejected_confidence_summary,
                rejected_confidence_histogram,
                config,
            ),
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
            avg_confidence=confidence_summary.avg,
            median_confidence=confidence_summary.median,
            confidence_n=confidence_summary.n,
            confidence_imputed=agg.confidence_imputed,
            confidence_histogram=confidence_histogram,
            avg_confidence_rejected=rejected_confidence_summary.avg,
            median_confidence_rejected=rejected_confidence_summary.median,
            confidence_n_rejected=rejected_confidence_summary.n,
            confidence_imputed_rejected=agg.confidence_imputed_rejected,
            confidence_histogram_rejected=rejected_confidence_histogram,
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
    ) -> tuple[list[ConfirmedEdge], BatchStats]:
        """Use LLM to judge edge candidates.

        Returns:
            Tuple of (confirmed_edges, stats):
              - confirmed_edges: list of `ConfirmedEdge` for accepted edges.
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
        #
        # `requested_ordered_pairs` (#373): same pairs as `requested_pairs` but
        # preserves the (src_label, dst_label) input order. The parser uses
        # this for directed edge_types (`depends_on`, `learned_from`) so that
        # an LLM-flipped pair is detected and silently dropped, preventing the
        # edge from being stored in the wrong direction. Undirected
        # `related_to` continues to use `requested_pairs` (orientation-agnostic).
        pair_lines = []
        requested_pairs: set[frozenset[str]] = set()
        requested_ordered_pairs: set[tuple[str, str]] = set()
        for src, dst, score in batch:
            if src not in id_to_label or dst not in id_to_label:
                continue
            src_label, dst_label = id_to_label[src], id_to_label[dst]
            pair_lines.append(f"  ({src_label}, {dst_label}): similarity={score:.3f}")
            requested_pairs.add(frozenset({src_label, dst_label}))
            requested_ordered_pairs.add((src_label, dst_label))

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
        confirmed: list[ConfirmedEdge] = []
        for edge in response.get("edges", []):
            pair = edge.get("pair", [])
            if len(pair) != 2 or pair[0] not in label_to_id or pair[1] not in label_to_id:
                # Malformed pair → not counted toward accepted/rejected.
                continue

            # Reject hallucinated/unrequested pairs: the LLM may invent pair
            # combinations that were never in `pair_lines`. Match
            # orientation-agnostic via frozenset; directional canonicalization
            # is applied below per `edge_type`.
            if frozenset(pair) not in requested_pairs:
                continue

            # Validate edge_type against DB CHECK constraint. Hoisted above
            # the `related` check so the directional dispatch can run before
            # we accept or reject — a flipped directed pair is malformed
            # regardless of `related=true|false`.
            #
            # `isinstance(raw_type, str)` guard: LLM JSON could return a
            # list/dict for `edge_type` (malformed schema). A non-string in
            # `set.__contains__` raises `TypeError`, which would abort
            # parsing of the entire successful LLM response — a single bad
            # row would silently lose every other valid edge in the batch.
            # Treat non-strings as invalid and coerce to `related_to`,
            # matching the existing fallback for unknown string values.
            raw_type = edge.get("edge_type", "related_to")
            # `frozenset.__contains__` does not narrow `raw_type` to `EdgeType`
            # for type checkers, so the membership check is promoted to a
            # `cast` — the `in VALID_EDGE_TYPES` predicate is the runtime
            # guarantee that the cast is sound.
            edge_type: EdgeType = (
                cast(EdgeType, raw_type)
                if isinstance(raw_type, str) and raw_type in VALID_EDGE_TYPES
                else "related_to"
            )

            # #373: directed edge_types must preserve input pair order. The
            # frozenset hallucination guard above is orientation-agnostic, so
            # an LLM that flipped (A, B) → (B, A) for `depends_on` / `learned_from`
            # would otherwise be accepted and the edge stored as B→A. Silently
            # skip flipped directed pairs — same treatment as hallucinated /
            # malformed pairs (no contribution to accepted/rejected/failures).
            # Undirected `related_to` is unaffected; coerced edge_types fall
            # through to the undirected path because `related_to` is the
            # coercion target.
            if (
                edge_type in DIRECTED_EDGE_TYPES
                and (pair[0], pair[1]) not in requested_ordered_pairs
            ):
                continue

            if not edge.get("related", False):
                raw_conf = edge.get("confidence", 0.5)
                if not isinstance(raw_conf, (int, float)) or not math.isfinite(raw_conf):
                    raw_conf = 0.5
                    stats.confidence_imputed_rejected += 1
                rejected_confidence = max(0.0, min(1.0, float(raw_conf)))
                stats.rejected += 1
                stats.rejected_confidences.append(rejected_confidence)
                continue

            raw_conf = edge.get("confidence", 0.5)
            # Guard against non-finite values (NaN/Inf): a NaN here would
            # propagate to avg_confidence (also NaN, breaking JSON-strict
            # serialization) and silently land in the "0.85-1.0" histogram
            # bucket because all NaN comparisons return False (#306).
            # Track the imputation count so operators can detect prompt/model
            # issues from production observability (would otherwise be silent).
            if not isinstance(raw_conf, (int, float)) or not math.isfinite(raw_conf):
                raw_conf = 0.5
                stats.confidence_imputed += 1
            confidence = max(0.0, min(1.0, float(raw_conf)))

            stats.accepted += 1
            stats.edge_type_counts[edge_type] = stats.edge_type_counts.get(edge_type, 0) + 1
            stats.confidences.append(confidence)

            confirmed.append(
                ConfirmedEdge(
                    src_id=label_to_id[pair[0]],
                    dst_id=label_to_id[pair[1]],
                    edge_type=edge_type,
                    confidence=confidence,
                )
            )

        # Per-batch invariant: one LLM call → failures is 0 here. The except
        # branch above already returned `BatchStats(failures=1)` for the failure
        # case, so by construction `stats.failures == 0` at this point. The
        # invariant is documented on `BatchStats`; no runtime assert needed.
        return confirmed, stats
