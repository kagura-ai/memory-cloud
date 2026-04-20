#!/usr/bin/env python3
"""Measure embedding similarity distributions for a context (Issue #240 Phase A).

Samples memories from a context, fetches top-k neighbor scores from Qdrant, and
reports percentile distributions. Also computes a random-pair baseline for
diagnostic comparison.

Design decisions from the issue (#240 D1–D7):

    D1  Runtime threshold (Phase B) uses the top-k neighbor distribution. The
        random-pair distribution here is DIAGNOSTIC ONLY — it shows the noise
        floor but is not used to set the runtime threshold.
    D2  Suggested threshold is reported as ``max(percentile_p90, floor=0.3)``
        so Phase B's config can ship the floor alongside the percentile.
    D3  Bootstrap gate is ≥200 memories OR ≥10k top-k observations. Phase A
        does NOT enforce the gate — it warns and continues. Phase B is the
        runtime enforcer.

Usage:
    python measure_embedding_threshold.py --context-id <UUID>
    python measure_embedding_threshold.py --context-id <UUID> --memories 500 --top-k 50
    python measure_embedding_threshold.py --context-id <UUID> --output results.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import numpy as np

# Add src to path for imports (script convention; see backend/scripts/*.py)
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from db.base import _get_session_factory  # noqa: E402
from db.qdrant import (  # noqa: E402
    KAGURA_MEMORIES_COLLECTION,
    KAGURA_MEMORIES_VECTOR_NAME,
    get_collection_name,
    get_qdrant_client,
    search_memories_qdrant,
)
from models.config import ContextSearchConfig  # noqa: E402
from models.memory import Memory  # noqa: E402
from utils.logger import get_logger  # noqa: E402

logger = get_logger(__name__)

BOOTSTRAP_MIN_MEMORIES = 200
BOOTSTRAP_MIN_OBSERVATIONS = 10_000
RUNTIME_FLOOR = 0.3

# Qdrant's retrieve() accepts up to a few hundred IDs per call comfortably; 100
# keeps batches small enough to surface per-batch errors early without
# pressuring the HTTP payload limit.
QDRANT_RETRIEVE_BATCH_SIZE = 100

# Concurrency cap for per-memory kNN search. At 12 concurrent searches, 200
# memories complete in roughly one round-trip time instead of 200 serialized
# trips — ~1s vs ~10s on a typical LAN. Qdrant partition isolation per
# context means no cross-request contention at this concurrency.
TOP_K_SEARCH_CONCURRENCY = 12

PERCENTILES = (25, 50, 75, 90, 95, 99)


def compute_percentiles(values: list[float]) -> dict[str, float]:
    """Compute p25/p50/p75/p90/p95/p99 for a list of observations.

    Returns an empty dict if values is empty — callers must check before
    indexing into the result.
    """
    if not values:
        return {}
    arr = np.asarray(values, dtype=np.float64)
    return {f"p{p}": float(np.percentile(arr, p)) for p in PERCENTILES}


def suggest_threshold(p90: float, floor: float = RUNTIME_FLOOR) -> dict[str, float]:
    """Apply D2 floor to a measured p90."""
    return {
        "percentile_p90": float(p90),
        "floor": float(floor),
        "effective": float(max(p90, floor)),
    }


async def resolve_embedding_model(db: AsyncSession, context_id: UUID) -> tuple[str, int, str]:
    """Return (model_name, dimensions, collection_name) for a context.

    Falls back to the legacy kagura_memories collection (text-embedding-3-small, 512)
    when a context has no ContextSearchConfig row.
    """
    result = await db.execute(
        select(ContextSearchConfig).where(ContextSearchConfig.context_id == context_id)
    )
    cfg = result.scalar_one_or_none()
    if cfg is None:
        return ("text-embedding-3-small", 512, KAGURA_MEMORIES_COLLECTION)
    return (
        cfg.embedding_model,
        cfg.embedding_dimensions,
        get_collection_name(cfg.embedding_model, cfg.embedding_dimensions),
    )


async def count_memories(db: AsyncSession, context_id: UUID) -> int:
    """Count searchable memories (not deleted, embedding_status='success')."""
    result = await db.execute(
        select(func.count(Memory.id)).where(
            Memory.context_id == context_id,
            Memory.deleted_at.is_(None),
            Memory.embedding_status == "success",
        )
    )
    return int(result.scalar_one())


async def sample_memories(db: AsyncSession, context_id: UUID, n: int) -> list[Memory]:
    """Random-sample n searchable memories from a context."""
    stmt = (
        select(Memory)
        .where(
            Memory.context_id == context_id,
            Memory.deleted_at.is_(None),
            Memory.embedding_status == "success",
        )
        .order_by(func.random())
        .limit(n)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def fetch_vectors(
    qdrant: Any, collection: str, memory_ids: list[UUID]
) -> dict[str, list[float]]:
    """Batch-fetch dense vectors keyed by memory id (string form).

    Points missing from Qdrant are silently skipped — this mirrors the
    tolerance the runtime already applies when Postgres and Qdrant briefly
    diverge.
    """
    if not memory_ids:
        return {}
    ids_str = [str(mid) for mid in memory_ids]
    vectors: dict[str, list[float]] = {}
    for i in range(0, len(ids_str), QDRANT_RETRIEVE_BATCH_SIZE):
        batch = ids_str[i : i + QDRANT_RETRIEVE_BATCH_SIZE]
        points = await qdrant.retrieve(
            collection_name=collection,
            ids=batch,
            with_vectors=[KAGURA_MEMORIES_VECTOR_NAME],
            with_payload=False,
        )
        for p in points:
            vec = p.vector
            dense = vec.get(KAGURA_MEMORIES_VECTOR_NAME) if isinstance(vec, dict) else None
            if dense:
                vectors[str(p.id)] = list(dense)
            else:
                logger.warning("fetch_vectors_missing_vector", point_id=str(p.id))
    return vectors


async def measure_top_k(
    memories: list[Memory],
    vectors: dict[str, list[float]],
    collection: str,
    top_k: int,
) -> list[float]:
    """Collect up to ``top_k`` neighbor scores for each sampled memory.

    Matches runtime ``_create_knn_seed_edges`` isolation: each memory queries
    with its own (user_id, workspace_id, context_id). The self-hit
    (cosine=1.0) is excluded. Runtime uses ``limit=k+1`` for the self-hit
    slot; we do the same.

    Searches run concurrently with a semaphore cap (TOP_K_SEARCH_CONCURRENCY)
    so a 200-memory run is bound by one round-trip rather than N serial ones.
    """
    sem = asyncio.Semaphore(TOP_K_SEARCH_CONCURRENCY)

    async def _one(mem: Memory) -> list[float]:
        mem_id_str = str(mem.id)
        vec = vectors.get(mem_id_str)
        if not vec or not mem.workspace_id or not mem.context_id or not mem.user_id:
            return []
        async with sem:
            try:
                candidates = await search_memories_qdrant(
                    user_id=str(mem.user_id),
                    query_vector=vec,
                    workspace_id=str(mem.workspace_id),
                    context_id=str(mem.context_id),
                    limit=top_k + 1,
                    collection_name=collection,
                )
            except Exception as exc:
                logger.warning(
                    "measure_top_k_search_failed",
                    memory_id=mem_id_str,
                    error=str(exc),
                )
                return []
        non_self = [float(c["score"]) for c in candidates if str(c["id"]) != mem_id_str]
        return non_self[:top_k]

    per_memory = await asyncio.gather(*(_one(m) for m in memories))
    return [s for sublist in per_memory for s in sublist]


def measure_random_pair(
    vectors: list[list[float]], n_pairs: int, *, seed: int | None = None
) -> list[float]:
    """Compute pairwise cosine for n_pairs random pairs (diagnostic, D1).

    Samples 2*n_pairs unique vector indices and splits them into two halves,
    so no vector is paired with itself and no pair repeats. When fewer than
    2*n_pairs vectors are available, the pair count is reduced to
    floor(len(vectors)/2).
    """
    arr = np.asarray(vectors, dtype=np.float64)
    available = arr.shape[0]
    max_pairs = available // 2
    if max_pairs == 0:
        return []
    pairs = min(n_pairs, max_pairs)
    rng = np.random.default_rng(seed)
    idx = rng.permutation(available)[: 2 * pairs]
    a = arr[idx[:pairs]]
    b = arr[idx[pairs:]]
    dots = np.einsum("ij,ij->i", a, b)
    norms = np.linalg.norm(a, axis=1) * np.linalg.norm(b, axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        sims = np.where(norms > 0, dots / norms, 0.0)
    return [float(s) for s in sims]


def build_report(
    *,
    context_id: UUID,
    model_name: str,
    dimensions: int,
    collection: str,
    sampled_memories: int,
    top_k_requested: int,
    random_pairs_requested: int,
    top_k_scores: list[float],
    random_pair_scores: list[float],
) -> dict[str, Any]:
    """Compose the final JSON-serializable report."""
    top_k_pcts = compute_percentiles(top_k_scores)
    pair_pcts = compute_percentiles(random_pair_scores)

    p90 = top_k_pcts.get("p90")
    threshold = suggest_threshold(p90) if p90 is not None else None

    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "context_id": str(context_id),
        "model": {"name": model_name, "dimensions": dimensions, "collection": collection},
        "sample": {
            "memories": sampled_memories,
            "top_k": top_k_requested,
            "random_pairs": random_pairs_requested,
            "observations_total": len(top_k_scores),
            "random_pair_observations": len(random_pair_scores),
        },
        "top_k_distribution": top_k_pcts,
        "random_pair_distribution": pair_pcts,
        "suggested_threshold": threshold,
    }


def check_bootstrap_gate(report: dict[str, Any]) -> list[str]:
    """Return warnings for D3 bootstrap under-sizing.

    D3 gate passes when ``memories >= BOOTSTRAP_MIN_MEMORIES`` OR
    ``observations_total >= BOOTSTRAP_MIN_OBSERVATIONS`` — either condition
    alone is enough for a stable percentile estimate. Warnings fire only when
    BOTH are below their thresholds. Emits a structlog event alongside the
    returned strings so operators can grep the log for the same signal.
    Warnings are informational: the function never mutates the report or
    exits.
    """
    memories = report["sample"]["memories"]
    observations = report["sample"]["observations_total"]
    if memories >= BOOTSTRAP_MIN_MEMORIES or observations >= BOOTSTRAP_MIN_OBSERVATIONS:
        return []

    logger.warning(
        "bootstrap_gate_below_threshold",
        memories=memories,
        observations=observations,
        min_memories=BOOTSTRAP_MIN_MEMORIES,
        min_observations=BOOTSTRAP_MIN_OBSERVATIONS,
    )
    return [
        (
            f"sample_size_below_bootstrap_gate: {memories} memories "
            f"(< {BOOTSTRAP_MIN_MEMORIES}); percentile estimate may be unstable"
        ),
        (
            f"observations_below_bootstrap_gate: {observations} top-k observations "
            f"(< {BOOTSTRAP_MIN_OBSERVATIONS})"
        ),
    ]


def _print_distribution(label: str, dist: dict[str, float]) -> None:
    """Print a labeled percentile table, or a sentinel line when empty."""
    if not dist:
        print(f"{label}: <no observations>")
        return
    print(label)
    for p in PERCENTILES:
        key = f"p{p}"
        print(f"  {key:>4}: {dist[key]:.4f}")


def print_report(report: dict[str, Any], warnings: list[str]) -> None:
    """Print a human-readable report to stdout."""
    print("\n" + "=" * 66)
    print("Embedding Threshold Calibration")
    print("=" * 66)
    print(f"Context:     {report['context_id']}")
    print(f"Model:       {report['model']['name']} ({report['model']['dimensions']}d)")
    print(f"Collection:  {report['model']['collection']}")
    print(f"Timestamp:   {report['timestamp']}")
    print()
    print("Sample")
    print(f"  Memories sampled:   {report['sample']['memories']:,}")
    print(f"  Top-k per memory:   {report['sample']['top_k']:,}")
    print(f"  Observations (top-k): {report['sample']['observations_total']:,}")
    print(f"  Random pairs:       {report['sample']['random_pair_observations']:,}")
    print()

    _print_distribution("Top-k neighbor distribution", report["top_k_distribution"])
    print()
    _print_distribution("Random-pair baseline", report["random_pair_distribution"])
    print()

    threshold = report["suggested_threshold"]
    if threshold:
        print("Suggested runtime threshold")
        print(f"  percentile_p90: {threshold['percentile_p90']:.4f}")
        print(f"  floor:          {threshold['floor']:.4f}")
        print(f"  effective:      {threshold['effective']:.4f}")
    else:
        print("Suggested runtime threshold: <insufficient data>")

    if warnings:
        print()
        print("⚠️  Warnings")
        for w in warnings:
            print(f"  - {w}")

    print("=" * 66)


async def measure(
    *,
    context_id: UUID,
    memories: int,
    top_k: int,
    random_pairs: int,
    seed: int | None,
) -> dict[str, Any]:
    """Orchestrate the measurement end-to-end and return the report dict."""
    session_factory = _get_session_factory()
    async with session_factory() as db:
        model_name, dimensions, collection = await resolve_embedding_model(db, context_id)
        total = await count_memories(db, context_id)
        logger.info(
            "measure_start",
            context_id=str(context_id),
            model=model_name,
            dimensions=dimensions,
            collection=collection,
            context_memory_count=total,
            requested_memories=memories,
            requested_top_k=top_k,
        )
        sampled = await sample_memories(db, context_id, memories)

    qdrant = get_qdrant_client()
    vectors = await fetch_vectors(qdrant, collection, [m.id for m in sampled])
    top_k_scores = await measure_top_k(sampled, vectors, collection, top_k)
    pair_scores = measure_random_pair(list(vectors.values()), random_pairs, seed=seed)

    return build_report(
        context_id=context_id,
        model_name=model_name,
        dimensions=dimensions,
        collection=collection,
        sampled_memories=len(sampled),
        top_k_requested=top_k,
        random_pairs_requested=random_pairs,
        top_k_scores=top_k_scores,
        random_pair_scores=pair_scores,
    )


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Measure embedding similarity distributions for a context (Issue #240 Phase A)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--context-id", type=UUID, required=True, help="Target context UUID")
    parser.add_argument(
        "--memories", type=int, default=200, help="Number of memories to sample (default: 200)"
    )
    parser.add_argument(
        "--top-k", type=int, default=50, help="Top-k neighbors per memory (default: 50)"
    )
    parser.add_argument(
        "--random-pairs",
        type=int,
        default=1000,
        help="Random pair count for diagnostic baseline (default: 1000)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional path to write the full JSON report (default: stdout only)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Optional RNG seed for random-pair sampling (reproducible diagnostic)",
    )
    args = parser.parse_args()

    if args.memories <= 0 or args.top_k <= 0 or args.random_pairs < 0:
        print(
            "error: --memories and --top-k must be positive; --random-pairs must be ≥ 0",
            file=sys.stderr,
        )
        sys.exit(2)

    report = await measure(
        context_id=args.context_id,
        memories=args.memories,
        top_k=args.top_k,
        random_pairs=args.random_pairs,
        seed=args.seed,
    )
    warnings = check_bootstrap_gate(report)
    print_report(report, warnings)

    if args.output:
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        print(f"\nFull report written to {args.output}")


if __name__ == "__main__":
    asyncio.run(main())
