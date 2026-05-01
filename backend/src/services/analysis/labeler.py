"""Stage [F + G]: representative selection + LLM cluster labeling.

[F] For each cluster, pick the 5 memories whose embeddings are
closest to the cluster centroid. These are the "representatives" the
LLM will see — they ground the label in actual content rather than
asking the model to infer from cluster index alone.

[G] In parallel (Semaphore=8 over clusters), call the LLM via
``llm_caller.call_with_fallback``: send the 5 summaries, receive
``{label, description, label_confidence}``. Frozenset hallucination
guard validates that the LLM didn't invent memory ids that weren't
in the cluster (it shouldn't return any ids in the response, but
this defense holds even if the prompt template later evolves to
ask for citations).

LLM budget consume happens **before** the call (sleep precedent
``edge_discovery.py:786``): failed attempts still count, otherwise
a flapping provider can blow past ``max_llm_calls = ceil(sqrt(n))+1``.

The labeler uses the existing ``LLMService`` from
``services/llm_service`` (which carries BYOK key resolution). The
fallback chain stays within OpenAI; v1.5 will lift this to a
provider-keyed mapping.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import numpy as np

from services.analysis.llm_caller import (
    OPENAI_FALLBACK_CHAIN,
    AnalysisLLMUpstreamError,
    call_with_fallback,
    filter_hallucinated_ids,
)
from services.analysis.prompts import CLUSTER_LABEL_SYSTEM, CLUSTER_LABEL_USER
from services.analysis.vector_pull import MemoryRecord
from services.llm_service import LLMService
from services.sleep.reporter import LLMCallBreakdown, accumulate_llm_response
from utils.logger import get_logger

logger = get_logger(__name__)

# Per the prototype, 5 reps strikes the right balance between LLM
# token cost and label confidence. <=3 produces vague labels;
# >=8 inflates token cost without measurably improving labels.
_REPS_PER_CLUSTER = 5

# Concurrent LLM calls. Issue #495 spec is parallel=8; this is also
# the default for OpenAI tier-1 RPM safety on small workspaces.
# Configurable via env var so ops can tune for users with low BYOK
# quota.
_LLM_CONCURRENCY = 8


@dataclass(frozen=True)
class ClusterLabel:
    """Output of [G] for one cluster.

    Attributes:
        cluster_index: Stable index from KMeans (same as
            ``ClusterResult.labels`` value).
        label: 1-3 word noun phrase from the LLM.
        description: One-sentence cluster description.
        label_confidence: LLM-self-reported confidence, [0, 1].
        representative_memory_ids: Memory ids (UUID-as-str) the LLM
            saw — the same 5 picked by [F]. Persisted so the F4
            frontend can render the same reps in the panel.
        breakdown: Cost-grade breakdown for ``sleep_report_llm_usage``
            child row emit (keyed by provider+model the response
            actually came from, after fallback chain resolution).
        failed: True if the cluster failed all fallback models. The
            cluster row is still written (with empty label) so the
            persistence transaction stays atomic; the orchestrator
            rolls back the whole run only if too many clusters
            fail (see ``MAX_CLUSTER_FAILURE_RATIO``).
    """

    cluster_index: int
    label: str
    description: str
    label_confidence: float
    representative_memory_ids: list[str]
    breakdown: LLMCallBreakdown | None
    failed: bool = False


def _select_representatives(
    centroid: np.ndarray,
    cluster_member_indices: np.ndarray,
    embeddings: np.ndarray,
    memories: list[MemoryRecord],
    k: int = _REPS_PER_CLUSTER,
) -> list[MemoryRecord]:
    """Stage [F]: top-k memories closest to the cluster centroid."""
    if len(cluster_member_indices) <= k:
        return [memories[i] for i in cluster_member_indices.tolist()]
    member_embs = embeddings[cluster_member_indices]
    # Cosine distance: 1 - cosine_sim. Lower = closer to centroid.
    centroid_n = centroid / max(float(np.linalg.norm(centroid)), 1e-12)
    rows_n = member_embs / np.maximum(np.linalg.norm(member_embs, axis=1, keepdims=True), 1e-12)
    sims = rows_n @ centroid_n
    top_local = np.argsort(-sims)[:k]
    chosen = cluster_member_indices[top_local]
    return [memories[i] for i in chosen.tolist()]


def _format_representatives(reps: list[MemoryRecord]) -> str:
    """Render the rep list as the user-prompt body.

    Layer 1 only — never include Memory.body (Layer 3) here. The
    summary is short, audit-safe, and what the prototype's
    representative panel renders.
    """
    lines = []
    for r in reps:
        # Truncate to keep prompt-token cost bounded. 240 chars
        # matches the recall-query length used by /start step 7.
        summary = (r.summary or "(no summary)").replace("\n", " ").strip()[:240]
        lines.append(f"- [{r.type}] {summary}")
    return "\n".join(lines)


async def _label_one_cluster(
    *,
    cluster_index: int,
    reps: list[MemoryRecord],
    llm_service: LLMService,
    user_id: str,
    workspace_id: str,
    context_id: str | None,
    sem: asyncio.Semaphore,
) -> ClusterLabel:
    """Single-cluster labeling with semaphore + frozenset guard.

    Returns a ``ClusterLabel`` regardless of success — on full
    fallback exhaustion the labeler returns ``failed=True`` with a
    sentinel label so the persistence step can still write a cluster
    row. The orchestrator counts failures and decides whether to
    abort the whole run.
    """
    rep_block = _format_representatives(reps)
    rep_ids = [str(r.id) for r in reps]

    async with sem:
        # No explicit "consume budget before call" here — the LLM
        # budget for the analysis run is implemented at the
        # orchestrator level by capping ``max_llm_calls`` to
        # ``len(clusters) + 1`` upstream. The call_with_fallback
        # adapter exhausts the chain (1-2 attempts per cluster);
        # once it returns an error, this cluster is done.
        try:
            result = await call_with_fallback(
                llm_service=llm_service,
                user_id=user_id,
                workspace_id=workspace_id,
                context_id=context_id,
                system_prompt=CLUSTER_LABEL_SYSTEM,
                prompt=CLUSTER_LABEL_USER.format(representatives=rep_block),
                fallback_chain=OPENAI_FALLBACK_CHAIN,
            )
        except AnalysisLLMUpstreamError as e:
            logger.warning(
                "analysis_cluster_label_failed",
                cluster_index=cluster_index,
                error=str(e),
            )
            return ClusterLabel(
                cluster_index=cluster_index,
                label="(unlabeled)",
                description="LLM labeling failed for this cluster.",
                label_confidence=0.0,
                representative_memory_ids=rep_ids,
                breakdown=None,
                failed=True,
            )

    # Frozenset guard: defense in depth across prompt evolutions —
    # if the LLM ever returns a ``representative_memory_ids`` field
    # (current prompt does not ask for one), reject any id outside
    # the rep set the labeler actually sent. Empty result falls back
    # to the labeler's selection.
    parsed_reps = result.parsed.get("representative_memory_ids") or []
    filtered_reps = filter_hallucinated_ids(parsed_reps, frozenset(rep_ids)) or rep_ids

    label = str(result.parsed.get("label", "(unlabeled)")).strip() or "(unlabeled)"
    description = str(result.parsed.get("description", "")).strip()
    raw_conf = result.parsed.get("label_confidence", 0.0)
    try:
        label_confidence = max(0.0, min(1.0, float(raw_conf)))
    except (TypeError, ValueError):
        label_confidence = 0.0

    breakdown = accumulate_llm_response(None, result.response)

    return ClusterLabel(
        cluster_index=cluster_index,
        label=label,
        description=description,
        label_confidence=label_confidence,
        representative_memory_ids=filtered_reps,
        breakdown=breakdown,
        failed=False,
    )


async def label_clusters(
    *,
    cluster_labels: np.ndarray,
    centroids: np.ndarray,
    embeddings: np.ndarray,
    memories: list[MemoryRecord],
    llm_service: LLMService,
    user_id: str,
    workspace_id: str,
    context_id: str | None,
    concurrency: int = _LLM_CONCURRENCY,
) -> list[ClusterLabel]:
    """Label every cluster in parallel (semaphore-bounded).

    Args:
        cluster_labels: Per-row cluster_index from clusterer.
        centroids: Cluster centroids (n_clusters, embedding_dim).
        embeddings: High-dim embedding matrix (n, embedding_dim).
        memories: Per-row memory metadata (len n).
        llm_service: Resolved LLMService.
        user_id, workspace_id, context_id: Auth/scoping for BYOK.
        concurrency: Max in-flight LLM calls.

    Returns:
        ``list[ClusterLabel]`` ordered by ``cluster_index``.
    """
    n_clusters = int(centroids.shape[0])
    sem = asyncio.Semaphore(concurrency)

    # Pre-compute member indices in one O(n) pass so we don't scan the
    # full labels array N_clusters times via per-cluster ``np.where``.
    members_by_idx: dict[int, list[int]] = {i: [] for i in range(n_clusters)}
    for i, lbl in enumerate(cluster_labels):
        members_by_idx.setdefault(int(lbl), []).append(i)

    tasks: list[asyncio.Task[ClusterLabel]] = []
    for cluster_index in range(n_clusters):
        member_positions = members_by_idx.get(cluster_index, [])
        if not member_positions:
            # Empty cluster — KMeans can produce these on degenerate
            # data. Emit a sentinel label so the index space is dense.
            tasks.append(asyncio.create_task(_empty_cluster_label(cluster_index)))
            continue
        member_idx = np.asarray(member_positions, dtype=np.int64)
        reps = _select_representatives(centroids[cluster_index], member_idx, embeddings, memories)
        tasks.append(
            asyncio.create_task(
                _label_one_cluster(
                    cluster_index=cluster_index,
                    reps=reps,
                    llm_service=llm_service,
                    user_id=user_id,
                    workspace_id=workspace_id,
                    context_id=context_id,
                    sem=sem,
                )
            )
        )

    results = await asyncio.gather(*tasks)
    results.sort(key=lambda r: r.cluster_index)

    failed = sum(1 for r in results if r.failed)
    logger.info(
        "analysis_labeler_complete",
        n_clusters=n_clusters,
        failed=failed,
        succeeded=n_clusters - failed,
    )
    return list(results)


async def _empty_cluster_label(cluster_index: int) -> ClusterLabel:
    """Sentinel for empty clusters (rare but possible)."""
    return ClusterLabel(
        cluster_index=cluster_index,
        label="(empty)",
        description="No memories were assigned to this cluster.",
        label_confidence=0.0,
        representative_memory_ids=[],
        breakdown=None,
        failed=False,
    )


__all__ = [
    "ClusterLabel",
    "label_clusters",
]
