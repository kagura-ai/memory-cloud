"""Write a memory's content chunk points (#2015).

Called after a memory's summary point is upserted (``process_pending_embedding``,
the Sleep reindex and the Sleep undo). The invariant the rest of the system
relies on: **a context with ``content_chunks_enabled`` off has no chunk
points** — so a chunk point never holds body text the memory no longer has,
and callers on such a context can skip the chunk cascade.

- Flag off: nothing is read from or written to the vector store (turning
  the flag off purges the context's chunks, see
  ``purge_context_chunk_points``).
- Flag on: the memory's chunk points are deleted first, then rewritten from
  the current content — content that shrank below the split threshold
  therefore just loses its chunks. A superseded run (below) does neither.

The flag is read again, fresh, right before the upsert, so a writer whose
embedding was in flight while the flag was turned off writes nothing; the
config update also purges after its commit and again when the flag is turned
back on, so a leftover from the remaining window never becomes visible.

Two overlapping re-embeds of one memory write the same chunk ids (#2041). So
the row is read again before the delete and again right before the upsert: a
run whose memory was deleted, edited (its content changed or it was re-queued
``pending``) or claimed by a newer embedding run (``embedding_attempted_at``
moved) stops there — the newer write owns the chunks. The first check keeps a
stale run from deleting chunks a newer run already wrote; the second catches
an edit that landed while the chunks were embedded. ``updated_at`` is not
used: metadata-only changes move it without a re-embed, so skipping on it
would leave the memory without chunks. What remains is the gap between the
second check and the upsert (one round trip): a newer run that deletes inside
it can still be overwritten by this run's upsert. A content hash or version
in the chunk payload would close it. The summary point has the same race and
is not guarded here.

A failed delete of the old chunk points is raised (the row is retried, the
old body never survives an edit). Any other failure is fail-open: the summary
point is already written, so it is logged (``content_chunks_failed``, error
class only — never the text) and the memory stays summary-only until its
next re-embed.
"""

from __future__ import annotations

import time
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
    return result.scalar_one_or_none() is True


# Process-local cache of the per-context flag for the hot metadata-update
# path (#2015): {context_id: (enabled, expires_at_monotonic)}.
_FLAG_CACHE: dict[str, tuple[bool, float]] = {}
_FLAG_CACHE_TTL_SECONDS = 30.0


async def chunk_cascade_needed(db: AsyncSession, context_id: Any) -> bool:
    """Whether a memory metadata update must also reach chunk points (#2015).

    Under the invariant "flag off ⇒ no chunk points" a context with the flag
    off has nothing to cascade to, so the extra Qdrant calls are skipped. The
    flag is cached per process for ``_FLAG_CACHE_TTL_SECONDS``; a lookup that
    fails answers True (cascade), the safe side. Writers of isolation fields
    (the creator transfer CLI) do not use this and always cascade.
    """
    if context_id is None:
        return False
    key = str(context_id)
    now = time.monotonic()
    cached = _FLAG_CACHE.get(key)
    if cached is not None and cached[1] > now:
        return cached[0]
    try:
        enabled = await content_chunks_enabled(db, context_id)
    except Exception:
        return True
    _FLAG_CACHE[key] = (enabled, now + _FLAG_CACHE_TTL_SECONDS)
    return enabled


def forget_cached_chunk_flag(context_id: Any) -> None:
    """Drop the cached flag of a context (its config just changed here)."""
    _FLAG_CACHE.pop(str(context_id), None)


def _is_resource_row(memory: Memory) -> bool:
    """A row the resource indexer owns: its point is the indexer's, never chunked."""
    if getattr(memory, "resource_id", None) is None:
        return False
    from services.resource_indexer import owns_resource_point

    return owns_resource_point(memory)


