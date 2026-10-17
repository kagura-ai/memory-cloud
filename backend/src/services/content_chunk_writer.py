"""Write a memory's content chunk points (#2015).

Called by ``process_pending_embedding`` after the memory's summary point is
upserted. Only a context with ``ContextSearchConfig.content_chunks_enabled``
gets chunk points, and only for content long enough to split
(``utils.content_chunks.split_content``). Every write first deletes the
memory's existing chunk points, so a re-embed after a content change (or of a
memory whose context turned the flag off) leaves no stale chunks.

Fail-open: the summary point is already written, so any failure here is
logged (``content_chunks_failed``) and the memory stays a summary-only memory.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.qdrant import delete_chunk_points, upsert_chunk_points
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
) -> int:
    """Replace the memory's chunk points; return how many were written.

    ``base_payload`` is the memory's summary-point payload (from
    ``build_memory_point``). Content too short to split returns before any
    query or Qdrant call, so the common short memory costs nothing. Known
    limit: a memory whose content shrinks below the split threshold keeps its
    old chunk points until it is deleted or rewritten long again (they still
    collapse to the same memory, and payload updates still reach them).
    """
    chunks = split_content(memory.content)
    if not chunks:
        return 0
    try:
        if not await content_chunks_enabled(db, memory.context_id):
            return 0
        await delete_chunk_points([memory.id], collection_name)
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
            error=str(e)[:200],
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
