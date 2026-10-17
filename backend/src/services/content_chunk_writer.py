"""Write a memory's content chunk points (#2015).

Called after a memory's summary point is upserted (``process_pending_embedding``,
the Sleep reindex and the Sleep undo). The invariant the rest of the system
relies on: **a context with ``content_chunks_enabled`` off has no chunk
points** — so a chunk point never holds body text the memory no longer has,
and callers on such a context can skip the chunk cascade.

- Flag off: nothing is read from or written to the vector store (turning
  the flag off purges the context's chunks, see
  ``purge_context_chunk_points``).
- Flag on: the memory's chunk points are always deleted first, then
  rewritten from the current content — content that shrank below the split
  threshold therefore just loses its chunks.

Fail-open: the summary point is already written, so a failure here is logged
(``content_chunks_failed``, error class only — never the text) and the memory
stays a summary-only memory until its next re-embed.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.qdrant import chunk_points_supported, delete_chunk_points, upsert_chunk_points
from models.config import ContextSearchConfig
from models.memory import Memory
from utils.content_chunks import build_chunk_payload, chunk_point_id, split_content
from utils.logger import get_logger

logger = get_logger(__name__)


async def content_chunks_enabled(db: AsyncSession, context_id: Any) -> bool:
    """Whether the context opted into content chunk vectors."""
    if context_id is None:
        return False
    result = await db.execute(
        select(ContextSearchConfig.content_chunks_enabled).where(
            ContextSearchConfig.context_id == context_id
        )
    )
    return bool(result.scalar_one_or_none())


async def write_content_chunks(
    db: AsyncSession,
    memory: Memory,
    *,
    base_payload: dict[str, Any],
    embed_svc: Any,
    collection_name: str,
    chunks_enabled: bool | None = None,
) -> int:
    """Replace the memory's chunk points; return how many were written.

    Args:
        db: Session used for the flag lookup.
        memory: The memory row (its ``content`` is split).
        base_payload: The memory's summary-point payload (``build_memory_point``).
        embed_svc: The context's embedding service.
        collection_name: The context's routed collection.
        chunks_enabled: The context's flag when the caller already has it
            (a per-context batch); ``None`` looks it up.
    """
    points: list[tuple[str, list[float], dict[str, Any]]] = []
    try:
        if chunks_enabled is None:
            chunks_enabled = await content_chunks_enabled(db, memory.context_id)
        if not chunks_enabled or not chunk_points_supported():
            return 0
        await delete_chunk_points([memory.id], collection_name)
        chunks = split_content(memory.content)
        if not chunks:
            return 0
        vectors = await embed_svc.embed_batch(
            chunks,
            memory.user_id,
            context_id=memory.context_id,
            workspace_id=memory.workspace_id,
        )
        points = [
            (
                chunk_point_id(memory.id, index),
                vector,
                build_chunk_payload(
                    base_payload, memory_id=memory.id, chunk_index=index, chunk_text=text
                ),
            )
            for index, (text, vector) in enumerate(zip(chunks, vectors, strict=True))
        ]
        await upsert_chunk_points(
            points,
            user_id=memory.user_id,
            workspace_id=str(memory.workspace_id),
            context_id=str(memory.context_id),
            collection_name=collection_name,
        )
    except Exception as e:
        logger.warning(
            "content_chunks_failed",
            memory_id=str(memory.id),
            collection=collection_name,
            error_class=type(e).__name__,
        )
        return 0
    logger.info(
        "content_chunks_written",
        memory_id=str(memory.id),
        collection=collection_name,
        chunks=len(points),
        chars=len(memory.content or ""),
    )
    return len(points)
