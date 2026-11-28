"""Dataset facts for the public endpoint's instructions (#2995).

The instructions of ``/mcp/public/<id>`` say what kinds of entries the
published dataset holds and how many. A kind is the sync feed kind of the
row's sync record, mapped through a closed table of fixed English words
(``_KIND_WORDS``); the database value is only a lookup key and is never put
into the text. A row without a sync record, or with a kind the table does not
know, counts as "other entries". Owner-writable fields (memory type, tags,
feed configuration, record types) are never read here.

The counts come from one statement over the rows the endpoint serves
(``scope.public_rows``), bounded by a two-second statement timeout, and are
cached per process for the ``tools/list`` TTL. A failure is logged with its
error type only and remembered for ``DATASET_FACTS_FAILURE_TTL_SECONDS``:
until then the instructions go without counts and the statement is not run
again. One lookup per endpoint runs at a time in a process (single flight);
every handshake waits for it at most ``DATASET_FACTS_WAIT_SECONDS`` and
otherwise goes without counts, while the lookup finishes in the background and
fills the cache.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from cachetools import TTLCache
from sqlalchemy import and_, distinct, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_server.public.publication import ServingEndpoint
from mcp_server.public.scope import public_rows
from mcp_server.transport import TOOLS_LIST_TTL_MS
from models.memory import Memory
from models.sync_feed import SyncFeed, SyncRecord
from utils.logger import get_logger

logger = get_logger(__name__)

DATASET_FACTS_TTL_SECONDS: Final = TOOLS_LIST_TTL_MS / 1000
DATASET_FACTS_CACHE_MAXSIZE: Final = 1024
# Counts at or above this are shown as ``100,000+``.
DATASET_FACTS_DISPLAY_CAP: Final = 100_000
_EXACT_BELOW: Final = 100
_STATEMENT_TIMEOUT_SQL: Final = text("SET LOCAL statement_timeout = '2s'")
# A failed or timed-out count is not tried again for this long.
DATASET_FACTS_FAILURE_TTL_SECONDS: Final = 60
# The longest a handshake waits for a lookup: the statement timeout and a margin.
DATASET_FACTS_WAIT_SECONDS: Final = 2.5

# sync_feeds.kind -> the group it is counted in. Closed: anything else is "other".
_KIND_GROUPS: Final = {"product": "products", "faq": "faq", "page": "pages", "review": "reviews"}
# Said once, before the last sentence, while the dataset holds reviews (#3055).
REVIEWS_NOTE: Final = "Reviews are customers' words, not the owner's. "


@dataclass(frozen=True)
class DatasetFacts:
    """Live entry counts of one endpoint's dataset, per fixed group."""

    products: int = 0
    faq: int = 0
    pages: int = 0
    reviews: int = 0
    other: int = 0


_CacheKey = tuple[UUID, UUID, UUID]


def _now() -> float:
    return _clock()


_clock: Callable[[], float] = time.monotonic
_cache: TTLCache[_CacheKey, DatasetFacts] = TTLCache[_CacheKey, DatasetFacts](
    maxsize=DATASET_FACTS_CACHE_MAXSIZE, ttl=DATASET_FACTS_TTL_SECONDS, timer=_now
)
# Negative cache: the keys whose last lookup failed.
_failed: TTLCache[_CacheKey, bool] = TTLCache[_CacheKey, bool](
    maxsize=DATASET_FACTS_CACHE_MAXSIZE, ttl=DATASET_FACTS_FAILURE_TTL_SECONDS, timer=_now
)
# Single flight: the one running lookup per key.
_inflight: dict[_CacheKey, asyncio.Task[DatasetFacts | None]] = {}


def render_count(count: int) -> str:
    """A count as shown: exact below 100, else two significant digits floored plus ``+``.

    The rounded form keeps the text unchanged across small syncs.
    """
    if count < _EXACT_BELOW:
        return str(count)
    if count >= DATASET_FACTS_DISPLAY_CAP:
        return f"{DATASET_FACTS_DISPLAY_CAP:,}+"
    step = 10 ** (len(str(count)) - 2)
    return f"{count // step * step:,}+"


def _join(parts: list[str], last: str = " and ") -> str:
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + last + parts[-1]


