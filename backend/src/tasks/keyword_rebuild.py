"""Rebuild the keyword (BM25) vectors of a context's points (#2984).

``python -m src.cli.rebuild_keyword_vectors`` runs it. A point's ``bm25``
sparse vector is BM25-weighted with its context's average field lengths
(``services.keyword_stats``) as they were when the point was written. This
pass brings every point of a context to the context's current averages, for
the upgrade that introduced the weighting (points written before it carry raw
term frequencies) and for contexts whose averages have moved since.

Per context, two passes over its live, embedded (``success``) rows, by id:

1. Lengths. Each row is tokenized from Postgres with the writer's own code
   (``memory_keyword_tokens``; a resource row from its point's indexed text),
   the length columns are stored, and a summary point whose token payload
   fields differ from the row's (a tokenizer or dictionary change since its
   write) gets them rewritten.
2. Vectors. With the context's averages read fresh (past the cache), each
   point's ``bm25`` vector is rebuilt from its token payload and replaced (or
   removed, when no token is left: a writer leaves such a point without one).
   A point whose vector already equals the rebuilt one is left
   (``unchanged``), so a second run over unchanged rows writes nothing.

No embedding call, no dense vector or other payload change, no row change but
the length columns.

Live rows that are not embedded (``pending``, ``processing``, ``failed``, never
embedded) are counted as ``not_embedded``: their writer weights them when it
writes their point.

Concurrent writers: each page's rows are locked ``FOR NO KEY UPDATE SKIP
LOCKED`` and only rows whose ``embedding_status`` is ``success`` are taken, and
the page's vector store writes happen while the locks are held. A writer that
rewrites a point first changes its row (an edit re-queues it ``pending``, the
pipeline claims it ``processing``, Sleep and the resource indexer update or
lock it), so it either waits for the page to commit and then writes the newer
point, or the page skips the row (``skipped``). In pass 2 a point whose token
payload does not have the lengths the row records (a writer came between the
passes) is skipped as well. A skipped row is counted, never an error: its
writer weights it. A point that disappears before the page's write
(``missing_point``) does not stop the run either. Each row is counted once per
run, whichever pass met it.

Feed-managed contexts (a sync feed or one stopping, ``sync_managed_guard``)
take no write here, as with every other vector writer: each page takes the
context's management fence first, and a managed or busy context is left
(``managed_contexts``), never an error.

LanceDB keeps no sparse vector (its FTS scores with BM25 itself), but its
full-text column is built from a point's token fields, so a tokenizer change
(#2986) needs it rebuilt as well: with the LanceDB store the run does pass 1
only, writing a row's token fields through the store's payload update (which
rebuilds the point's ``search_text``) when they differ from the stored ones.
Its resource points are not in the LanceDB store (``missing_point``).
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.qdrant import (
    KAGURA_MEMORIES_BM25_VECTOR_NAME,
    QDRANT_TOKEN_PAYLOAD_FIELDS,
    _active_store,
    retrieve_keyword_points,
    update_memory_payload_in_qdrant,
    write_keyword_points,
)
from models.auth import Context
from models.memory import Memory
from services.context_routing import resolve_collection_name
from services.keyword_stats import (
    MEMORY_LENGTH_COLUMNS,
    RESOURCE_LENGTH_COLUMN,
    apply_keyword_lengths,
    memory_field_stats,
    memory_keyword_lengths,
    resource_fulltext_avgdl,
    resource_keyword_lengths,
)
from services.memory_service import memory_keyword_tokens
from services.resource_indexer import owns_resource_point
from utils.exceptions import QdrantError, SyncLockBusyError
from utils.logger import get_logger
from utils.sparse_vector import (
    MemoryFieldStats,
    build_document_sparse_vector,
    build_resource_sparse_vector,
    token_count,
)
from utils.tokenizer import tokenize_for_search

logger = get_logger(__name__)

#: Rows per page (one lock window, one retrieve and one vector store write).
PAGE_SIZE = 200
#: Relative tolerance when comparing a stored vector (float32) with a rebuilt one.
_REL_TOLERANCE = 1e-5

Progress = Callable[[str], None]

_SKIPPED = "skipped"
_MISSING = "missing_point"
_NOT_EMBEDDED = "not_embedded"


@dataclass
class KeywordRebuild:
    """What a run planned or did."""

    dry_run: bool
    #: ``"lance"`` with the LanceDB store (token fields and full-text column only).
    store: str = "qdrant"
    contexts: int = 0
    #: Plan: live embedded rows of the contexts, and those without lengths yet.
    rows: int = 0
    rows_without_lengths: int = 0
    #: Apply counters.
    lengths_written: int = 0
    tokens_rewritten: int = 0
    vectors_written: int = 0
    unchanged: int = 0
    #: Rows another writer holds (locked, or changed between the passes).
    skipped: int = 0
    #: Rows whose point is not in the store (or left it during the run).
    missing_point: int = 0
    #: Live rows that are not embedded (pending, processing, failed).
    not_embedded: int = 0
    #: Contexts left because they are feed-managed (or their fence was busy).
    managed_contexts: int = 0
    #: The last context finished: ``--after`` of the next run.
    last_context: UUID | None = None
    #: ``"limit"`` when ``--limit`` contexts were done and more remain.
    stopped: str | None = None
    seconds: float = 0.0
    #: The rows counted per kind in the current context (each row once).
    _counted: dict[str, set[UUID]] = field(default_factory=dict, repr=False, compare=False)

    @property
    def written(self) -> int:
        return self.lengths_written + self.tokens_rewritten + self.vectors_written

    def count_row(self, kind: str, memory_id: UUID) -> None:
        """Count ``memory_id`` under ``kind`` once, whichever pass meets it."""
        seen = self._counted.setdefault(kind, set())
        if memory_id not in seen:
            seen.add(memory_id)
            setattr(self, kind, getattr(self, kind) + 1)

    def next_context(self) -> None:
        """Rows belong to one context: the per-row memory starts again."""
        self._counted.clear()


class _ContextManaged(Exception):
    """The context is feed-managed (or its fence is busy): leave it."""

    def __init__(self, state: str) -> None:
        super().__init__(state)
        self.state = state


@dataclass
class _Page:
    """One page of a context's live rows, by id."""

    #: Every live row of the page.
    ids: list[UUID]
    #: The embedded rows this run locked (and may write).
    rows: list[Memory]
    #: The live rows that were not embedded when the page was read.
    not_embedded: list[UUID]


