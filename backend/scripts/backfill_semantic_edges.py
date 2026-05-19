#!/usr/bin/env python3
"""One-shot backfill of origin='semantic' edges from cosine similarity.

Issue #722. Recovers semantic graph structure lost to the Hebbian decay
loop before the origin discriminator was introduced. Idempotent via
ON CONFLICT DO NOTHING on the (user_id, src_id, dst_id) unique index.

Usage:
    python -m scripts.backfill_semantic_edges \\
        [--context-id <uuid>] \\
        [--min-memories 50] [--sim-threshold 0.7] [--top-k 10] [--dry-run]

If --context-id is omitted, every context with >= --min-memories alive
memories is backfilled.

Operational notes:
    * Commits per-context, so a failure mid-run loses at most one context's work.
    * The DB session is held open across Qdrant I/O for the full run; on very
      large contexts (>1000 memories) consider running with --context-id once
      per context instead of the all-contexts sweep.
    * Use --dry-run first to size the impact before committing.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE.parent))  # backend/  → enables `from src.X`
sys.path.insert(0, str(_HERE.parent / "src"))  # backend/src/ → enables `from X`

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from db.base import _get_session_factory  # noqa: E402
from models.memory import EDGE_ORIGIN_SEMANTIC, Memory  # noqa: E402
from repositories.neural_edge import NeuralEdgeRepository  # noqa: E402
from utils.logger import get_logger  # noqa: E402

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Qdrant neighbour protocol — decouples backfill_context() from the real
# Qdrant client so the tests can pass a simple MagicMock.
# ---------------------------------------------------------------------------


class QdrantNeighborClient(Protocol):
    """Minimal interface required by backfill_context()."""

    async def query_neighbors(
        self,
        memory_id: UUID,
        context_id: UUID,
        user_id: str,
        workspace_id: UUID,
        top_k: int,
    ) -> list[tuple[UUID, float]]:
        """Return up to top_k (neighbor_uuid, cosine_score) pairs for memory_id."""
        ...


# ---------------------------------------------------------------------------
# Real Qdrant adapter
# ---------------------------------------------------------------------------


class RealQdrantNeighborClient:
    """Wraps the real Qdrant client to satisfy the QdrantNeighborClient protocol.

    Strategy: retrieve the stored dense vector for memory_id via
    client.retrieve(with_vectors=True), then pass that vector as the query
    to search_memories_qdrant() with limit=top_k+1 (excluding the self-hit).
    """

    def __init__(self) -> None:
        from db.qdrant import (
            KAGURA_MEMORIES_COLLECTION,
            KAGURA_MEMORIES_VECTOR_NAME,
            get_qdrant_client,
            search_memories_qdrant,
        )

        self._client = get_qdrant_client()
        self._collection = KAGURA_MEMORIES_COLLECTION
        self._vector_name = KAGURA_MEMORIES_VECTOR_NAME
        self._search = search_memories_qdrant

    async def query_neighbors(
        self,
        memory_id: UUID,
        context_id: UUID,
        user_id: str,
        workspace_id: UUID,
        top_k: int,
    ) -> list[tuple[UUID, float]]:
        """Retrieve stored vector then find top-K neighbors in the same context."""
        points = await self._client.retrieve(
            collection_name=self._collection,
            ids=[str(memory_id)],
            with_vectors=True,
            with_payload=False,
        )
        if not points or points[0].vector is None:
            return []

        vector_obj = points[0].vector
        if isinstance(vector_obj, dict):
            dense_vec = vector_obj.get(self._vector_name, [])
        else:
            # Anonymous vector (shouldn't happen in this collection but be safe)
            dense_vec = list(vector_obj) if vector_obj else []

        if not dense_vec:
            return []

        results = await self._search(
            user_id=user_id,
            query_vector=dense_vec,
            workspace_id=str(workspace_id),
            context_id=str(context_id),
            limit=top_k + 1,  # +1 because the self-hit will be in results
        )

        out: list[tuple[UUID, float]] = []
        for hit in results:
            hit_id = UUID(str(hit["id"]))
            if hit_id == memory_id:
                continue
            out.append((hit_id, hit["score"]))
            if len(out) >= top_k:
                break
        return out


# ---------------------------------------------------------------------------
# Core backfill logic (pure — uses the protocol, not the real client)
# ---------------------------------------------------------------------------


async def backfill_context(
    db: AsyncSession,
    qdrant: QdrantNeighborClient,
    context_id: UUID,
    *,
    min_memories: int = 50,
    sim_threshold: float = 0.7,
    top_k: int = 10,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Backfill origin='semantic' edges for one context.

    Args:
        db: Async SQLAlchemy session.
        qdrant: QdrantNeighborClient exposing ``query_neighbors(memory_id,
            context_id, user_id, workspace_id, top_k) -> list[(UUID, float)]``.
        context_id: Target context UUID.
        min_memories: Skip context when alive memory count is below this floor.
        sim_threshold: Minimum cosine score for an edge to be inserted.
        top_k: Number of nearest neighbours to retrieve per memory.
        dry_run: If True, count pairs but do not write to DB.

    Returns:
        Dict with keys: skipped, reason (on skip), memory_count,
        pairs_evaluated, edges_inserted, edges_failed, pairs_would_insert, dry_run.
    """
    # ---- 1. Count alive memories in this context --------------------------
    count_q = await db.execute(
        select(func.count(Memory.id)).where(
            Memory.context_id == context_id,
            Memory.deleted_at.is_(None),
            Memory.workspace_id.isnot(None),
        )
    )
    memory_count: int = count_q.scalar_one()

    if memory_count < min_memories:
        return {
            "skipped": True,
            "reason": "below_memory_floor",
            "memory_count": memory_count,
        }

    # ---- 2. Load alive memories (need user_id + workspace_id per memory) --
    mem_q = await db.execute(
        select(Memory.id, Memory.user_id, Memory.workspace_id).where(
            Memory.context_id == context_id,
            Memory.deleted_at.is_(None),
            Memory.workspace_id.isnot(None),
        )
    )
    memories = mem_q.all()

    # ---- 3. Iterate memories, query neighbours, build deduplicated pairs ---
    edge_repo = NeuralEdgeRepository(db)
    seen_pairs: set[tuple[UUID, UUID]] = set()
    pairs_evaluated = 0
    edges_inserted = 0
    edges_failed = 0
    pairs_would_insert = 0

    for row in memories:
        mem_id: UUID = row.id
        user_id: str = row.user_id
        workspace_id: UUID = row.workspace_id

        try:
            neighbors = await qdrant.query_neighbors(
                mem_id, context_id, user_id, workspace_id, top_k
            )
        except Exception as exc:
            logger.warning(
                "backfill_semantic_qdrant_error",
                memory_id=str(mem_id),
                context_id=str(context_id),
                error=str(exc),
            )
            continue

        for neighbor_id, score in neighbors:
            if score < sim_threshold:
                continue

            # Canonical pair to deduplicate (A,B) vs (B,A)
            a, b = sorted([mem_id, neighbor_id], key=str)
            pair_key = (a, b)
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            pairs_evaluated += 1

            if dry_run:
                pairs_would_insert += 1
                continue

            try:
                async with db.begin_nested():
                    edge = await edge_repo.create_edge_if_absent(
                        user_id=user_id,
                        src_id=a,
                        dst_id=b,
                        edge_type="related_to",
                        weight=score,
                        confidence=1.0,
                        workspace_id=str(workspace_id),
                        context_id=str(context_id),
                        origin=EDGE_ORIGIN_SEMANTIC,
                    )
                if edge is not None:
                    edges_inserted += 1
            except Exception as exc:
                edges_failed += 1
                logger.warning(
                    "backfill_semantic_edge_error",
                    src_id=str(a),
                    dst_id=str(b),
                    context_id=str(context_id),
                    error=str(exc),
                )

    if not dry_run:
        await db.commit()

    logger.info(
        "backfill_context_done",
        context_id=str(context_id),
        memory_count=memory_count,
        pairs_evaluated=pairs_evaluated,
        edges_inserted=edges_inserted,
        edges_failed=edges_failed,
        pairs_would_insert=pairs_would_insert,
        dry_run=dry_run,
    )

    return {
        "skipped": False,
        "memory_count": memory_count,
        "pairs_evaluated": pairs_evaluated,
        "edges_inserted": edges_inserted,
        "edges_failed": edges_failed,
        "pairs_would_insert": pairs_would_insert,
        "dry_run": dry_run,
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


async def _main(args: argparse.Namespace) -> int:
    session_factory = _get_session_factory()
    qdrant = RealQdrantNeighborClient()

    total_inserted = 0
    total_failed = 0
    total_would_insert = 0
    total_contexts = 0
    skipped = 0

    async with session_factory() as db:
        if args.context_id:
            context_ids = [UUID(args.context_id)]
        else:
            # Discover all contexts with >= min_memories alive memories
            q = await db.execute(
                select(Memory.context_id, func.count(Memory.id).label("cnt"))
                .where(
                    Memory.deleted_at.is_(None),
                    Memory.workspace_id.isnot(None),
                    Memory.context_id.isnot(None),
                )
                .group_by(Memory.context_id)
                .having(func.count(Memory.id) >= args.min_memories)
            )
            context_ids = [UUID(str(row.context_id)) for row in q.all()]

        n_contexts = len(context_ids)
        for ctx_id in context_ids:
            result = await backfill_context(
                db,
                qdrant,
                ctx_id,
                min_memories=args.min_memories,
                sim_threshold=args.sim_threshold,
                top_k=args.top_k,
                dry_run=args.dry_run,
            )
            total_contexts += 1
            if result.get("skipped"):
                skipped += 1
            else:
                total_inserted += result.get("edges_inserted", 0)
                total_failed += result.get("edges_failed", 0)
                total_would_insert += result.get("pairs_would_insert", 0)

    mode = "DRY-RUN" if args.dry_run else "EXECUTE"
    print(f"\n=== semantic edge backfill ({mode}) ===")
    print(f"  Contexts processed:  {total_contexts}")
    print(f"  Contexts skipped:    {skipped}")
    if args.dry_run:
        print(
            f"  Dry run: {total_would_insert} edges would be inserted across {n_contexts} contexts"
        )
    else:
        print(
            f"  Inserted {total_inserted} edges ({total_failed} failures) across {n_contexts} contexts"
        )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill origin='semantic' edges from cosine similarity (Issue #722)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--context-id",
        help="Restrict to one context UUID; omit to backfill all eligible contexts.",
    )
    parser.add_argument(
        "--min-memories",
        type=int,
        default=50,
        help="Skip contexts with fewer alive memories than this (default: 50).",
    )
    parser.add_argument(
        "--sim-threshold",
        type=float,
        default=0.7,
        help="Minimum cosine similarity to create an edge (default: 0.7).",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=10,
        help="Nearest neighbours to query per memory (default: 10).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log counts but do not insert edges.",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(_main(args)))


if __name__ == "__main__":
    main()
