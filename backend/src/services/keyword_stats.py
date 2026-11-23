"""Per-context statistics of the keyword (BM25) leg (#2984).

The document side of a point's ``bm25`` sparse vector is BM25-weighted
(``utils.sparse_vector``): each field's value depends on the field's length
``dl`` and on ``avgdl``, the average length of that field over the context.

The lengths live on the rows (``Memory.keyword_*_len``), written by the code
that writes the point, so Postgres stays the system of record. The averages are
an aggregate over the context's live rows (not soft-deleted, not suppressed),
counting only rows where the field is indexed (> 0): a row whose point was
never written since the columns exist (NULL) or whose field is empty adds
nothing. No row qualifies (a new context, a field nobody filled) = ``None``,
and the builder then uses the field's own length (saturation only).

The aggregate is cached in-process per (context, kind) for
``STATS_TTL_SECONDS``. So a write is weighted with averages up to that old, and
without its own row: expected, the averages move slowly, and
``python -m src.cli.rebuild_keyword_vectors`` brings every point of a context
to its current averages (it reads them with ``fresh=True``). Multi-process
deployments each hold their own cache; nothing is shared or invalidated.

A summary point fills the four memory lengths, a resource point (the
resource indexer's) the fulltext one; the others are set NULL, so the two
kinds never share an average.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.memory import Memory
from utils.sparse_vector import MemoryFieldStats, token_count

#: How long an aggregate is reused before it is read again (seconds).
STATS_TTL_SECONDS = 300.0
#: Bound on cached entries; past it the cache is emptied (it is only a cache).
_MAX_CACHED = 10_000

#: Summary-point payload token field -> the row column holding its length.
MEMORY_LENGTH_COLUMNS: dict[str, str] = {
    "summary_tokens": "keyword_summary_len",
    "context_summary_tokens": "keyword_context_summary_len",
    "content_tokens": "keyword_content_len",
    "summary_reading": "keyword_reading_len",
}
#: The row column holding a resource point's fulltext length.
RESOURCE_LENGTH_COLUMN = "keyword_fulltext_len"
LENGTH_COLUMNS: tuple[str, ...] = (*MEMORY_LENGTH_COLUMNS.values(), RESOURCE_LENGTH_COLUMN)

_MEMORY = "memory"
_RESOURCE = "resource"

_cache: dict[tuple[UUID, str], tuple[float, Any]] = {}


def clear_keyword_stats_cache() -> None:
    """Forget every cached aggregate (tests, and the rebuild CLI after a pass)."""
    _cache.clear()


def memory_keyword_lengths(payload: Mapping[str, Any]) -> dict[str, int | None]:
    """The length columns of a row whose summary point carries ``payload``.

    ``payload`` is what ``build_memory_point`` built (or a point's payload):
    the lengths are counted from its token fields, so they are what was
    indexed.
    """
    lengths: dict[str, int | None] = {
        column: token_count(str(payload.get(field) or ""))
        for field, column in MEMORY_LENGTH_COLUMNS.items()
    }
    lengths[RESOURCE_LENGTH_COLUMN] = None
    return lengths


def resource_keyword_lengths(fulltext_tokens: str) -> dict[str, int | None]:
    """The length columns of a row whose resource point indexed ``fulltext_tokens``."""
    lengths: dict[str, int | None] = dict.fromkeys(MEMORY_LENGTH_COLUMNS.values())
    lengths[RESOURCE_LENGTH_COLUMN] = token_count(fulltext_tokens)
    return lengths


def apply_keyword_lengths(memory: Any, lengths: Mapping[str, int | None]) -> bool:
    """Set ``lengths`` on the ORM row (flushed with its session); whether one changed."""
    changed = False
    for column, value in lengths.items():
        if getattr(memory, column, None) != value:
            setattr(memory, column, value)
            changed = True
    return changed


def _live(context_id: UUID) -> list[Any]:
    return [
        Memory.context_id == context_id,
        Memory.deleted_at.is_(None),
        Memory.embedding_suppressed_at.is_(None),
    ]


def _average(column: Any) -> Any:
    return func.avg(column).filter(column > 0)


def _cached(key: tuple[UUID, str]) -> tuple[bool, Any]:
    entry = _cache.get(key)
    if entry is not None and time.monotonic() - entry[0] < STATS_TTL_SECONDS:
        return True, entry[1]
    return False, None


def _store(key: tuple[UUID, str], value: Any) -> None:
    if len(_cache) >= _MAX_CACHED:
        _cache.clear()
    _cache[key] = (time.monotonic(), value)


def _as_float(value: Any) -> float | None:
    return float(value) if value is not None else None


async def memory_field_stats(
    db: AsyncSession, context_id: UUID | None, *, fresh: bool = False
) -> MemoryFieldStats:
    """The context's per-field averages of its summary points.

    Args:
        db: A session (one read).
        context_id: The context; ``None`` (a context-less row) = no averages.
        fresh: Read the aggregate even when a cached one is current (the
            rebuild CLI); the result is cached either way.

    Returns:
        ``MemoryFieldStats``; a field no live row has indexed is ``None``.
    """
    if not isinstance(context_id, UUID):
        return MemoryFieldStats()
    key = (context_id, _MEMORY)
    if not fresh:
        hit, value = _cached(key)
        if hit:
            return value
    row = (
        await db.execute(
            select(
                _average(Memory.keyword_summary_len),
                _average(Memory.keyword_context_summary_len),
                _average(Memory.keyword_content_len),
                _average(Memory.keyword_reading_len),
            ).where(*_live(context_id))
        )
    ).one()
    stats = MemoryFieldStats(
        summary=_as_float(row[0]),
        context_summary=_as_float(row[1]),
        content=_as_float(row[2]),
        summary_reading=_as_float(row[3]),
    )
    _store(key, stats)
    return stats


async def resource_fulltext_avgdl(
    db: AsyncSession, context_id: UUID | None, *, fresh: bool = False
) -> float | None:
    """The context's average fulltext length of its resource points (``None`` = none yet).

    Args: as ``memory_field_stats``.
    """
    if not isinstance(context_id, UUID):
        return None
    key = (context_id, _RESOURCE)
    if not fresh:
        hit, value = _cached(key)
        if hit:
            return value
    value = _as_float(
        await db.scalar(select(_average(Memory.keyword_fulltext_len)).where(*_live(context_id)))
    )
    _store(key, value)
    return value