def _live(context_id: UUID) -> list[Any]:
    return [
        Memory.context_id == context_id,
        Memory.deleted_at.is_(None),
        Memory.embedding_suppressed_at.is_(None),
    ]


async def _contexts(
    db: AsyncSession, context_id: UUID | None, after: UUID | None, limit: int | None
) -> tuple[list[UUID], bool]:
    """The contexts of this run, by id, and whether more remain past ``limit``."""
    if context_id is not None:
        return [context_id], False
    stmt = select(Context.id).where(Context.deleted_at.is_(None)).order_by(Context.id)
    if after is not None:
        stmt = stmt.where(Context.id > after)
    if limit is not None:
        stmt = stmt.limit(limit + 1)
    ids = list((await db.execute(stmt)).scalars().all())
    if limit is not None and len(ids) > limit:
        return ids[:limit], True
    return ids, False


async def _plan_context(db: AsyncSession, context_id: UUID, result: KeywordRebuild) -> None:
    no_lengths = Memory.keyword_summary_len.is_(None) & Memory.keyword_fulltext_len.is_(None)
    rows, without = (
        await db.execute(
            select(func.count(), func.count().filter(no_lengths)).where(
                *_live(context_id), Memory.embedding_status == "success"
            )
        )
    ).one()
    result.rows += int(rows or 0)
    result.rows_without_lengths += int(without or 0)


async def _managed_state(db: AsyncSession, context_id: UUID) -> str | None:
    """The context's management state when no vector writer may write it, else None.

    Takes the context's shared management fence in the page's transaction
    (``sync_managed_guard.target_fence``), so no mode change can come between
    this check and the page's writes. A fence another transaction holds
    exclusively (a mode change in progress) reads ``"busy"``.
    """
    from services.sync_managed_guard import target_fence

    try:
        fence = await target_fence(db, context_id)
    except SyncLockBusyError:
        return "busy"
    return fence.state if fence.state not in (None, "manual") else None


