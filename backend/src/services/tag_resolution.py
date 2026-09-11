"""Read-time tag resolution against a context's vocabulary (Issue #1503).

``recall`` tag filters are exact-match against the Qdrant payload, so a
writer-side spelling drift makes matching memories invisible with no signal that
near-miss tags exist — the caller cannot tell "nothing is stored" apart from
"spelled differently".

Two opt-in read-time affordances, both driven by the tags actually present in
the context:

* :func:`expand_tag_filter` — widen the filter to every stored spelling that is
  a MECHANICAL variant of what was asked for (case / separators / plural).
  Applied only when the caller passes ``filters.tags_normalize = true``, so
  exact semantics remain the default.
* :func:`suggest_tags` — advisory hints when a tag filter matched nothing.
  Includes looser relations (abbreviation, typo) that must never widen a filter.

Both read the vocabulary with one bounded query (``expand_tag_filter`` through
the #1512 TTL cache, ``suggest_tags`` as stored). The caller is responsible for
having authorized the (workspace, context) first — these helpers do no access
check of their own and must never be reachable from an unauthorized path.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from time import perf_counter
from typing import Final
from uuid import UUID

from cachetools import TTLCache
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.memory import Memory
from utils.logger import get_logger
from utils.tag_normalize import is_near_duplicate, normalize_tag

logger = get_logger(__name__)

# Bound on the vocabulary read. A context past this many distinct tags has a
# hygiene problem of its own (#746); truncating keeps one recall from scanning
# an unbounded set, at the cost of missing rare tags in the tail.
VOCABULARY_LIMIT = 2000

# Bound on how many spellings one requested tag may expand to, so a pathological
# vocabulary cannot inflate the Qdrant filter without limit.
MAX_EXPANSION_PER_TAG = 20

# Bound on suggestions returned per requested tag.
MAX_SUGGESTIONS_PER_TAG = 5

# #1512: the ``tag_near_duplicate`` rule in ``write_lint`` runs on every tagged
# ``remember()`` / ``update_memory()`` response path, and the aggregate is
# O(memories x tags) with no index to serve ``unnest``. Tag vocabularies change
# slowly, so writes (and ``expand_tag_filter``, whose widening only needs the
# mechanical variants) read through a process-local TTL cache — the same shape
# as the llm_pricing cache (#713). ``suggest_tags`` stays uncached: it is
# already gated to zero-result tag-filtered recalls and must cite the
# vocabulary as stored.
#
# Staleness bound — be honest about it: the entry is written on a miss and
# extended by each write's tags (write-through), but nothing invalidates it.
# forget() / update_memory() / delete_context / Sleep leave the entry untouched
# until the TTL expires, so a tag that no longer exists (or whose count moved)
# can be cited in hints for up to VOCABULARY_CACHE_TTL_SECONDS, and an update
# that replaced a row's tags keeps the old spellings counted for as long. That
# is the accepted trade for taking the aggregate off most writes: every hint
# is advisory and cites nothing the caller could not read.
#
# The key carries the #1511 scoping dimension: a non-shared context aggregates
# only the caller's own rows, so its entry is keyed per user; a shared context
# aggregates every author and is keyed once for all of them. Without that, one
# user's tag names and counts would be served to another. Sharing is resolved
# on EVERY call (one indexed SELECT) — it is the property that keeps a private
# context from serving another user's tags, so it is never cached.
#
# A read that fails is cached as an empty vocabulary for the TTL (negative
# caching): a context whose aggregate cannot complete must not re-run it on
# every write. Concurrent misses on one key share a single in-flight read.
VOCABULARY_CACHE_TTL_SECONDS: Final = 120
VOCABULARY_CACHE_MAXSIZE: Final = 512
_SHARED_SCOPE: Final = "*"
_CacheKey = tuple[UUID, UUID, str]
_vocabulary_cache: TTLCache[_CacheKey, dict[str, int]] = TTLCache[_CacheKey, dict[str, int]](
    maxsize=VOCABULARY_CACHE_MAXSIZE, ttl=VOCABULARY_CACHE_TTL_SECONDS
)
# Single-flight: one aggregate per key at a time; later misses await it.
_inflight: dict[_CacheKey, asyncio.Future[dict[str, int]]] = {}


def clear_vocabulary_cache() -> None:
    """Drop every entry from the process-local vocabulary cache.

    Exposed for tests that need a deterministic cache state (the autouse
    conftest fixture calls it around every test).
    """
    _vocabulary_cache.clear()
    _inflight.clear()


async def fetch_vocabulary(
    db: AsyncSession,
    *,
    workspace_id: UUID,
    context_id: UUID,
    user_id: str,
) -> dict[str, int]:
    """Distinct tag -> memory count for one context, as THIS caller may read it.

    ``DISTINCT id, unnest(tags)`` semantics are not needed here: the outer
    aggregate counts distinct memory ids per tag directly, so a memory carrying
    the same tag twice still counts once (the #614 lesson).

    The ``user_id`` filter mirrors the one ``SearchService`` applies to recall:
    in a context that is not shared, a caller sees only their OWN memories, so
    aggregating over every author would let tag names and counts describe rows
    the caller cannot read. Sharing is resolved once per call, and only a shared
    context aggregates across authors.

    Args:
        db: Session.
        workspace_id: Authorized workspace.
        context_id: Authorized context.
        user_id: Caller identity, used to scope the aggregate.

    Returns:
        ``{tag: memory_count}``, capped at ``VOCABULARY_LIMIT`` by descending count.
    """
    shared = await _is_context_shared(db, context_id)
    return await _read_vocabulary(
        db, workspace_id=workspace_id, context_id=context_id, user_id=user_id, shared=shared
    )


async def fetch_vocabulary_cached(
    db: AsyncSession,
    *,
    workspace_id: UUID,
    context_id: UUID,
    user_id: str,
) -> dict[str, int]:
    """:func:`fetch_vocabulary` through the TTL cache (#1512) — read-only.

    Returns the cached mapping itself; callers must not mutate it. See the
    cache notes above for the staleness bound.
    """
    entry, _hit = await _cached_entry(
        db, workspace_id=workspace_id, context_id=context_id, user_id=user_id
    )
    return entry


async def vocabulary_before_write(
    db: AsyncSession,
    *,
    workspace_id: UUID,
    context_id: UUID,
    user_id: str,
    written_tags: Iterable[str],
) -> dict[str, int]:
    """The vocabulary as it stood BEFORE the write carrying ``written_tags``.

    ``write_lint`` runs after the row is committed, so a raw aggregate already
    contains the write being linted and ``tag in vocabulary`` would suppress
    every hint on a cache miss while a hit (a snapshot taken before the write)
    would fire it — the rule's output must not depend on cache warmth. The
    snapshot returned here always excludes this write:

    * miss — the aggregate ran after the commit and includes the row, so the
      snapshot is the aggregate minus this write's tags (count - 1 each, dropped
      at 0); the cache keeps the aggregate, which already reflects the row.
    * hit — the cached entry predates the row, so it IS the snapshot; the
      write's tags are then written through into the entry (count + 1 each) so
      later hits see them as established spellings.

    The snapshot is a copy; the cached entry is never handed out from here.
    """
    tags = {t for t in written_tags if t}
    entry, hit = await _cached_entry(
        db, workspace_id=workspace_id, context_id=context_id, user_id=user_id
    )
    snapshot = dict(entry)
    if hit:
        for tag in tags:
            entry[tag] = entry.get(tag, 0) + 1
        return snapshot
    for tag in tags:
        remaining = snapshot.get(tag, 0) - 1
        if remaining > 0:
            snapshot[tag] = remaining
        else:
            snapshot.pop(tag, None)
    return snapshot


async def _cached_entry(
    db: AsyncSession,
    *,
    workspace_id: UUID,
    context_id: UUID,
    user_id: str,
) -> tuple[dict[str, int], bool]:
    """``(entry, hit)`` for the caller's scope, loading it single-flight on a miss."""
    shared = await _is_context_shared(db, context_id)
    key: _CacheKey = (workspace_id, context_id, _SHARED_SCOPE if shared else user_id)
    cached = _vocabulary_cache.get(key)
    if cached is not None:
        logger.debug("tag_vocabulary_read", context_id=str(context_id), cache="hit")
        return cached, True

    pending = _inflight.get(key)
    if pending is not None:
        return await pending, False

    loop = asyncio.get_running_loop()
    future: asyncio.Future[dict[str, int]] = loop.create_future()
    _inflight[key] = future
    started = perf_counter()
    try:
        try:
            vocabulary = await _read_vocabulary(
                db,
                workspace_id=workspace_id,
                context_id=context_id,
                user_id=user_id,
                shared=shared,
            )
        except Exception as e:  # noqa: BLE001 — negative-cache a failed aggregate
            logger.warning(
                "tag_vocabulary_read_failed",
                context_id=str(context_id),
                error=str(e),
                cached_empty_for_seconds=VOCABULARY_CACHE_TTL_SECONDS,
            )
            vocabulary = {}
        _vocabulary_cache[key] = vocabulary
        logger.info(
            "tag_vocabulary_read",
            context_id=str(context_id),
            cache="miss",
            duration_ms=round((perf_counter() - started) * 1000.0, 2),
            vocabulary_size=len(vocabulary),
            scope="shared" if shared else "user",
        )
        future.set_result(vocabulary)
        return vocabulary, False
    except BaseException as e:
        # Cancellation (or anything else unexpected) must not strand the
        # waiters on a future that never resolves.
        if not future.done():
            future.set_exception(e)
        raise
    finally:
        _inflight.pop(key, None)


async def _is_context_shared(db: AsyncSession, context_id: UUID) -> bool:
    from services.context_service import ContextService

    return await ContextService(db).is_context_shared(context_id)


async def _read_vocabulary(
    db: AsyncSession,
    *,
    workspace_id: UUID,
    context_id: UUID,
    user_id: str,
    shared: bool,
) -> dict[str, int]:
    """The uncached aggregate; ``shared`` decides whether ``user_id`` scopes it."""
    conditions = [
        Memory.workspace_id == workspace_id,
        Memory.context_id == context_id,
        Memory.deleted_at.is_(None),
    ]
    if not shared:
        conditions.append(Memory.user_id == user_id)

    tag = func.unnest(Memory.tags).label("tag")
    inner = select(Memory.id.label("memory_id"), tag).where(*conditions).subquery()
    stmt = (
        select(inner.c.tag, func.count(func.distinct(inner.c.memory_id)))
        .group_by(inner.c.tag)
        .order_by(func.count(func.distinct(inner.c.memory_id)).desc())
        .limit(VOCABULARY_LIMIT)
    )
    rows = await db.execute(stmt)
    return {row[0]: row[1] for row in rows.all() if row[0]}


async def expand_tag_filter(
    db: AsyncSession,
    *,
    workspace_id: UUID,
    context_id: UUID,
    user_id: str,
    tags: list[str],
) -> tuple[list[str], dict[str, list[str]]]:
    """Widen tags to every stored MECHANICAL variant of each requested tag.

    Args:
        db: Session (caller must already have authorized the context).
        workspace_id: Authorized workspace.
        context_id: Authorized context.
        user_id: Caller identity (scopes the vocabulary read).
        tags: Tags the caller filtered on.

    Returns:
        ``(expanded, added_by_tag)``. ``expanded`` preserves the caller's tags
        (even ones absent from the vocabulary, so the filter never silently
        becomes broader-but-different) plus the variants found.
        ``added_by_tag`` maps each requested tag to the extra spellings it
        gained, for telemetry and the response hint; empty when nothing drifted.

    Note:
        Returns the input unchanged if the vocabulary read fails — widening is
        an enhancement and must never break a recall.
    """
    try:
        # #1512: widening only needs the mechanical variants, so a vocabulary up
        # to VOCABULARY_CACHE_TTL_SECONDS stale is harmless here — unlike
        # suggest_tags, this runs on EVERY tags_normalize=true recall, not only
        # on a zero-result one.
        vocabulary = await fetch_vocabulary_cached(
            db, workspace_id=workspace_id, context_id=context_id, user_id=user_id
        )
    except Exception as e:  # noqa: BLE001 — enhancement must not break recall
        logger.warning("tag_vocabulary_read_failed", error=str(e))
        return tags, {}

    by_normalized: dict[str, list[str]] = {}
    for stored in vocabulary:
        folded = normalize_tag(stored)
        if folded:
            by_normalized.setdefault(folded, []).append(stored)

    expanded: list[str] = []
    seen: set[str] = set()
    added_by_tag: dict[str, list[str]] = {}

    for requested in tags:
        if requested not in seen:
            expanded.append(requested)
            seen.add(requested)
        folded = normalize_tag(requested)
        if not folded:
            continue
        variants = [v for v in by_normalized.get(folded, []) if v != requested][
            :MAX_EXPANSION_PER_TAG
        ]
        gained = []
        for variant in variants:
            if variant not in seen:
                expanded.append(variant)
                seen.add(variant)
                gained.append(variant)
        if gained:
            added_by_tag[requested] = gained

    return expanded, added_by_tag


async def suggest_tags(
    db: AsyncSession,
    *,
    workspace_id: UUID,
    context_id: UUID,
    user_id: str,
    tags: list[str],
) -> dict[str, list[str]]:
    """Near-duplicate tags that exist in the context, for a filter that matched nothing.

    Args:
        db: Session (caller must already have authorized the context).
        workspace_id: Authorized workspace.
        context_id: Authorized context.
        user_id: Caller identity (scopes the vocabulary read).
        tags: Tags the caller filtered on.

    Returns:
        ``{requested_tag: ["stored-tag (count)", ...]}``, omitting requested
        tags with no near-duplicate. Empty dict when nothing is close — which is
        itself the signal that the topic genuinely is not stored, rather than
        misspelled.
    """
    try:
        vocabulary = await fetch_vocabulary(
            db, workspace_id=workspace_id, context_id=context_id, user_id=user_id
        )
    except Exception as e:  # noqa: BLE001 — a hint must not break recall
        logger.warning("tag_vocabulary_read_failed", error=str(e))
        return {}

    suggestions: dict[str, list[str]] = {}
    for requested in tags:
        hits = [
            f"{stored} ({count})"
            for stored, count in vocabulary.items()
            if stored != requested and is_near_duplicate(requested, stored)
        ][:MAX_SUGGESTIONS_PER_TAG]
        if hits:
            suggestions[requested] = hits
    return suggestions
