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
   point's ``bm25`` vector is rebuilt from its token payload and replaced. A
   point whose vector already equals the rebuilt one is left (``unchanged``),
   so a second run over unchanged rows writes nothing.

No embedding call, no dense vector or other payload change, no row change but
the length columns.

Concurrent writers: each page's rows are locked ``FOR NO KEY UPDATE SKIP
LOCKED`` and only rows whose ``embedding_status`` is ``success`` are taken, and
the page's vector store writes happen while the locks are held. A writer that
rewrites a point first changes its row (an edit re-queues it ``pending``, the
pipeline claims it ``processing``, Sleep and the resource indexer update or
lock it), so it either waits for the page to commit and then writes the newer
point, or the page skips the row (``skipped``). In pass 2 a point whose token
payload does not have the lengths the row records (a writer came between the
passes) is skipped as well. A skipped row is counted, never an error: its
writer weights it.

LanceDB keeps no sparse vector (its FTS scores with BM25 itself): the run
stops with ``store="lance"`` and writes nothing.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.qdrant import (
    KAGURA_MEMORIES_BM25_VECTOR_NAME,
    QDRANT_TOKEN_PAYLOAD_FIELDS,
    _active_store,
    retrieve_keyword_points,
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


@dataclass
class KeywordRebuild:
    """What a run planned or did."""

    dry_run: bool
    #: ``"lance"`` when the run stopped because the active store is LanceDB.
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
    skipped: int = 0
    missing_point: int = 0
    #: The last context finished: ``--after`` of the next run.
    last_context: UUID | None = None
    #: ``"limit"`` when ``--limit`` contexts were done and more remain.
    stopped: str | None = None
    seconds: float = 0.0

    @property
    def written(self) -> int:
        return self.lengths_written + self.tokens_rewritten + self.vectors_written


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


async def _locked_page(
    db: AsyncSession, context_id: UUID, after_id: UUID | None, page_size: int
) -> tuple[list[UUID], list[Memory]]:
    """The next page's ids, and the rows of it this run may write (locked)."""
    stmt = select(Memory.id).where(*_live(context_id)).order_by(Memory.id).limit(page_size)
    if after_id is not None:
        stmt = stmt.where(Memory.id > after_id)
    ids = list((await db.execute(stmt)).scalars().all())
    if not ids:
        return [], []
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
    return ids, rows


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


async def _lengths_page(
    db: AsyncSession,
    context_id: UUID,
    collection: str,
    rows: Sequence[Memory],
    result: KeywordRebuild,
) -> None:
    points = await retrieve_keyword_points([memory.point_id for memory in rows], collection)
    payload_writes: list[tuple[UUID, dict[str, Any]]] = []
    for memory in rows:
        payload = _point_of(points, memory, context_id)
        if payload is None:
            result.missing_point += 1
            continue
        if owns_resource_point(memory):
            text = _resource_text(payload)
            if text is None:
                result.missing_point += 1
                continue
            lengths = resource_keyword_lengths(tokenize_for_search(text))
        else:
            fields = memory_keyword_tokens(memory).payload_fields()
            if any(payload.get(key) != value for key, value in fields.items()):
                payload_writes.append((memory.point_id, fields))
                result.tokens_rewritten += 1
            lengths = memory_keyword_lengths(fields)
        if apply_keyword_lengths(memory, lengths):
            result.lengths_written += 1
    # Under the page's row locks: the rows commit after the points are written.
    await write_keyword_points(collection, payloads=payload_writes)
    await db.flush()


def _same_vector(record: Any, indices: list[int], values: list[float]) -> bool:
    vector = getattr(record, "vector", None)
    stored = vector.get(KAGURA_MEMORIES_BM25_VECTOR_NAME) if isinstance(vector, dict) else None
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
    for memory in rows:
        payload = _point_of(points, memory, context_id)
        if payload is None:
            result.missing_point += 1
            continue
        if owns_resource_point(memory):
            text = _resource_text(payload)
            if text is None:
                result.missing_point += 1
                continue
            tokens = tokenize_for_search(text)
            if getattr(memory, RESOURCE_LENGTH_COLUMN) != token_count(tokens):
                result.skipped += 1
                continue
            indices, values = build_resource_sparse_vector(text, resource_avgdl, tokens=tokens)
        else:
            if not _memory_lengths_match(memory, payload):
                result.skipped += 1
                continue
            indices, values = build_document_sparse_vector(
                summary_tokens=str(payload.get("summary_tokens") or ""),
                context_summary_tokens=str(payload.get("context_summary_tokens") or ""),
                content_tokens=str(payload.get("content_tokens") or ""),
                summary_reading=str(payload.get("summary_reading") or ""),
                stats=stats,
            )
        if not indices:
            result.unchanged += 1
            continue
        if _same_vector(points.get(str(memory.point_id)), indices, values):
            result.unchanged += 1
            continue
        writes.append((memory.point_id, indices, values))
    await write_keyword_points(collection, vectors=writes)
    result.vectors_written += len(writes)


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
        ids, rows = await _locked_page(db, context_id, after_id, page_size)
        if not ids:
            await db.commit()
            return
        result.skipped += len(ids) - len(rows)
        if rows:
            await handle(rows)
        # Ends the page's transaction: the locks go, the lengths commit.
        await db.commit()
        db.expunge_all()
        after_id = ids[-1]
        seen += len(ids)
        if progress is not None:
            progress(f"context {context_id} {label}: {seen} rows")


async def _rebuild_context(
    db: AsyncSession,
    context_id: UUID,
    page_size: int,
    result: KeywordRebuild,
    progress: Progress | None,
) -> None:
    collection = await resolve_collection_name(db, context_id)
    await db.commit()

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
        return result
    contexts, more = await _contexts(db, context_id, after, limit)
    for current in contexts:
        if dry_run:
            await _plan_context(db, current, result)
        else:
            await _rebuild_context(db, current, page_size, result, progress)
            logger.info("keyword_rebuild_context_done", context_id=str(current))
        result.contexts += 1
        result.last_context = current
    if more:
        result.stopped = "limit"
    await db.commit()
    result.seconds = time.monotonic() - started
    return result
