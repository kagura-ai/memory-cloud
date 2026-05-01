"""Stage [C]: Pull memory rows + their existing Qdrant embeddings.

Re-uses the embeddings already computed during memory ingestion
(``services/embedding_service.EmbeddingService``); the analysis
pipeline never re-embeds. Re-embedding 8000 memories at run time
would blow the cost budget by ~10x and the wall-clock budget
several-fold, and the vectors are already authoritative — the
indexed scroll is just a faster way to read them than the SQL
``memories.embedding`` (when present).

What this stage produces, in dependency order:

1. **Memory rows** matching ``params.filters``: ``from`` / ``to``
   on ``created_at``, optional ``types`` allow-list, optional
   ``tags`` (ANY-match), optional ``min_importance``, optional
   ``query`` (ignored at this stage; v1 spec defers query-shaped
   pre-filtering to the API layer in #496).

2. **Embedding-model homogeneity check**: every memory in the
   result set MUST have been embedded with the same model. The
   context's ``ContextSearchConfig.embedding_model`` is the
   declared model, but a context that recently switched models
   may have legacy memories on the old model still resident in
   the same Qdrant collection. We surface ``EmbeddingMismatchError``
   (422) in that case so the API caller can either re-run after a
   reindex or scope the analysis to a date range that uses one
   model.

3. **Vector matrix**: shape (n, embedding_dim), float32. The
   batch-of-1000 scroll keeps the heap bounded for n=50000 runs.

The stage does NOT load Layer 3 (full memory body) — only the
``memories`` row's metadata + the Qdrant vector. Layer 3 is fetched
on demand by ``get_cluster`` MCP tool in #496.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import numpy as np
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.qdrant import (
    KAGURA_MEMORIES_COLLECTION,
    KAGURA_MEMORIES_VECTOR_NAME,
    get_collection_name,
    get_qdrant_client,
)
from models.config import ContextSearchConfig
from models.memory import Memory
from utils.exceptions import ValidationError
from utils.logger import get_logger

logger = get_logger(__name__)

# Qdrant scroll batch size — issue #495 spec calls for batch=1000.
# Larger batches consume more memory; smaller batches make more
# round trips. 1000 is a good middle ground for the 8000-memory
# canonical run (8 round trips).
_QDRANT_SCROLL_BATCH = 1000


class EmbeddingMismatchError(ValidationError):
    """Memories in the result set use multiple embedding models (422).

    The pipeline cannot cluster across models because the embedding
    spaces are not aligned (cosine similarity between Voyage and
    OpenAI vectors is meaningless). The API layer (#496) returns
    422 with the offending model list so the user can either
    refilter or trigger a reindex.
    """

    def __init__(self, embedding_models: list[str]) -> None:
        super().__init__(
            message=(
                "Memories in scope use multiple embedding models "
                f"({embedding_models}); broadlistening cannot cluster "
                "across models. Filter to a single model or reindex."
            ),
            field="embedding_models",
            embedding_models=embedding_models,
        )


@dataclass(frozen=True)
class MemoryRecord:
    """Subset of memory fields the analysis pipeline needs."""

    id: UUID
    type: str
    summary: str
    tags: list[str]
    importance: float
    created_at: datetime


@dataclass(frozen=True)
class VectorPullResult:
    """Output of Stage [C].

    Attributes:
        memories: Ordered memory metadata, len == n.
        embeddings: 2D matrix (n, embedding_dim), float32. Row i
            corresponds to ``memories[i]``.
        embedding_model: The single model name common to all rows.
        embedding_dim: ``embeddings.shape[1]``.
    """

    memories: list[MemoryRecord]
    embeddings: np.ndarray
    embedding_model: str
    embedding_dim: int


async def pull_memories_with_vectors(
    db: AsyncSession,
    *,
    workspace_id: UUID,
    context_id: UUID,
    from_dt: datetime | None = None,
    to_dt: datetime | None = None,
    types: list[str] | None = None,
    tags: list[str] | None = None,
    min_importance: float | None = None,
) -> VectorPullResult:
    """Pull memories matching filters + their Qdrant vectors.

    Args:
        db: AsyncSession bound to the request transaction.
        workspace_id: Required isolation parameter.
        context_id: Required isolation parameter.
        from_dt: Optional ``created_at`` lower bound (inclusive).
        to_dt: Optional ``created_at`` upper bound (exclusive).
        types: Optional allow-list of ``memory.type`` values.
        tags: Optional ANY-match tag filter.
        min_importance: Optional ``importance >= x`` filter.

    Returns:
        ``VectorPullResult`` with aligned memory metadata and
        embedding matrix.

    Raises:
        EmbeddingMismatchError: Multiple embedding models present.
        ValueError: Empty result set (caller should surface 422 or
            return early).
    """
    # 1. Resolve the canonical embedding model for this context. The
    #    context-search-config row is the single source of truth; if
    #    it is absent we fall back to the default collection (and the
    #    homogeneity check will surface any discrepancy at scroll time).
    config_stmt = select(ContextSearchConfig).where(ContextSearchConfig.context_id == context_id)
    config_row = (await db.execute(config_stmt)).scalar_one_or_none()
    declared_model = config_row.embedding_model if config_row else None
    declared_dim = config_row.embedding_dimensions if config_row else None
    collection_name = (
        get_collection_name(declared_model, declared_dim)
        if (declared_model and declared_dim)
        else KAGURA_MEMORIES_COLLECTION
    )

    # 2. Pull metadata rows from the SQL side. The DB is authoritative
    #    for filter semantics (importance threshold, tag ANY-match,
    #    type allow-list, date range). Qdrant only provides vectors.
    conditions = [
        Memory.workspace_id == workspace_id,
        Memory.context_id == context_id,
        Memory.deleted_at.is_(None),
    ]
    if from_dt is not None:
        conditions.append(Memory.created_at >= from_dt)
    if to_dt is not None:
        conditions.append(Memory.created_at < to_dt)
    if types:
        conditions.append(Memory.type.in_(types))
    if min_importance is not None:
        conditions.append(Memory.importance >= min_importance)
    if tags:
        # ANY-match: at least one tag in ``tags`` is present in the
        # row's tag array. Postgres array overlap operator (&&).
        conditions.append(Memory.tags.op("&&")(tags))

    stmt = select(Memory).where(and_(*conditions)).order_by(Memory.created_at)
    memories: list[Memory] = list((await db.execute(stmt)).scalars().all())

    if not memories:
        raise ValueError(
            "No memories matched the analysis filters; refusing to start an "
            "empty run. The API layer should pre-flight this and surface 422."
        )

    # 3. Pull vectors from Qdrant in parallel batches. ``retrieve``
    #    (point-id lookup) is preferred over ``scroll`` because we
    #    already know the exact IDs. Independent batches are gathered
    #    concurrently — at n=8000 this is 8 round-trips that previously
    #    serialized into ~400-800ms of network latency.
    client = get_qdrant_client()
    memory_ids_str = [str(m.id) for m in memories]
    batches = [
        memory_ids_str[i : i + _QDRANT_SCROLL_BATCH]
        for i in range(0, len(memory_ids_str), _QDRANT_SCROLL_BATCH)
    ]
    batch_results = await asyncio.gather(
        *(
            client.retrieve(
                collection_name=collection_name,
                ids=batch_ids,
                with_vectors=[KAGURA_MEMORIES_VECTOR_NAME],
                with_payload=["embedding_model"],
            )
            for batch_ids in batches
        )
    )

    vector_by_id: dict[str, np.ndarray] = {}
    seen_models: set[str] = set()
    for points in batch_results:
        for p in points:
            payload_model = (p.payload or {}).get("embedding_model") or declared_model
            if payload_model:
                seen_models.add(str(payload_model))
            vec_dict = p.vector if isinstance(p.vector, dict) else {}
            vec = vec_dict.get(KAGURA_MEMORIES_VECTOR_NAME)
            if vec is not None:
                vector_by_id[str(p.id)] = np.asarray(vec, dtype=np.float32)

    if len(seen_models) > 1:
        raise EmbeddingMismatchError(sorted(seen_models))

    # 4. Build aligned outputs. Drop memories that have no Qdrant
    #    vector (rare — would mean a half-indexed memory). Log at
    #    warn so observability catches the divergence.
    aligned_memories: list[MemoryRecord] = []
    aligned_vectors: list[np.ndarray] = []
    missing = 0
    for m in memories:
        v = vector_by_id.get(str(m.id))
        if v is None:
            missing += 1
            continue
        aligned_memories.append(
            MemoryRecord(
                id=m.id,
                type=m.type,
                summary=m.summary or "",
                tags=list(m.tags or []),
                importance=float(m.importance or 0.0),
                created_at=m.created_at,
            )
        )
        aligned_vectors.append(v)

    if missing:
        logger.warning(
            "analysis_vector_pull_missing_vectors",
            missing=missing,
            total=len(memories),
            collection=collection_name,
        )

    if not aligned_memories:
        raise ValueError(f"No memories had matching Qdrant vectors in {collection_name!r}.")

    embeddings = np.vstack(aligned_vectors)
    final_model = next(iter(seen_models)) if seen_models else (declared_model or "unknown")
    return VectorPullResult(
        memories=aligned_memories,
        embeddings=embeddings,
        embedding_model=final_model,
        embedding_dim=int(embeddings.shape[1]),
    )
