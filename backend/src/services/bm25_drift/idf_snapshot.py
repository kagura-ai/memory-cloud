"""Snapshot the BM25 IDF state of a context's slice of a Qdrant collection.

Issue #343: Reads sparse vectors back from Qdrant via scroll, classifying
each point as memory-source vs resource-source by the presence of the
`resource_id` payload field, and accumulates per-token document
frequencies. The output feeds psi_calculator.compute_psi.

Per #334 the kagura_memories collection is shared across contexts and
isolation is achieved at the payload-filter level — both filters here
include `context_id` so the snapshot is scoped correctly.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from uuid import UUID

from qdrant_client import AsyncQdrantClient
from qdrant_client.http.models import FieldCondition, Filter, MatchValue

from db.qdrant import KAGURA_MEMORIES_BM25_VECTOR_NAME
from utils.logger import get_logger

logger = get_logger(__name__)

# Scroll batch size. Mirrors the value used by admin context-recovery
# (backend/src/api/routes/admin.py) and check_orphaned_qdrant_points.
_SCROLL_LIMIT = 100


@dataclass(frozen=True)
class IdfSnapshot:
    """Per-token document frequency snapshot for one (collection, context)."""

    df_memory: dict[int, int]
    df_global: dict[int, int]
    m_memory: int
    r_resource: int

    @property
    def n_global(self) -> int:
        return self.m_memory + self.r_resource


async def build_idf_snapshot(
    client: AsyncQdrantClient,
    collection_name: str,
    context_id: UUID,
) -> IdfSnapshot:
    """Scroll the context's slice of `collection_name` and accumulate df.

    Each point's bm25 sparse vector is fetched (with_vectors=["bm25"]) and
    its indices counted as documents-containing-token. Source classification
    is by `resource_id` payload presence: memory-side writes never set
    this field, resource-side writes always set it (see
    services/resource_indexer._apply_upsert).
    """
    df_memory: dict[int, int] = defaultdict(int)
    df_global: dict[int, int] = defaultdict(int)
    m_memory = 0
    r_resource = 0

    scroll_filter = Filter(
        must=[
            FieldCondition(
                key="context_id",
                match=MatchValue(value=str(context_id)),
            )
        ]
    )

    offset = None
    while True:
        points, next_offset = await client.scroll(
            collection_name=collection_name,
            scroll_filter=scroll_filter,
            limit=_SCROLL_LIMIT,
            offset=offset,
            with_payload=["resource_id"],
            with_vectors=[KAGURA_MEMORIES_BM25_VECTOR_NAME],
        )

        for point in points:
            # Source classification by payload-field presence.
            payload = point.payload or {}
            is_resource = payload.get("resource_id") is not None
            if is_resource:
                r_resource += 1
            else:
                m_memory += 1

            # Defensive: a point may have no bm25 vector (e.g. legacy rows
            # written before #335 / #345 wired the dual emit). Skip cleanly
            # rather than corrupting df counts.
            vector = point.vector
            if not isinstance(vector, dict):
                continue
            sparse = vector.get(KAGURA_MEMORIES_BM25_VECTOR_NAME)
            if sparse is None:
                continue

            indices = getattr(sparse, "indices", None)
            if not indices:
                continue

            # Each unique index counts once per document for df. The Qdrant
            # client returns indices as a list of int (not necessarily
            # de-duplicated), so we set() before counting.
            unique = set(indices)
            for idx in unique:
                df_global[idx] += 1
                if not is_resource:
                    df_memory[idx] += 1

        if next_offset is None:
            break
        offset = next_offset

    logger.debug(
        "bm25_idf_snapshot_built",
        context_id=str(context_id),
        collection=collection_name,
        m_memory=m_memory,
        r_resource=r_resource,
        unique_terms=len(df_global),
    )

    return IdfSnapshot(
        df_memory=dict(df_memory),
        df_global=dict(df_global),
        m_memory=m_memory,
        r_resource=r_resource,
    )