async def _locked_page(
    db: AsyncSession, context_id: UUID, after_id: UUID | None, page_size: int
) -> _Page:
    """The next page's live rows, and the embedded ones this run may write (locked)."""
    stmt = (
        select(Memory.id, Memory.embedding_status)
        .where(*_live(context_id))
        .order_by(Memory.id)
        .limit(page_size)
    )
    if after_id is not None:
        stmt = stmt.where(Memory.id > after_id)
    listed = list((await db.execute(stmt)).all())
    ids = [row[0] for row in listed]
    if not ids:
        return _Page([], [], [])
    not_embedded = [row[0] for row in listed if row[1] != "success"]
    rows = list(
        (
            await db.execute(
                select(Memory)
                .where(
                    Memory.id.in_(ids),
                    *_live(context_id),
                    Memory.embedding_status == "success",
                )
                .order_by(Memory.id)
                .with_for_update(skip_locked=True, key_share=True)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    return _Page(ids, rows, not_embedded)


def _point_of(points: dict[str, Any], memory: Memory, context_id: UUID) -> dict[str, Any] | None:
    """The payload of ``memory``'s point, or None when it has none in this context."""
    record = points.get(str(memory.point_id))
    payload = getattr(record, "payload", None)
    if not isinstance(payload, dict) or payload.get("context_id") != str(context_id):
        return None
    return payload


def _resource_text(payload: dict[str, Any]) -> str | None:
    content = payload.get("content")
    return content if isinstance(content, str) and content else None


async def _write_points(
    collection: str,
    *,
    payloads: Sequence[tuple[UUID, dict[str, Any]]] = (),
    vectors: Sequence[tuple[UUID, list[int], list[float]]] = (),
    delete_vectors: Sequence[UUID] = (),
) -> set[str]:
    """Write a page's points; the ids of those that were gone (written nothing).

    One request for the page. When it fails (one point deleted since it was
    read fails the whole request), each point is written on its own: a point
    that no longer exists is returned, any other failure is raised.
    """
    if not payloads and not vectors and not delete_vectors:
        return set()
    try:
        await write_keyword_points(
            collection, payloads=payloads, vectors=vectors, delete_vectors=delete_vectors
        )
        return set()
    except QdrantError as exc:
        logger.warning("keyword_rebuild_page_write_failed", error_class=type(exc).__name__)

    gone: set[str] = set()

    async def one(point_id: UUID, **write: Any) -> None:
        try:
            await write_keyword_points(collection, **write)
        except QdrantError:
            if await retrieve_keyword_points([point_id], collection):
                raise
            gone.add(str(point_id))

    for point_id, payload in payloads:
        await one(point_id, payloads=[(point_id, payload)])
    for point_id, indices, values in vectors:
        await one(point_id, vectors=[(point_id, indices, values)])
    for point_id in delete_vectors:
        await one(point_id, delete_vectors=[point_id])
    return gone


async def _lengths_page(
    db: AsyncSession,
    context_id: UUID,
    collection: str,
    rows: Sequence[Memory],
    result: KeywordRebuild,
) -> None:
    points = await retrieve_keyword_points([memory.point_id for memory in rows], collection)
    payload_writes: list[tuple[UUID, dict[str, Any]]] = []
    lengths: list[tuple[Memory, dict[str, int | None]]] = []
    for memory in rows:
        payload = _point_of(points, memory, context_id)
        if payload is None:
            result.count_row(_MISSING, memory.id)
            continue
        if owns_resource_point(memory):
            text = _resource_text(payload)
            if text is None:
                result.count_row(_MISSING, memory.id)
                continue
            lengths.append((memory, resource_keyword_lengths(tokenize_for_search(text))))
        else:
            fields = memory_keyword_tokens(memory).payload_fields()
            if any(payload.get(key) != value for key, value in fields.items()):
                payload_writes.append((memory.point_id, fields))
            lengths.append((memory, memory_keyword_lengths(fields)))
    # Under the page's row locks: the rows commit after the points are written.
    gone = await _write_points(collection, payloads=payload_writes)
    rewritten = {str(point_id) for point_id, _ in payload_writes} - gone
    for memory, values in lengths:
        if str(memory.point_id) in gone:
            result.count_row(_MISSING, memory.id)
            continue
        if str(memory.point_id) in rewritten:
            result.tokens_rewritten += 1
        if apply_keyword_lengths(memory, values):
            result.lengths_written += 1
    await db.flush()


def _stored_bm25(record: Any) -> Any:
    vector = getattr(record, "vector", None)
    return vector.get(KAGURA_MEMORIES_BM25_VECTOR_NAME) if isinstance(vector, dict) else None


def _same_vector(record: Any, indices: list[int], values: list[float]) -> bool:
    stored = _stored_bm25(record)
    if stored is None or len(stored.indices) != len(indices):
        return False
    old = dict(zip(stored.indices, stored.values, strict=True))
    for index, value in zip(indices, values, strict=True):
        if index not in old or not math.isclose(old[index], value, rel_tol=_REL_TOLERANCE):
            return False
    return True


def _memory_lengths_match(memory: Memory, payload: dict[str, Any]) -> bool:
    return all(
        getattr(memory, MEMORY_LENGTH_COLUMNS[field]) == token_count(str(payload.get(field) or ""))
        for field in QDRANT_TOKEN_PAYLOAD_FIELDS
    )


async def _vectors_page(
    context_id: UUID,
    collection: str,
    rows: Sequence[Memory],
    stats: MemoryFieldStats,
    resource_avgdl: float | None,
    result: KeywordRebuild,
) -> None:
    points = await retrieve_keyword_points(
        [memory.point_id for memory in rows], collection, with_bm25=True
    )
    writes: list[tuple[UUID, list[int], list[float]]] = []
    deletes: list[UUID] = []
    by_point: dict[str, Memory] = {}
    for memory in rows:
        payload = _point_of(points, memory, context_id)
        if payload is None:
            result.count_row(_MISSING, memory.id)
            continue
        if owns_resource_point(memory):
            text = _resource_text(payload)
            if text is None:
                result.count_row(_MISSING, memory.id)
                continue
            tokens = tokenize_for_search(text)
            if getattr(memory, RESOURCE_LENGTH_COLUMN) != token_count(tokens):
                result.count_row(_SKIPPED, memory.id)
                continue
            indices, values = build_resource_sparse_vector(text, resource_avgdl, tokens=tokens)
        else:
            if not _memory_lengths_match(memory, payload):
                result.count_row(_SKIPPED, memory.id)
                continue
            indices, values = build_document_sparse_vector(
                summary_tokens=str(payload.get("summary_tokens") or ""),
                context_summary_tokens=str(payload.get("context_summary_tokens") or ""),
                content_tokens=str(payload.get("content_tokens") or ""),
                summary_reading=str(payload.get("summary_reading") or ""),
                stats=stats,
            )
        record = points.get(str(memory.point_id))
        by_point[str(memory.point_id)] = memory
        if not indices:
            # No token left: the point carries no bm25 vector, as a writer leaves it.
            stored = _stored_bm25(record)
            if stored is not None and len(stored.indices):
                deletes.append(memory.point_id)
            else:
                result.unchanged += 1
            continue
        if _same_vector(record, indices, values):
            result.unchanged += 1
            continue
        writes.append((memory.point_id, indices, values))
    gone = await _write_points(collection, vectors=writes, delete_vectors=deletes)
    for point_id in gone:
        result.count_row(_MISSING, by_point[point_id].id)
    result.vectors_written += sum(
        1 for point_id in [*(w[0] for w in writes), *deletes] if str(point_id) not in gone
    )


async def _walk(
    db: AsyncSession,
    context_id: UUID,
    page_size: int,
    result: KeywordRebuild,
    handle: Callable[[Sequence[Memory]], Any],
    progress: Progress | None,
    label: str,
) -> None:
    after_id: UUID | None = None
    seen = 0
    while True:
        # The fence first, in the page's transaction: a managed context takes
        # no write from this page on.
        state = await _managed_state(db, context_id)
        if state is not None:
            await db.rollback()
            raise _ContextManaged(state)
        page = await _locked_page(db, context_id, after_id, page_size)
        if not page.ids:
            await db.commit()
            return
        for memory_id in page.not_embedded:
            result.count_row(_NOT_EMBEDDED, memory_id)
        taken = {memory.id for memory in page.rows}
        for memory_id in page.ids:
            if memory_id not in taken and memory_id not in page.not_embedded:
                result.count_row(_SKIPPED, memory_id)
        if page.rows:
            await handle(page.rows)
        # Ends the page's transaction: the locks go, the lengths commit.
        await db.commit()
        db.expunge_all()
        after_id = page.ids[-1]
        seen += len(page.ids)
        if progress is not None:
            progress(f"context {context_id} {label}: {seen} rows")


async def _lance_page(
    context_id: UUID, collection: str, rows: Sequence[Memory], result: KeywordRebuild
) -> None:
    """Pass 1 on the LanceDB store: a summary point gets the row's token fields."""
    summary_rows = [memory for memory in rows if not owns_resource_point(memory)]
    for memory in rows:
        if owns_resource_point(memory):
            # The resource indexer's points are not in the LanceDB store.
            result.count_row(_MISSING, memory.id)
    points = await retrieve_keyword_points([memory.point_id for memory in summary_rows], collection)
    for memory in summary_rows:
        payload = _point_of(points, memory, context_id)
        if payload is None:
            result.count_row(_MISSING, memory.id)
            continue
        fields = memory_keyword_tokens(memory).payload_fields()
        if any(payload.get(key) != value for key, value in fields.items()):
            # The store rebuilds the point's full-text column from the merged payload.
            await update_memory_payload_in_qdrant(
                memory.point_id, dict(fields), collection, cascade_chunks=False
            )
            result.tokens_rewritten += 1
        else:
            result.unchanged += 1
        if apply_keyword_lengths(memory, memory_keyword_lengths(fields)):
            result.lengths_written += 1


async def _rebuild_context(
    db: AsyncSession,
    context_id: UUID,
    page_size: int,
    result: KeywordRebuild,
    progress: Progress | None,
) -> None:
    collection = await resolve_collection_name(db, context_id)
    await db.commit()

    if result.store == "lance":

        async def lance(rows: Sequence[Memory]) -> None:
            await _lance_page(context_id, collection, rows, result)
            await db.flush()

        await _walk(db, context_id, page_size, result, lance, progress, "tokens")
        return

    async def lengths(rows: Sequence[Memory]) -> None:
        await _lengths_page(db, context_id, collection, rows, result)

    await _walk(db, context_id, page_size, result, lengths, progress, "lengths")

    stats = await memory_field_stats(db, context_id, fresh=True)
    resource_avgdl = await resource_fulltext_avgdl(db, context_id, fresh=True)
    await db.commit()

    async def vectors(rows: Sequence[Memory]) -> None:
        await _vectors_page(context_id, collection, rows, stats, resource_avgdl, result)

    await _walk(db, context_id, page_size, result, vectors, progress, "vectors")


async def run_keyword_rebuild(
    db: AsyncSession,
    *,
    context_id: UUID | None = None,
    after: UUID | None = None,
    limit: int | None = None,
    dry_run: bool = True,
    page_size: int = PAGE_SIZE,
    progress: Progress | None = None,
) -> KeywordRebuild:
    """Plan or run the keyword vector rebuild.

    Args:
        db: A session; the run commits page by page.
        context_id: One context; ``None`` = every live context, by id.
        after: Every context: only those with an id above this (``resume``).
        limit: Every context: at most this many contexts in this run.
        dry_run: Count only (no vector store call, no write).
        page_size: Rows per page.
        progress: Called with a line after each page.

    Returns:
        The counts; ``last_context`` is the resume point.
    """
    started = time.monotonic()
    result = KeywordRebuild(dry_run=dry_run)
    if _active_store() is not None:
        result.store = "lance"
    contexts, more = await _contexts(db, context_id, after, limit)
    for current in contexts:
        if dry_run:
            await _plan_context(db, current, result)
        else:
            result.next_context()
            try:
                await _rebuild_context(db, current, page_size, result, progress)
                logger.info("keyword_rebuild_context_done", context_id=str(current))
            except _ContextManaged as managed:
                result.managed_contexts += 1
                logger.info(
                    "keyword_rebuild_context_managed",
                    context_id=str(current),
                    state=managed.state,
                )
                if progress is not None:
                    progress(f"context {current} left: managed ({managed.state})")
        result.contexts += 1
        result.last_context = current
    if more:
        result.stopped = "limit"
    await db.commit()
    result.seconds = time.monotonic() - started
    return result
