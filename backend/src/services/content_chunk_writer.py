"""Write a memory's content chunk points (#2015).

Called after a memory's summary point is upserted (``process_pending_embedding``,
the Sleep reindex and the Sleep undo). The invariant the rest of the system
relies on: **a context with ``content_chunks_enabled`` off has no chunk
points** — so a chunk point never holds body text the memory no longer has,
and callers on such a context can skip the chunk cascade.

- Flag off: nothing is read from or written to the vector store (turning
  the flag off purges the context's chunks, see
  ``purge_context_chunk_points``).
- Flag on: the chunks of the current content are upserted over the old ones
  (chunk ids are deterministic per index), then the old chunks past the new
  count are deleted (#2061) — content that shrank below the split threshold
  loses them all. A superseded run (below) does neither.

The flag is read again, fresh, right before the upsert, so a writer whose
embedding was in flight while the flag was turned off writes nothing; the
config update also purges after its commit and again when the flag is turned
back on, so a leftover from the remaining window never becomes visible.

Two overlapping re-embeds of one memory write the same chunk ids (#2041). So
the row is read again before anything is embedded and again right before the
upsert: a
run whose memory was deleted, edited (its content changed or it was re-queued
``pending``) or, for an embedding run, claimed by a newer run (the stamp its
claim holds moved; ``services.embedding_claim``) stops there — the newer
write owns the chunks. The Sleep paths hold no claim and compare no stamp.
The first check keeps a stale run from paying for an embedding and from
writing over chunks a newer run already wrote; the second catches an edit
that landed while the chunks were embedded. ``updated_at`` is not
used: metadata-only changes move it without a re-embed, so skipping on it
would leave the memory without chunks. What remains is the gap between the
second check and the upsert and trim (two round trips): a newer run that
writes inside it can still be overwritten by this run's upsert. Comparing the
``content_hash`` every chunk point now carries would close it; nothing
compares it yet. The summary point has the same race and is not guarded here.

Every failure is logged (``content_chunks_failed``, error class only — never
the text). One before the upsert leaves the old chunks as they were. By
default it is then fail-open: the summary point is already written and the
memory keeps whatever chunks it had until its next re-embed. ``process_pending_embedding`` passes
``fail_open=False`` (#2049) so the failure is raised and the row is marked
failed and retried by the embedding sweep, never stamped ``success`` without
its chunks. Its outcome write is fenced on the claim's own
``embedding_attempted_at``, so a run superseded here (it returns 0, like a
no-op) cannot stamp the newer run's claim either.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.qdrant import (
    chunk_points_supported,
    delete_chunk_points,
    delete_chunk_points_from,
    sync_chunk_points_payload,
    upsert_chunk_points,
)
from models.config import ContextSearchConfig
from models.memory import Memory
from utils.content_chunks import (
    build_chunk_payload,
    chunk_payload_updates,
    chunk_point_id,
    content_hash,
    split_content,
)
from utils.exceptions import MemoryCloudException
from utils.logger import get_logger

if TYPE_CHECKING:
    from services.embedding_claim import EmbeddingClaim

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
    db: AsyncSession,
    memory_id: Any,
    content: str | None,
    claimed_at: Any,
    *,
    check_claim: bool = True,
) -> bool:
    """Whether a newer write owns the memory's chunks now (#2041).

    True when the row is gone or soft-deleted, its content is no longer what
    this run split, it was re-queued (``pending``: an edit, picked up by the
    embedding sweep) or — with ``check_claim`` — a newer run claimed it
    (``embedding_attempted_at`` is no longer ``claimed_at``, the stamp this
    run's claim holds). One lookup by primary key, inside a SAVEPOINT:
    on Postgres a failed statement aborts the whole transaction, so without it
    a failed check would also fail the later checks (the run would delete the
    old chunks and write nothing) and leave the caller's session unusable.
    """
    conditions = [
        Memory.id == memory_id,
        Memory.deleted_at.is_(None),
        Memory.content == content,
        Memory.embedding_status != "pending",
    ]
    if check_claim:
        conditions.append(Memory.embedding_attempted_at.is_not_distinct_from(claimed_at))
    async with db.begin_nested():
        result = await db.execute(select(Memory.id).where(*conditions))
    return result.scalar_one_or_none() is None


async def _check_superseded(
    db: AsyncSession, memory_id: Any, content: str | None, claim: EmbeddingClaim | None
) -> bool:
    """``_superseded`` against the run's own claim, its renewals held off.

    A caller without a claim (the Sleep reindex and undo) compares no stamp: a
    live embedding run renews its stamp while it works, so a stamp loaded
    earlier would read as a newer claim. The content check still applies.
    """
    if claim is None:
        return await _superseded(db, memory_id, content, None, check_claim=False)
    async with claim.held() as stamp:
        return await _superseded(db, memory_id, content, stamp)


async def _superseded_before_write(
    db: AsyncSession, memory: Memory, content: str | None, claim: EmbeddingClaim | None
) -> bool:
    """The first superseded check (#2041); a failed lookup does not stop the run.

    It runs before anything is embedded or written, so a stale run pays for no
    embedding and touches no chunk. The check right before the upsert still
    runs; a failure there writes nothing.
    """
    try:
        superseded = await _check_superseded(db, memory.id, content, claim)
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
            stage="before_write",
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
    fail_open: bool = True,
    claim: EmbeddingClaim | None = None,
    usage: dict[str, int] | None = None,
) -> int:
    """Replace the memory's chunk points in place; return how many were written.

    The new chunks are upserted over the old ones (same ids per index), then
    the old chunks past the new count are deleted; content too short to split
    deletes them all. A failed flag lookup, embedding, upsert or delete is
    logged, then returns 0 — or, with ``fail_open=False``, is raised
    (``_without_text``) so the caller can retry the memory. A failure before
    the upsert leaves the old chunks as they were until that retry rewrites
    them; a caller passing ``fail_open=False`` never records the memory as
    embedded with them.

    Args:
        db: Session used for the flag lookups.
        memory: The memory row (its ``content`` is split).
        base_payload: The memory's summary-point payload (``build_memory_point``).
        embed_svc: The context's embedding service.
        collection_name: The context's routed collection.
        chunks_enabled: The context's flag when the caller already has it
            (a per-context batch); ``None`` looks it up. Either way the flag
            is read again, fresh, right before the upsert.
        fail_open: False re-raises a failed flag lookup, embedding, upsert or
            delete after logging it (#2049), so the caller's failure path
            retries.
        claim: The embedding run's own claim (#2041); its stamp, renewed
            while the run works, is what the superseded checks compare.
            ``None`` (the Sleep paths) compares no stamp, only the content,
            the soft delete and a re-queue.
        usage: Optional out-parameter, filled by ``embed_batch`` (``cached``,
            ``requests``, ``tokens``) when the chunks were embedded — set even
            when the write then fails, since the provider has billed them.
            The Sleep reindex adds it to its embedding accounting.

    Raises:
        Exception: with ``fail_open=False``, a failed flag lookup, embedding,
            upsert or delete.
    """
    if _is_resource_row(memory):
        return 0
    # #2041: what this run embeds, taken before anything is awaited.
    content = memory.content
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
    if await _superseded_before_write(db, memory, content, claim):
        return 0
    chunks = split_content(content)
    points: list[tuple[str, list[float], dict[str, Any]]] = []
    if usage is None:
        usage = {}
    try:
        if not chunks:
            # Too short to split (it may have shrunk below the threshold): the
            # memory keeps no chunk points, so removed body text never
            # survives in ``chunk_text``.
            await delete_chunk_points([memory.id], collection_name)
            return 0
        digest = content_hash(content)
        vectors = await embed_svc.embed_batch(
            chunks,
            memory.user_id,
            context_id=memory.context_id,
            workspace_id=memory.workspace_id,
            usage=usage,
        )
        points = [
            (
                chunk_point_id(memory.id, index),
                vector,
                build_chunk_payload(
                    base_payload,
                    memory_id=memory.id,
                    chunk_index=index,
                    chunk_text=text,
                    content_hash=digest,
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
        if await _check_superseded(db, memory.id, content, claim):
            logger.info(
                "content_chunks_skipped_superseded",
                memory_id=str(memory.id),
                stage="before_upsert",
            )
            return 0
        # #2061: upsert first, then trim. The ids are deterministic per index,
        # so the upsert replaces chunks 0..n-1 in place and the trim deletes
        # only the old ones past n: the memory is never left without chunks
        # while the new ones are embedded, and a failed embedding keeps the
        # old ones.
        await upsert_chunk_points(
            points,
            user_id=memory.user_id,
            workspace_id=str(memory.workspace_id),
            context_id=str(memory.context_id),
            collection_name=collection_name,
        )
        await delete_chunk_points_from(memory.id, len(points), collection_name)
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
        chars=len(content or ""),
        tokens=usage.get("tokens", 0),
        cached=usage.get("cached", 0),
    )
    return len(points)


#: Summary-point payload keys ``build_memory_point`` writes only when the
#: memory has the value: a payload sync removes them from the chunk points
#: when the memory no longer does (a cleared location must not keep matching
#: ``filters.near`` through a chunk).
_OPTIONAL_PAYLOAD_KEYS = ("context", "location")


async def sync_content_chunk_payload(
    db: AsyncSession,
    memory: Memory,
    *,
    base_payload: dict[str, Any],
    collection_name: str,
    chunks_enabled: bool | None = None,
) -> bool:
    """Copy the memory's new summary-point payload onto its chunk points; no embedding.

    For a memory whose metadata changed but whose content did not (#2015 D5:
    the Sleep importance re-eval and promotions), so its chunk vectors are
    still right. A promotion moves ``scope`` in Postgres only, so this is what
    carries it to the chunk points. Every copied field is set, not only the
    changed ones (the payload is rebuilt from the row), and the optional
    fields the payload no longer has are removed.

    Returns:
        True when the chunk points need nothing more: they carry the payload,
        the context has the flag off (it has no chunk points) or the store
        keeps none. False when they could not be updated (they were deleted,
        or the flag lookup or the delete failed): the caller rewrites them
        with :func:`write_content_chunks`. Never raises.
    """
    if _is_resource_row(memory):
        return True
    try:
        if chunks_enabled is None:
            chunks_enabled = await content_chunks_enabled(db, memory.context_id)
        if not chunks_enabled or not chunk_points_supported():
            return True
        return await sync_chunk_points_payload(
            memory.id,
            chunk_payload_updates(base_payload),
            collection_name,
            delete_keys=[k for k in _OPTIONAL_PAYLOAD_KEYS if k not in base_payload] or None,
        )
    except Exception as e:
        _log_failure(memory, collection_name, e)
        return False


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
    embedded in batches across memories, not one memory at a time. Unlike
    :func:`write_content_chunks` (upsert first, then trim, #2061) it still
    deletes first: content that is now too short loses its chunks, and chunks
    an earlier migration to the same model left are replaced. Recall reads the
    context's points in the target only after the switch, so the window
    without chunks is not seen. The
    flag is read fresh before the delete and again right before the upsert,
    and each read's transaction is ended before the next embedding or vector
    store call. It is not fail-open: a failed delete, embedding or upsert is
    logged and raised, so the migration stops and can be re-run (point ids
    are deterministic).

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
        # One delete for the whole page: no memory of it is to blame.
        logger.warning(
            "content_chunks_page_delete_failed",
            context_id=str(context_id),
            collection=collection_name,
            memories=len(plain),
            error_class=type(e).__name__,
        )
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
    digests = {memory.id: content_hash(memory.content) for memory in plain}
    # The page's embed usage, summed for the log line.
    total = {"cached": 0, "requests": 0, "tokens": 0}
    current: Memory | None = None
    try:
        for user_id, jobs in by_user.items():
            for start in range(0, len(jobs), CHUNK_EMBED_BATCH_SIZE):
                part = jobs[start : start + CHUNK_EMBED_BATCH_SIZE]
                current = part[0][0]
                batch_usage: dict[str, int] = {}
                try:
                    got = await embed_svc.embed_batch(
                        [text for _, _, text in part],
                        user_id,
                        context_id=str(context_id),
                        workspace_id=str(current.workspace_id),
                        usage=batch_usage,
                    )
                finally:
                    for key, value in batch_usage.items():
                        total[key] = total.get(key, 0) + value
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
                                content_hash=digests[memory.id],
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
        tokens=total["tokens"],
        cached=total["cached"],
    )
    return written
