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

The flag is read again, fresh, right before the upsert, so a writer whose
embedding was in flight while the flag was turned off writes nothing; the
config update also purges after its commit and again when the flag is turned
back on, so a leftover from the remaining window never becomes visible.

A failed delete of the old chunk points is raised (the row is retried, the
old body never survives an edit). Any other failure is logged
(``content_chunks_failed``, error class only — never the text). By default it
is then fail-open: the summary point is already written and the memory stays
summary-only until its next re-embed. ``process_pending_embedding`` passes
``fail_open=False`` (#2049) so the failure is raised and the row is marked
failed and retried by the embedding sweep, never stamped ``success`` without
its chunks.
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
from utils.exceptions import MemoryCloudException
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


async def write_content_chunks(
    db: AsyncSession,
    memory: Memory,
    *,
    base_payload: dict[str, Any],
    embed_svc: Any,
    collection_name: str,
    chunks_enabled: bool | None = None,
    fail_open: bool = True,
) -> int:
    """Replace the memory's chunk points; return how many were written.

    A failed delete of the old chunk points is raised — the caller's normal
    failure path (``process_pending_embedding`` marks the row failed and
    retries it), so an edit never leaves the old body in ``chunk_text``. A
    failed flag lookup, embedding or upsert is logged, then returns 0 — or,
    with ``fail_open=False``, is raised (``_without_text``).

    Args:
        db: Session used for the flag lookups.
        memory: The memory row (its ``content`` is split).
        base_payload: The memory's summary-point payload (``build_memory_point``).
        embed_svc: The context's embedding service.
        collection_name: The context's routed collection.
        chunks_enabled: The context's flag when the caller already has it
            (a per-context batch); ``None`` looks it up. Either way the flag
            is read again, fresh, right before the upsert.
        fail_open: False re-raises a failed flag lookup, embedding or upsert
            after logging it (#2049), so the caller's failure path retries.

    Raises:
        Exception: a failed delete of the old chunk points; with
            ``fail_open=False`` also a failed flag lookup, embedding or upsert.
    """
    if _is_resource_row(memory):
        return 0
    try:
        if chunks_enabled is None:
            chunks_enabled = await content_chunks_enabled(db, memory.context_id)
    except Exception as e:
        _log_failure(memory, collection_name, e)
        if not fail_open:
            raise _without_text(e) from None
        return 0
    if not chunks_enabled or not chunk_points_supported():
        return 0
    await delete_chunk_points([memory.id], collection_name)
    chunks = split_content(memory.content)
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
        await upsert_chunk_points(
            points,
            user_id=memory.user_id,
            workspace_id=str(memory.workspace_id),
            context_id=str(memory.context_id),
            collection_name=collection_name,
        )
    except Exception as e:
        _log_failure(memory, collection_name, e)
        if not fail_open:
            raise _without_text(e) from None
        return 0
    logger.info(
        "content_chunks_written",
        memory_id=str(memory.id),
        collection=collection_name,
        chunks=len(points),
        chars=len(memory.content or ""),
    )
    return len(points)


class ContentChunkWriteError(RuntimeError):
    """A chunk write failed outside this project's own errors (#2049)."""


def _without_text(error: Exception) -> Exception:
    """The error to raise for a failed chunk write: never the chunk text.

    The caller stores and logs the message (``embedding_error``,
    ``embedding_failed``). The project's own errors keep their type — the
    retry budget is decided by it (``is_configuration_failure``) — and their
    message, which names no content. Anything else (a Qdrant or validation
    error can echo its input) becomes its class name only.
    """
    if isinstance(error, MemoryCloudException):
        return error
    return ContentChunkWriteError(f"Content chunk write failed ({type(error).__name__})")


def _log_failure(memory: Memory, collection_name: str, error: Exception) -> None:
    logger.warning(
        "content_chunks_failed",
        memory_id=str(memory.id),
        collection=collection_name,
        error_class=type(error).__name__,
    )


#: Chunk texts per ``embed_batch`` request (and points per upsert) in
#: :func:`rebuild_content_chunks` (a page of 64 long memories is up to 1,024
#: chunks; one request for all of them would exceed a provider's per-request
#: token limit).
CHUNK_EMBED_BATCH_SIZE = 64


async def rebuild_content_chunks(
    db: AsyncSession,
    rows: list[Memory],
    *,
    base_payloads: dict[Any, dict[str, Any]],
    embed_svc: Any,
    collection_name: str,
    context_id: Any,
) -> int:
    """Rebuild the chunk points of a page of one context's memories in ``collection_name``.

    The embedding migration's re-embed (#2015 PR 2a): its target collection
    is not routed yet, so nothing else writes there, and a page's chunks are
    embedded in batches across memories, not one memory at a time. The
    contract is :func:`write_content_chunks`'s: delete first (content that
    is now too short loses its chunks, chunks an earlier migration to the
    same model left are replaced), then write; the flag is read fresh before the delete
    and again right before the upsert, and each read's transaction is ended
    before the next embedding or vector store call. Unlike that writer it is
    not fail-open: a failed delete, embedding or upsert is logged and raised,
    so the migration stops and can be re-run (point ids are deterministic).

    Args:
        db: Session for the flag reads.
        rows: The page's memories (resource rows are skipped).
        base_payloads: Memory id -> its summary-point payload (``build_memory_point``).
        embed_svc: The target model's embedding service.
        collection_name: The target collection.
        context_id: The context of ``rows``.

    Returns:
        The number of chunk points written.
    """
    plain = [memory for memory in rows if not _is_resource_row(memory)]
    if not plain or not chunk_points_supported():
        return 0
    enabled = await content_chunks_enabled(db, context_id)
    await db.commit()
    if not enabled:
        logger.info("content_chunks_skipped_flag_off", context_id=str(context_id))
        return 0
    try:
        await delete_chunk_points([memory.id for memory in plain], collection_name)
    except Exception as e:
        _log_failure(plain[0], collection_name, e)
        raise

    # Every chunk of the page, grouped by user (embed_batch and the upsert
    # take one user_id), embedded CHUNK_EMBED_BATCH_SIZE texts per request.
    split = [(memory, split_content(memory.content)) for memory in plain]
    by_user: dict[str, list[tuple[Memory, int, str]]] = {}
    for memory, chunks in split:
        for index, text in enumerate(chunks):
            by_user.setdefault(memory.user_id, []).append((memory, index, text))
    if not by_user:
        return 0
    vectors: dict[tuple[Any, int], list[float]] = {}
    current: Memory | None = None
    try:
        for user_id, jobs in by_user.items():
            for start in range(0, len(jobs), CHUNK_EMBED_BATCH_SIZE):
                part = jobs[start : start + CHUNK_EMBED_BATCH_SIZE]
                current = part[0][0]
                got = await embed_svc.embed_batch(
                    [text for _, _, text in part],
                    user_id,
                    context_id=str(context_id),
                    workspace_id=str(current.workspace_id),
                )
                if len(got) != len(part):
                    raise RuntimeError(
                        f"embed_batch returned {len(got)} vectors for {len(part)} texts"
                    )
                for (memory, index, _), vector in zip(part, got, strict=True):
                    vectors[(memory.id, index)] = vector
        current = None
        # The flag may have been turned off (and every collection purged)
        # while the chunks were embedded: re-read it fresh.
        enabled = await content_chunks_enabled(db, context_id)
        await db.commit()
        if not enabled:
            logger.info("content_chunks_skipped_flag_off", context_id=str(context_id))
            return 0
        written = 0
        for user_id, jobs in by_user.items():
            for start in range(0, len(jobs), CHUNK_EMBED_BATCH_SIZE):
                part = jobs[start : start + CHUNK_EMBED_BATCH_SIZE]
                current = part[0][0]
                await upsert_chunk_points(
                    [
                        (
                            chunk_point_id(memory.id, index),
                            vectors[(memory.id, index)],
                            build_chunk_payload(
                                base_payloads[memory.id],
                                memory_id=memory.id,
                                chunk_index=index,
                                chunk_text=text,
                            ),
                        )
                        for memory, index, text in part
                    ],
                    user_id=user_id,
                    workspace_id=str(current.workspace_id),
                    context_id=str(context_id),
                    collection_name=collection_name,
                )
                written += len(part)
    except Exception as e:
        if current is not None:
            _log_failure(current, collection_name, e)
        raise
    logger.info(
        "content_chunks_rebuilt",
        context_id=str(context_id),
        collection=collection_name,
        memories=sum(1 for _, chunks in split if chunks),
        chunks=written,
    )
    return written