async def _superseded(
    db: AsyncSession, memory_id: Any, content: str | None, claimed_at: Any
) -> bool:
    """Whether a newer write owns the memory's chunks now (#2041).

    True when the row is gone or soft-deleted, its content is no longer what
    this run split, it was re-queued (``pending``: an edit, picked up by the
    embedding sweep) or a newer run claimed it (``embedding_attempted_at`` is
    stamped on every claim). One lookup by primary key, inside a SAVEPOINT:
    on Postgres a failed statement aborts the whole transaction, so without it
    a failed check would also fail the later checks (the run would delete the
    old chunks and write nothing) and leave the caller's session unusable.
    """
    async with db.begin_nested():
        result = await db.execute(
            select(Memory.id).where(
                Memory.id == memory_id,
                Memory.deleted_at.is_(None),
                Memory.content == content,
                Memory.embedding_status != "pending",
                Memory.embedding_attempted_at.is_not_distinct_from(claimed_at),
            )
        )
    return result.scalar_one_or_none() is None


async def _superseded_before_delete(
    db: AsyncSession, memory: Memory, content: str | None, claimed_at: Any
) -> bool:
    """The first superseded check (#2041); a failed lookup does not stop the run.

    The check right before the upsert still runs; a failure there writes nothing.
    """
    try:
        superseded = await _superseded(db, memory.id, content, claimed_at)
    except Exception as e:
        logger.warning(
            "content_chunks_superseded_check_failed",
            memory_id=str(memory.id),
            error_class=type(e).__name__,
        )
        return False
    if superseded:
        logger.info(
            "content_chunks_skipped_superseded",
            memory_id=str(memory.id),
            stage="before_delete",
        )
    return superseded


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

    A failed delete of the old chunk points is raised — the caller's normal
    failure path (``process_pending_embedding`` marks the row failed and
    retries it), so an edit never leaves the old body in ``chunk_text``. A
    failed flag lookup, embedding or upsert is logged and returns 0.

    Args:
        db: Session used for the flag lookups.
        memory: The memory row (its ``content`` is split).
        base_payload: The memory's summary-point payload (``build_memory_point``).
        embed_svc: The context's embedding service.
        collection_name: The context's routed collection.
        chunks_enabled: The context's flag when the caller already has it
            (a per-context batch); ``None`` looks it up. Either way the flag
            is read again, fresh, right before the upsert.
    """
    if _is_resource_row(memory):
        return 0
    # #2041: what this run embeds, taken before anything is awaited.
    content = memory.content
    claimed_at = getattr(memory, "embedding_attempted_at", None)
    try:
        if chunks_enabled is None:
            chunks_enabled = await content_chunks_enabled(db, memory.context_id)
    except Exception as e:
        _log_failure(memory, collection_name, e)
        return 0
    if not chunks_enabled or not chunk_points_supported():
        return 0
    if await _superseded_before_delete(db, memory, content, claimed_at):
        return 0
    await delete_chunk_points([memory.id], collection_name)
    chunks = split_content(content)
    if not chunks:
        return 0
    points: list[tuple[str, list[float], dict[str, Any]]] = []
    try:
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
        # The flag may have been turned off (and the context purged) while
        # the chunks were embedded: re-read it fresh, never from a cache.
        if not await content_chunks_enabled(db, memory.context_id):
            logger.info("content_chunks_skipped_flag_off", memory_id=str(memory.id))
            return 0
        # #2041: an edit or a newer embedding run landed while the chunks were
        # embedded; that run writes the chunks of the current content.
        if await _superseded(db, memory.id, content, claimed_at):
            logger.info(
                "content_chunks_skipped_superseded",
                memory_id=str(memory.id),
                stage="before_upsert",
            )
            return 0
        await upsert_chunk_points(
            points,
            user_id=memory.user_id,
            workspace_id=str(memory.workspace_id),
            context_id=str(memory.context_id),
            collection_name=collection_name,
        )
    except Exception as e:
        _log_failure(memory, collection_name, e)
        return 0
    logger.info(
        "content_chunks_written",
        memory_id=str(memory.id),
        collection=collection_name,
        chunks=len(points),
        chars=len(content or ""),
    )
    return len(points)


def _log_failure(memory: Memory, collection_name: str, error: Exception) -> None:
    logger.warning(
        "content_chunks_failed",
        memory_id=str(memory.id),
        collection=collection_name,
        error_class=type(error).__name__,
    )