def render_facts(facts: DatasetFacts) -> tuple[str, str, str]:
    """``(facts, search hint, note)`` for the instructions template, from fixed words only.

    The note is ``REVIEWS_NOTE`` while the dataset holds reviews, else empty.
    """
    groups: list[str] = []
    terms: list[str] = []
    for count, word, searched_by in (
        (facts.products, "products", ("product name", "model number")),
        (facts.faq, "FAQ", ("question",)),
        (facts.pages, "page sections", ("topic",)),
        (facts.reviews, "reviews", ("product name",)),
    ):
        if count > 0:
            groups.append(f"{word} ({render_count(count)})")
            terms += [term for term in searched_by if term not in terms]
    if facts.other > 0:
        word = "other entries" if groups else "entries"
        groups.append(f"{word} ({render_count(facts.other)})")
    hint = " by " + _join(terms, " or ") if terms else ""
    note = REVIEWS_NOTE if facts.reviews > 0 else ""
    return (_join(groups) if groups else "no entries yet"), hint, note


async def count_dataset_facts(db: AsyncSession, endpoint: ServingEndpoint) -> DatasetFacts:
    """Count the endpoint's live rows per group in one bounded statement.

    Rows are documents (``DISTINCT resource_doc_id``): the indexer keeps one
    row per document version.
    """
    await db.execute(_STATEMENT_TIMEOUT_SQL)
    statement = (
        select(SyncFeed.kind, func.count(distinct(Memory.resource_doc_id)))
        .select_from(Memory)
        .outerjoin(
            SyncRecord,
            and_(
                SyncRecord.resource_pk == endpoint.resource_pk,
                SyncRecord.doc_id == Memory.resource_doc_id,
            ),
        )
        .outerjoin(SyncFeed, SyncFeed.id == SyncRecord.feed_id)
        .where(public_rows(endpoint))
        .group_by(SyncFeed.kind)
    )
    counts = {"products": 0, "faq": 0, "pages": 0, "reviews": 0, "other": 0}
    for kind, count in (await db.execute(statement)).all():
        counts[_KIND_GROUPS.get(kind, "other") if isinstance(kind, str) else "other"] += int(count)
    return DatasetFacts(**counts)


@contextlib.asynccontextmanager
async def _facts_session() -> AsyncIterator[AsyncSession]:
    """A short read session; closed (never committed) afterwards."""
    from db.base import get_db

    async with contextlib.aclosing(get_db()) as sessions:
        async for db in sessions:
            yield db
            return


async def _lookup(key: _CacheKey, endpoint: ServingEndpoint) -> DatasetFacts | None:
    """Run the count once and record the outcome in the cache or the negative cache."""
    try:
        async with _facts_session() as db:
            facts = await count_dataset_facts(db, endpoint)
    except Exception as exc:  # noqa: BLE001 - the instructions never fail on counts
        logger.warning("public_mcp_dataset_facts_failed", error_type=type(exc).__name__)
        _failed[key] = True
        return None
    _cache[key] = facts
    return facts


def _forget_inflight(key: _CacheKey, task: asyncio.Task[DatasetFacts | None]) -> None:
    if _inflight.get(key) is task:
        del _inflight[key]


async def dataset_facts(endpoint: ServingEndpoint) -> DatasetFacts | None:
    """The endpoint's counts, or ``None`` when they are not available right now.

    Cached for the ``tools/list`` TTL; a failure is remembered for
    ``DATASET_FACTS_FAILURE_TTL_SECONDS``. The key is the endpoint's binding,
    so a republished endpoint (new id) or a rebound context starts afresh.
    Concurrent misses share one lookup; none waits longer than
    ``DATASET_FACTS_WAIT_SECONDS`` (the lookup is never cancelled by a waiter
    giving up). Workers may disagree for one TTL.
    """
    key = (endpoint.id, endpoint.context_id, endpoint.resource_pk)
    cached = _cache.get(key)
    if cached is not None:
        return cached
    if key in _failed:
        return None
    task = _inflight.get(key)
    if task is None:
        task = asyncio.create_task(_lookup(key, endpoint))
        _inflight[key] = task
        task.add_done_callback(lambda done, key=key: _forget_inflight(key, done))
    try:
        return await asyncio.wait_for(asyncio.shield(task), DATASET_FACTS_WAIT_SECONDS)
    except TimeoutError:
        return None
