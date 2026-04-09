"""Pilot #249 sampling script for Sleep edge_discovery probe.

READ-ONLY research script. Does NOT touch services/sleep production code.
Output: ``snapshot.json`` + ``pairs.jsonl`` in ``--out-dir``.

Reproducibility: ``numpy.random.default_rng(seed=42)`` is the only RNG.
The same DB snapshot must yield byte-identical ``pairs.jsonl`` across runs.
Determinism is guarded by:
  1. ``build_eligible_pairs`` sorts candidates by canonical key BEFORE the
     RNG sees them.
  2. ``load_memories_for_context`` sorts loaded memories by id_str so that
     Qdrant page-boundary ordering cannot leak into the sample.
  3. The single ``rng`` instance is threaded through every stratum draw in
     a fixed order (kagura-dev → personal_memo, A → B → C, then deterministic
     Stratum D from A's universe).

Usage:
    python sampling_script.py \\
        --user-id <user-uuid> \\
        --workspace-id <workspace-uuid> \\
        [--out-dir .] \\
        [--seed 42] \\
        [--dry-run]

Note on ``print()``: this script intentionally uses ``print()`` for its CLI
summary output. The "no print()" backend rule in ``.claude/rules/backend.md``
applies to production service code, not standalone CLI tools whose primary
user interface is stdout. ``structlog`` is still used for events.

See ``README.md`` in this directory for the full pilot context.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

import numpy as np

# --- Make backend/src importable when running as a CLI script. ---
# pytest already has ``pythonpath = ["src"]``, so this is a no-op under tests.
HERE = Path(__file__).resolve().parent
# HERE = backend/tests/services/sleep/eval/pilot_2026_04
# parents: [eval, sleep, services, tests, backend]
BACKEND_SRC = HERE.parents[4] / "src"
if BACKEND_SRC.exists() and str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

# --- Dev convenience: auto-load backend/.env.local before touching config. ---
# DATABASE_URL and QDRANT_URL are read at module-import time inside
# ``db.base`` / ``db.qdrant``, so env vars MUST be set BEFORE those imports
# below. Production is configured via docker-compose env vars and does not
# need this path. override=False means shell-exported vars still win.
try:
    from dotenv import load_dotenv as _load_dotenv  # type: ignore[import-not-found]

    _ENV_LOCAL = BACKEND_SRC.parent / ".env.local"
    if _ENV_LOCAL.exists():
        _load_dotenv(_ENV_LOCAL, override=False)
except ImportError:
    pass  # python-dotenv not installed — caller must set env vars in shell

# These imports require BACKEND_SRC on sys.path.
from qdrant_client.models import FieldCondition, Filter, MatchValue  # noqa: E402
from sqlalchemy import select  # noqa: E402

from db.base import _get_session_factory  # noqa: E402  # type: ignore[import-not-found]
from db.qdrant import KAGURA_MEMORIES_COLLECTION, get_qdrant_client  # noqa: E402  # type: ignore[import-not-found]
from models.auth import Context  # noqa: E402  # type: ignore[import-not-found]
from models.memory import Memory, NeuralMemoryEdge  # noqa: E402  # type: ignore[import-not-found]
from utils.logger import get_logger  # noqa: E402  # type: ignore[import-not-found]

logger = get_logger(__name__)


# ============================================================================
# Constants — refinement #1: per-cell n is pinned, not computed.
# Verified by ``test_pilot_2026_04_sampling.py``.
# ============================================================================

SEED = 42

# Sampling source contexts. Order is load-order (used for the RNG's stratum
# walk); kagura-dev first → personal_memo second.
CONTEXT_NAMES: tuple[str, ...] = ("kagura-dev", "personal_memo")

# Fallback threshold: if personal_memo has fewer than this many memories,
# kagura-dev absorbs the entire 50-pair budget.
MIN_MEMORIES_FOR_DUAL_CONTEXT = 100

# Per-cell n. Totals: A=25, B=10, C=8, D=7, all=50. 60/40 ratio between
# kagura-dev and personal_memo, mirroring the per-context totals.
ALLOCATION: dict[str, dict[str, int]] = {
    "kagura-dev": {"A": 15, "B": 6, "C": 5, "D": 4},
    "personal_memo": {"A": 10, "B": 4, "C": 3, "D": 3},
}

# Fallback when ``personal_memo`` is missing or undersized.
ALLOCATION_FALLBACK: dict[str, dict[str, int]] = {
    "kagura-dev": {"A": 25, "B": 10, "C": 8, "D": 7},
    "personal_memo": {"A": 0, "B": 0, "C": 0, "D": 0},
}

# Cosine bands. A and C are half-open ``[lo, hi)``; B is closed ``[lo, hi]``
# to match production ``SIMILARITY_MIN``/``SIMILARITY_MAX`` in
# ``services/sleep/edge_discovery.py``.
STRATUM_BANDS: dict[str, tuple[float, float]] = {
    "A": (0.4, 0.6),
    "B": (0.6, 0.9),
    "C": (0.2, 0.4),
    # D has no band — it is shared-tag-ranked from A's eligible universe.
}

# Mirror of ``services.sleep.edge_discovery.SEMANTIC_SIMILARITY_SYNTHETIC_WEIGHT_THRESHOLD``.
# If production drifts, ``test_synthetic_seed_matches_production`` fails loudly.
SEMANTIC_SIMILARITY_SYNTHETIC_WEIGHT_THRESHOLD = 0.5


# ============================================================================
# Data classes
# ============================================================================


@dataclass(frozen=True)
class MemoryRecord:
    """One memory loaded from Postgres + Qdrant for the pilot.

    ``vector`` is unit-normalized at load time so that cosine similarity is
    just a dot product.
    """

    id: UUID
    summary: str
    tags: tuple[str, ...]
    vector: np.ndarray
    created_at_iso: str

    @property
    def id_str(self) -> str:
        return str(self.id)


@dataclass
class Candidate:
    """One candidate pair under consideration for sampling."""

    src: MemoryRecord
    dst: MemoryRecord
    cosine: float
    has_existing_edge: bool
    existing_edge_type: str | None
    synthetic_seed_edge: bool

    @property
    def canonical_key(self) -> tuple[str, str]:
        """Sorted ``(src_id, dst_id)`` pair, for dedup and deterministic order."""
        a, b = sorted([self.src.id_str, self.dst.id_str])
        return (a, b)


# ============================================================================
# Production parity
# ============================================================================


def is_synthetic_seed(edge_type: str, weight: float) -> bool:
    """Mirror of ``services.sleep.edge_discovery._is_synthetic_seed_edge``.

    Pure-data form (takes type + weight, not an ORM object) so it can be
    called against the in-memory edge lookup table.
    """
    return (
        edge_type == "semantic_similarity"
        and weight < SEMANTIC_SIMILARITY_SYNTHETIC_WEIGHT_THRESHOLD
    )


# ============================================================================
# Cosine matrix
# ============================================================================


def compute_cosine_matrix(memories: list[MemoryRecord]) -> np.ndarray:
    """N×N cosine matrix.

    Vectors are unit-normalized at load time, so cosine reduces to a matmul.
    For N up to a few thousand this is trivially fast (<1s, <100 MB RAM).
    """
    if not memories:
        return np.zeros((0, 0))
    matrix = np.stack([m.vector for m in memories])
    return matrix @ matrix.T


# ============================================================================
# Pair enumeration + sampling
# ============================================================================


def build_eligible_pairs(
    memories: list[MemoryRecord],
    cosine: np.ndarray,
    edges_by_pair: dict[tuple[str, str], tuple[str, float]],
    band: tuple[float, float],
    require_no_edge: bool,
    half_open: bool = True,
) -> list[Candidate]:
    """Enumerate all ``(i, j)`` with ``i < j`` where ``cosine[i, j]`` is in ``band``.

    If ``require_no_edge`` (Stratum A semantics), exclude pairs that have any
    non-synthetic edge between them. Synthetic seed edges (low-weight
    ``semantic_similarity``) are NOT considered "connected" — same predicate
    as production ``_filter_existing_edges``.

    Returned list is sorted by ``canonical_key`` BEFORE any RNG call sees it,
    so dict/set iteration order cannot leak into seed-42 reproducibility.
    """
    lo, hi = band
    candidates: list[Candidate] = []
    n = len(memories)
    for i in range(n):
        for j in range(i + 1, n):
            score = float(cosine[i, j])
            if half_open:
                if not (lo <= score < hi):
                    continue
            elif not (lo <= score <= hi):
                continue

            src = memories[i]
            dst = memories[j]
            key = (
                min(src.id_str, dst.id_str),
                max(src.id_str, dst.id_str),
            )
            edge = edges_by_pair.get(key)

            if edge is None:
                has_edge = False
                edge_type: str | None = None
                synthetic = False
            else:
                edge_type, weight = edge
                synthetic = is_synthetic_seed(edge_type, weight)
                has_edge = not synthetic
                if synthetic:
                    edge_type = None  # do not surface synthetic seeds in metadata

            if require_no_edge and has_edge:
                continue

            candidates.append(
                Candidate(
                    src=src,
                    dst=dst,
                    cosine=score,
                    has_existing_edge=has_edge,
                    existing_edge_type=edge_type,
                    synthetic_seed_edge=synthetic,
                )
            )

    candidates.sort(key=lambda c: c.canonical_key)
    return candidates


def sample_stratum(
    candidates: list[Candidate],
    n: int,
    rng: np.random.Generator,
) -> list[Candidate]:
    """Deterministic sample without replacement, ``n`` items.

    If fewer than ``n`` candidates exist, returns all of them and logs a
    warning. The caller decides whether undersampling is fatal.
    """
    if not candidates or n <= 0:
        return []
    if len(candidates) <= n:
        logger.warning(
            "stratum_undersampled",
            available=len(candidates),
            requested=n,
        )
        return list(candidates)
    indices = rng.choice(len(candidates), size=n, replace=False)
    indices.sort()  # preserve canonical order in the output
    return [candidates[int(i)] for i in indices]


def build_stratum_d(
    stratum_a_picked: list[Candidate],
    stratum_a_universe: list[Candidate],
    n: int,
) -> list[Candidate]:
    """Build hard negatives for Stratum D (refinement #5).

    NOT random sampling. This is a DETERMINISTIC RANK + TOP-K of pairs that
    LOOK similar to picked Stratum A pairs (high shared-tag overlap) but
    might be inferentially unrelated. Annotator validation only — Stratum D
    counts are excluded from the main findings.

    Procedure:
      1. Universe = ``stratum_a_universe \\ stratum_a_picked`` — i.e. the
         Stratum A eligible pairs that did not get sampled into the main
         A bucket.
      2. For each candidate ``c`` in the universe, compute::

            score(c) = max over picked p of |tags(c) & tags(p)|

         where ``tags(x) = src.tags ∪ dst.tags``.
      3. Sort by ``(-score, cosine_int, canonical_key)`` — higher overlap
         first; lower cosine wins ties (harder negative); canonical key is
         the deterministic tiebreak.
      4. Return top ``n``.

    Each row in ``pairs.jsonl`` for Stratum D records the ``shared_tag_count``
    and the ``pair_id`` of the Stratum A pair it was scored against, so the
    derivation is auditable.
    """
    if not stratum_a_picked or n <= 0:
        return []

    picked_keys = {c.canonical_key for c in stratum_a_picked}
    universe = [c for c in stratum_a_universe if c.canonical_key not in picked_keys]
    if not universe:
        logger.warning("stratum_d_universe_empty", picked=len(stratum_a_picked))
        return []

    scored: list[tuple[int, int, tuple[str, str], Candidate]] = []
    for c in universe:
        c_tags = set(c.src.tags) | set(c.dst.tags)
        if not c_tags:
            continue

        best_overlap = 0
        for p in stratum_a_picked:
            p_tags = set(p.src.tags) | set(p.dst.tags)
            overlap = len(c_tags & p_tags)
            if overlap > best_overlap:
                best_overlap = overlap

        if best_overlap == 0:
            continue

        # Negate score for ascending sort = descending overlap.
        # ``cosine_int`` makes lower cosine sort first (harder negative).
        scored.append(
            (
                -best_overlap,
                int(c.cosine * 1000),
                c.canonical_key,
                c,
            )
        )

    if not scored:
        logger.warning("stratum_d_no_overlapping_pairs")
        return []

    scored.sort()
    return [c for (_, _, _, c) in scored[:n]]


# ============================================================================
# Async data loading
# ============================================================================


async def resolve_context_id(
    db,
    workspace_id: str,
    context_name: str,
) -> str | None:
    """Look up ``context_id`` by ``(workspace_id, name)``.

    Returns ``None`` if the context does not exist or is soft-deleted.
    """
    stmt = (
        select(Context.id)
        .where(
            Context.workspace_id == UUID(workspace_id),
            Context.name == context_name,
            Context.deleted_at.is_(None),
        )
        .limit(1)
    )
    result = await db.execute(stmt)
    row = result.first()
    return str(row[0]) if row else None


async def load_memories_for_context(
    user_id: str,
    workspace_id: str,
    context_name: str,
    db,
) -> tuple[list[MemoryRecord], str | None]:
    """Resolve ``context_name`` → ``context_id``, then scroll all live memories
    with embeddings from Qdrant for that ``(user, workspace, context)``.

    Returns ``(memories, context_id)``. Returns ``([], None)`` if the context
    does not exist; ``([], context_id)`` if the context exists but has zero
    qualifying memories.
    """
    context_id = await resolve_context_id(db, workspace_id, context_name)
    if context_id is None:
        logger.warning(
            "context_not_found",
            workspace_id=workspace_id,
            context_name=context_name,
        )
        return [], None

    # Pull live memory metadata from Postgres first.
    stmt = select(Memory.id, Memory.summary, Memory.tags, Memory.created_at).where(
        Memory.user_id == user_id,
        Memory.workspace_id == UUID(workspace_id),
        Memory.context_id == UUID(context_id),
        Memory.deleted_at.is_(None),
        Memory.embedding_status == "success",
    )
    result = await db.execute(stmt)
    rows = result.all()
    if not rows:
        return [], context_id

    metadata_by_id: dict[str, dict[str, Any]] = {}
    for memory_id, summary, tags, created_at in rows:
        metadata_by_id[str(memory_id)] = {
            "summary": summary,
            "tags": tuple(tags or ()),
            "created_at_iso": created_at.isoformat() if created_at else "",
        }

    # Scroll Qdrant for the dense vectors.
    client = get_qdrant_client()
    scroll_filter = Filter(
        must=[
            FieldCondition(key="user_id", match=MatchValue(value=user_id)),
            FieldCondition(key="workspace_id", match=MatchValue(value=workspace_id)),
            FieldCondition(key="context_id", match=MatchValue(value=context_id)),
        ]
    )

    memories: list[MemoryRecord] = []
    next_offset: Any = None
    while True:
        points, next_offset = await client.scroll(
            collection_name=KAGURA_MEMORIES_COLLECTION,
            scroll_filter=scroll_filter,
            limit=512,
            offset=next_offset,
            with_vectors=["dense"],
            with_payload=False,
        )
        for point in points:
            point_id = str(point.id)
            meta = metadata_by_id.get(point_id)
            if meta is None:
                # Vector exists in Qdrant but no live PG row → skip.
                continue
            vector_dict = point.vector if isinstance(point.vector, dict) else {}
            dense = vector_dict.get("dense")
            if not dense:
                continue
            v = np.asarray(dense, dtype=np.float64)
            norm = float(np.linalg.norm(v))
            if norm == 0.0:
                continue
            v = v / norm
            memories.append(
                MemoryRecord(
                    id=UUID(point_id),
                    summary=meta["summary"],
                    tags=meta["tags"],
                    vector=v,
                    created_at_iso=meta["created_at_iso"],
                )
            )
        if next_offset is None:
            break

    # Deterministic order regardless of Qdrant page boundaries.
    memories.sort(key=lambda m: m.id_str)
    logger.info(
        "context_memories_loaded",
        context_name=context_name,
        context_id=context_id,
        count=len(memories),
    )
    return memories, context_id


async def existing_edge_lookup(
    db,
    user_id: str,
    memory_ids: list[UUID],
) -> dict[tuple[str, str], tuple[str, float]]:
    """Fetch all edges where BOTH ``src_id`` and ``dst_id`` are in ``memory_ids``.

    Returns ``{canonical_key → (edge_type, weight)}`` where ``canonical_key``
    is a sorted ``(src_str, dst_str)`` tuple. If both directions of an edge
    exist for the same pair, the higher-weight one wins (more "real").
    """
    if not memory_ids:
        return {}

    stmt = select(
        NeuralMemoryEdge.src_id,
        NeuralMemoryEdge.dst_id,
        NeuralMemoryEdge.edge_type,
        NeuralMemoryEdge.weight,
    ).where(
        NeuralMemoryEdge.user_id == user_id,
        NeuralMemoryEdge.src_id.in_(memory_ids),
        NeuralMemoryEdge.dst_id.in_(memory_ids),
    )
    result = await db.execute(stmt)
    edges_by_pair: dict[tuple[str, str], tuple[str, float]] = {}
    for src_id, dst_id, edge_type, weight in result.all():
        key = (
            min(str(src_id), str(dst_id)),
            max(str(src_id), str(dst_id)),
        )
        existing = edges_by_pair.get(key)
        if existing is None or float(weight) > existing[1]:
            edges_by_pair[key] = (str(edge_type), float(weight))
    return edges_by_pair


# ============================================================================
# Snapshot
# ============================================================================


def _git_sha_for_file(rel_path: str) -> str | None:
    """Return the latest commit SHA touching a file, or ``None`` if git fails."""
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%H", "--", rel_path],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        return result.stdout.strip() or None
    except Exception:
        return None


def capture_snapshot(
    *,
    user_id: str,
    workspace_id: str,
    contexts_resolved: dict[str, dict[str, Any]],
    allocation_used: dict[str, dict[str, int]],
    fallback_reason: str | None,
    seed: int,
    out_dir: Path,
) -> dict[str, Any]:
    """Build the snapshot dict that gets written to ``snapshot.json``."""
    return {
        "pilot": "issue_249_pilot_2026_04",
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "user_id": user_id,
        "workspace_id": workspace_id,
        "seed": seed,
        "contexts": contexts_resolved,
        "allocation_used": allocation_used,
        "fallback_reason": fallback_reason,
        "edge_discovery_git_sha": _git_sha_for_file("backend/src/services/sleep/edge_discovery.py"),
        "constants": {
            "MIN_MEMORIES_FOR_DUAL_CONTEXT": MIN_MEMORIES_FOR_DUAL_CONTEXT,
            "STRATUM_BANDS": {k: list(v) for k, v in STRATUM_BANDS.items()},
            "SEMANTIC_SIMILARITY_SYNTHETIC_WEIGHT_THRESHOLD": SEMANTIC_SIMILARITY_SYNTHETIC_WEIGHT_THRESHOLD,  # noqa: E501
        },
        "versions": {
            "python": sys.version.split()[0],
            "numpy": np.__version__,
        },
        "out_dir": str(out_dir),
    }


# ============================================================================
# Pair row builder
# ============================================================================


def _candidate_to_row(
    *,
    pair_id: str,
    candidate: Candidate,
    context_name: str,
    context_id: str,
    stratum: str,
    snapshot_t0: str,
    embedding_model_id: str,
    filter_state: str,
    sampling_seed: int,
    labeling_prompt_sha256: str,
    d_shared_tag_count: int | None = None,
    d_ranked_from_pair_id: str | None = None,
) -> dict[str, Any]:
    """Convert a ``Candidate`` to a ``pairs.jsonl`` row dict."""
    return {
        "pair_id": pair_id,
        "context_name": context_name,
        "context_id": context_id,
        "stratum": stratum,
        "src_id": candidate.src.id_str,
        "dst_id": candidate.dst.id_str,
        "src_summary": candidate.src.summary,
        "dst_summary": candidate.dst.summary,
        "src_tags": list(candidate.src.tags),
        "dst_tags": list(candidate.dst.tags),
        "src_created_at": candidate.src.created_at_iso,
        "dst_created_at": candidate.dst.created_at_iso,
        "cosine_similarity": round(candidate.cosine, 6),
        "embedding_model_id": embedding_model_id,
        "has_existing_edge": candidate.has_existing_edge,
        "existing_edge_type": candidate.existing_edge_type,
        "synthetic_seed_edge": candidate.synthetic_seed_edge,
        "d_shared_tag_count": d_shared_tag_count,
        "d_ranked_from_pair_id": d_ranked_from_pair_id,
        "snapshot_t0": snapshot_t0,
        "filter_state": filter_state,
        "sampling_seed": sampling_seed,
        "labeling_prompt_sha256": labeling_prompt_sha256,
        "annotations": {},
    }


# ============================================================================
# Helpers
# ============================================================================


def _hash_labeling_prompt() -> str:
    """SHA-256 of ``labeling_prompt.md`` next to this script.

    Returns ``"missing"`` and logs a warning if the prompt has not been
    committed yet (the gate1 commitment is that the prompt commit comes
    BEFORE the first annotation run, but sampling can run without it).
    """
    prompt_path = HERE / "labeling_prompt.md"
    if not prompt_path.exists():
        logger.warning("labeling_prompt_missing", path=str(prompt_path))
        return "missing"
    return hashlib.sha256(prompt_path.read_bytes()).hexdigest()


# ============================================================================
# Orchestration
# ============================================================================


async def run_sampling(
    *,
    user_id: str,
    workspace_id: str,
    out_dir: Path,
    seed: int = SEED,
    embedding_model_id: str = "text-embedding-3-small",
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """End-to-end sampling.

    Loads both contexts, decides allocation (dual vs fallback), samples all
    four strata per context, and returns ``(snapshot, list_of_pair_rows)``.
    The caller writes them to disk (or not, in dry-run mode).
    """
    factory = _get_session_factory()

    contexts_resolved: dict[str, dict[str, Any]] = {}
    memories_by_context: dict[str, list[MemoryRecord]] = {}
    edges_by_context: dict[str, dict[tuple[str, str], tuple[str, float]]] = {}

    async with factory() as db:
        for ctx_name in CONTEXT_NAMES:
            mems, ctx_id = await load_memories_for_context(
                user_id=user_id,
                workspace_id=workspace_id,
                context_name=ctx_name,
                db=db,
            )
            contexts_resolved[ctx_name] = {
                "context_id": ctx_id,
                "total_memories": len(mems),
            }
            memories_by_context[ctx_name] = mems

        # Allocation decision.
        pm_count = len(memories_by_context.get("personal_memo", []))
        if pm_count < MIN_MEMORIES_FOR_DUAL_CONTEXT:
            allocation_used = ALLOCATION_FALLBACK
            fallback_reason = (
                f"personal_memo has {pm_count} memories "
                f"(< MIN_MEMORIES_FOR_DUAL_CONTEXT={MIN_MEMORIES_FOR_DUAL_CONTEXT}); "
                f"kagura-dev absorbs all 50 pairs"
            )
            logger.warning("fallback_to_single_context", reason=fallback_reason)
            print(f"⚠️  FALLBACK: {fallback_reason}", file=sys.stderr)
        else:
            allocation_used = ALLOCATION
            fallback_reason = None

        # Edge lookup per context.
        for ctx_name, mems in memories_by_context.items():
            if not mems:
                edges_by_context[ctx_name] = {}
                continue
            edges_by_context[ctx_name] = await existing_edge_lookup(
                db=db,
                user_id=user_id,
                memory_ids=[m.id for m in mems],
            )

    # All DB reads done. Sampling below is pure compute (deterministic).
    rng = np.random.default_rng(seed)
    snapshot_t0 = datetime.now(timezone.utc).isoformat()
    filter_state = "post-248"
    labeling_prompt_sha256 = _hash_labeling_prompt()

    rows: list[dict[str, Any]] = []
    pair_counter = 0

    def _next_pair_id() -> str:
        nonlocal pair_counter
        pair_counter += 1
        return f"p{pair_counter:04d}"

    for ctx_name in CONTEXT_NAMES:
        mems = memories_by_context.get(ctx_name, [])
        ctx_id = contexts_resolved[ctx_name]["context_id"]
        per_stratum_n = allocation_used.get(ctx_name, {})
        if not mems or not ctx_id or not any(per_stratum_n.values()):
            continue

        cosine = compute_cosine_matrix(mems)
        edges_by_pair = edges_by_context[ctx_name]

        # Stratum A — primary, no existing edge
        a_universe = build_eligible_pairs(
            mems,
            cosine,
            edges_by_pair,
            band=STRATUM_BANDS["A"],
            require_no_edge=True,
            half_open=True,
        )
        a_picked = sample_stratum(a_universe, per_stratum_n.get("A", 0), rng)
        for c in a_picked:
            rows.append(
                _candidate_to_row(
                    pair_id=_next_pair_id(),
                    candidate=c,
                    context_name=ctx_name,
                    context_id=ctx_id,
                    stratum="A",
                    snapshot_t0=snapshot_t0,
                    embedding_model_id=embedding_model_id,
                    filter_state=filter_state,
                    sampling_seed=seed,
                    labeling_prompt_sha256=labeling_prompt_sha256,
                )
            )

        # Stratum B — closed band, edges allowed (diagnostic)
        b_universe = build_eligible_pairs(
            mems,
            cosine,
            edges_by_pair,
            band=STRATUM_BANDS["B"],
            require_no_edge=False,
            half_open=False,
        )
        b_picked = sample_stratum(b_universe, per_stratum_n.get("B", 0), rng)
        for c in b_picked:
            rows.append(
                _candidate_to_row(
                    pair_id=_next_pair_id(),
                    candidate=c,
                    context_name=ctx_name,
                    context_id=ctx_id,
                    stratum="B",
                    snapshot_t0=snapshot_t0,
                    embedding_model_id=embedding_model_id,
                    filter_state=filter_state,
                    sampling_seed=seed,
                    labeling_prompt_sha256=labeling_prompt_sha256,
                )
            )

        # Stratum C — diagnostic blind-spot check below 0.4
        c_universe = build_eligible_pairs(
            mems,
            cosine,
            edges_by_pair,
            band=STRATUM_BANDS["C"],
            require_no_edge=False,
            half_open=True,
        )
        c_picked = sample_stratum(c_universe, per_stratum_n.get("C", 0), rng)
        for c in c_picked:
            rows.append(
                _candidate_to_row(
                    pair_id=_next_pair_id(),
                    candidate=c,
                    context_name=ctx_name,
                    context_id=ctx_id,
                    stratum="C",
                    snapshot_t0=snapshot_t0,
                    embedding_model_id=embedding_model_id,
                    filter_state=filter_state,
                    sampling_seed=seed,
                    labeling_prompt_sha256=labeling_prompt_sha256,
                )
            )

        # Stratum D — hard negatives ranked from A's universe
        d_picked = build_stratum_d(
            stratum_a_picked=a_picked,
            stratum_a_universe=a_universe,
            n=per_stratum_n.get("D", 0),
        )
        for c in d_picked:
            # Compute the actual best overlap + the source pair_id for audit.
            c_tags = set(c.src.tags) | set(c.dst.tags)
            best_overlap = 0
            best_pair_id_str: str | None = None
            for p in a_picked:
                p_tags = set(p.src.tags) | set(p.dst.tags)
                overlap = len(c_tags & p_tags)
                if overlap > best_overlap:
                    best_overlap = overlap
                    p_canonical = p.canonical_key
                    for r in rows:
                        if r["stratum"] == "A" and r["context_name"] == ctx_name:
                            r_key = (
                                min(r["src_id"], r["dst_id"]),
                                max(r["src_id"], r["dst_id"]),
                            )
                            if r_key == p_canonical:
                                best_pair_id_str = r["pair_id"]
                                break
            rows.append(
                _candidate_to_row(
                    pair_id=_next_pair_id(),
                    candidate=c,
                    context_name=ctx_name,
                    context_id=ctx_id,
                    stratum="D",
                    snapshot_t0=snapshot_t0,
                    embedding_model_id=embedding_model_id,
                    filter_state=filter_state,
                    sampling_seed=seed,
                    labeling_prompt_sha256=labeling_prompt_sha256,
                    d_shared_tag_count=best_overlap,
                    d_ranked_from_pair_id=best_pair_id_str,
                )
            )

    snapshot = capture_snapshot(
        user_id=user_id,
        workspace_id=workspace_id,
        contexts_resolved=contexts_resolved,
        allocation_used=allocation_used,
        fallback_reason=fallback_reason,
        seed=seed,
        out_dir=out_dir,
    )
    return snapshot, rows


def _print_summary(snapshot: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    """Human-facing summary printed at the end of the run."""
    print()
    print("=" * 60)
    print("Pilot #249 sampling summary")
    print("=" * 60)
    print(f"Captured at: {snapshot['captured_at_utc']}")
    print(f"Seed: {snapshot['seed']}")
    print(f"Edge discovery git SHA: {snapshot['edge_discovery_git_sha']}")
    print()
    print("Contexts resolved:")
    for ctx_name, info in snapshot["contexts"].items():
        print(
            f"  {ctx_name:15s}  total_memories={info['total_memories']}  "
            f"context_id={info['context_id']}"
        )
    if snapshot["fallback_reason"]:
        print(f"\n⚠️  FALLBACK: {snapshot['fallback_reason']}")
    print()
    print("Allocation used:")
    for ctx_name, cells in snapshot["allocation_used"].items():
        cells_str = ", ".join(f"{k}={v}" for k, v in cells.items())
        print(f"  {ctx_name:15s}  {cells_str}")
    print()
    print("Per-cell sampled (actual rows in pairs.jsonl):")
    actual: dict[tuple[str, str], int] = defaultdict(int)
    for row in rows:
        actual[(row["context_name"], row["stratum"])] += 1
    for (ctx_name, stratum), count in sorted(actual.items()):
        print(f"  {ctx_name:15s}  {stratum}: {count}")
    print()
    print(f"Total pairs: {len(rows)}")
    print("=" * 60)


# ============================================================================
# CLI
# ============================================================================


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Sample 50 memory pairs for the #249 pilot eval probe.",
    )
    p.add_argument("--user-id", required=True, help="User UUID")
    p.add_argument("--workspace-id", required=True, help="Workspace UUID")
    p.add_argument(
        "--out-dir",
        default=str(HERE),
        help="Output directory (default: this script's directory)",
    )
    p.add_argument("--seed", type=int, default=SEED)
    p.add_argument(
        "--embedding-model",
        default="text-embedding-3-small",
        help="Embedding model id (recorded in pairs.jsonl)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute everything but do NOT write pairs.jsonl/snapshot.json",
    )
    return p


async def _async_main(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    snapshot, rows = await run_sampling(
        user_id=args.user_id,
        workspace_id=args.workspace_id,
        out_dir=out_dir,
        seed=args.seed,
        embedding_model_id=args.embedding_model,
    )

    _print_summary(snapshot, rows)

    if args.dry_run:
        print("\n[DRY-RUN] No files written.")
        return 0

    snapshot_path = out_dir / "snapshot.json"
    pairs_path = out_dir / "pairs.jsonl"

    with open(snapshot_path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2, ensure_ascii=False)
        f.write("\n")

    with open(pairs_path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(f"\nWrote {snapshot_path}")
    print(f"Wrote {pairs_path}")
    return 0


def main() -> int:
    args = _build_arg_parser().parse_args()
    return asyncio.run(_async_main(args))


if __name__ == "__main__":
    sys.exit(main())
