"""Close the content chunk gaps a context collects while its chunks are inactive (#2107).

Under the plan gate (#2103, ``services.content_chunk_policy``) a context can
keep ``content_chunks_enabled`` on and still be inactive: its plan lacks the
feature, or it ranks past the plan's cap. Its writes then keep no chunks: a
new long memory gets none, an edited one loses its old ones (they would hold
the old text). An embedding switch changes the collection its chunks are read
from, and the new one may have none (the migration rebuilt none while the
context was inactive) or chunks of an older text. When the context becomes
active again — an upgrade, another context turning chunks off or being
deleted, a larger cap — those memories would stay without chunks until their
next content edit.

Two halves, so no transition (plan change, cap change, a sibling turned off
or deleted, a restore, the gate turned on) needs a hook of its own:

- **Stamp** where a gap is made (:func:`record_content_chunk_gap`):
  ``context_search_configs.content_chunks_gap_at``. The chunk writer stamps
  when it keeps no current chunks for a long memory of an inactive context,
  a merge when it copies long memories into one, and — where the deployment
  gates content chunks by plan — the embedding switch whenever the flag is
  stored on. Throttled to one write a minute per context, committed on its
  own.
- **Reconcile** (:func:`reconcile_content_chunk_gaps`, a scheduler job beside
  the embedding sweep): for each stamped context that is active NOW, read the
  first chunk point of its long rows in the collection it is served from and
  re-queue (:func:`queue_content_chunk_backfill`, the #2015 D4 backfill) the
  rows with none or with chunks of another text. A few contexts and a page of
  rows per tick, behind a backlog check, so the embedding provider sees the
  re-queued rows at the sweep's pace. The stamp is cleared when the scan ends,
  only if no new gap moved it meanwhile.

Turning the flag on or off clears the stamp (on: the D4 backfill re-queues
every long row; off: the chunks are purged). Gaps made before #2107 left no
stamp: :func:`seed_content_chunk_gaps` stamps the flag-on contexts once, at
the scheduler leader's start, where the deployment gates content chunks.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from db.base import isolated_session
from db.qdrant import chunk_points_content_hashes, chunk_points_supported
from models.auth import Context
from models.config import ConfigOverride, ContextSearchConfig
from models.memory import Memory
from services.content_chunk_policy import content_chunks_effective_ids, content_chunks_gated
from utils.content_chunks import CONTENT_CHUNK_MIN_CONTENT_CHARS, content_hash, split_content
from utils.logger import get_logger

logger = get_logger(__name__)

#: A stamp is written again only when the stored one is older than this, so a
#: busy inactive context writes its config row at most twice a minute.
GAP_STAMP_REFRESH_SECONDS = 30
#: A stamp is reconciled only once it is older than this. It must exceed the
#: refresh: a gap made while a scan runs then always moves the stamp, and the
#: scan's fenced clear (``gap_at = <read value>``) leaves it for the next pass.
#: The margin (settle minus refresh) is also how long the transaction that made
#: a gap the throttle did not re-stamp may stay open (a Sleep run is one
#: transaction): a row it has not committed yet is invisible to the scan.
GAP_SETTLE_SECONDS = 600
#: Contexts reconciled per tick.
RECONCILE_CONTEXTS_PER_TICK = 5
#: Memory ids of one context read per tick (cheap columns; one keyset page).
RECONCILE_SCAN_BATCH = 1000
#: Of those, how many have their content and chunk point 0 read at a time.
RECONCILE_CHECK_BATCH = 100
#: Long rows of one context waiting for the sweep at which it is skipped this
#: tick; what a tick re-queues is capped to the room left below it.
RECONCILE_BACKLOG_LIMIT = 100
#: Rows of the whole deployment waiting for the sweep (``pending`` /
#: ``processing``) at which a tick re-queues nothing; a tick re-queues at most
#: the room left below it, so fresh writes are not queued behind a backfill.
RECONCILE_GLOBAL_BACKLOG_LIMIT = 200
#: Stamped contexts read per tick, in context id order from where the last
#: tick stopped, before the active ones are picked.
STAMPED_READ_LIMIT = 500
#: The ``config_overrides`` key that records the one-time startup stamp
#: (:func:`seed_content_chunk_gaps`). Nothing reads ``config_overrides`` as
#: settings at runtime (#1580); the key is namespaced so it cannot be taken
#: for one.
GAP_SEED_MARKER_KEY = "marker:content_chunks_gap_seed"
#: How long a stamp, or the reconciler's re-queue, waits for a row lock.
_LOCK_TIMEOUT = text("SET LOCAL lock_timeout = '5s'")


def _stamp_values() -> dict[str, Any]:
    """The values a stamp writes: now, a fresh walk, and ``updated_at`` kept.

    ``updated_at`` is written back as itself: the embedding rollback falls
    back to it as the switch time (#2098), and the ORM's ``onupdate`` would
    move it otherwise.
    """
    return {
        "content_chunks_gap_at": func.clock_timestamp(),
        "content_chunks_gap_cursor": None,
        "updated_at": ContextSearchConfig.updated_at,
    }


def _seconds(seconds: int) -> Any:
    """``seconds`` as an SQL interval (a bound ``make_interval``)."""
    return func.make_interval(0, 0, 0, 0, 0, 0, float(seconds))


def _long_plain_live(context_id: Any) -> list[Any]:
    """The D4 backfill's rows: the context's live long memories that are not resource rows."""
    return [
        Memory.context_id == context_id,
        Memory.deleted_at.is_(None),
        func.char_length(Memory.content) >= CONTENT_CHUNK_MIN_CONTENT_CHARS,
        Memory.resource_id.is_(None),
    ]


async def queue_content_chunk_backfill(
    db: AsyncSession,
    context_id: UUID,
    *,
    memory_ids: Sequence[UUID] | None = None,
    include_in_flight: bool = False,
) -> int:
    """Re-queue the context's long ``success`` memories for chunking (#2015 D4, #2107).

    One UPDATE, in the caller's transaction, locking its rows in id order
    (an ordered ``FOR NO KEY UPDATE`` subquery): every live ``success`` row of the
    context whose content is long enough to split — or, with ``memory_ids``,
    those of them — goes back to ``pending``, and the embedding sweep rebuilds
    it (summary point and chunk points) under the usual claim and retry. The
    row keeps its summary point meanwhile, so it stays searchable. ``pending``
    / ``failed`` rows are picked up anyway and are left alone. Neither
    ``embedding_attempted_at`` (the pipeline's clock) nor ``updated_at`` (an
    edit's cue) is written. Resource rows (``resource_id``) never get chunks
    and are left out.

    ``include_in_flight`` (#2095): move ``processing`` rows too. The enable
    passes it: a run in flight read the chunk flag before the enable
    committed, so it writes no chunks and would stamp ``success`` without
    them. Moving its row to ``pending`` fences that run off (its renewal, its
    chunk checks and its outcome writes all match on ``processing``), and the
    sweep embeds the row again with the flag on. No retry budget is spent.
    The gap reconciler (``memory_ids``) leaves it off: its flag was already on.

    Returns:
        How many rows were queued (0 on a store without chunk points or for
        an empty ``memory_ids``).
    """
    if not chunk_points_supported() or (memory_ids is not None and not memory_ids):
        return 0
    status = (
        Memory.embedding_status.in_(("success", "processing"))
        if include_in_flight
        else Memory.embedding_status == "success"
    )
    conditions = [*_long_plain_live(context_id), status]
    if memory_ids is not None:
        conditions.append(Memory.id.in_(list(memory_ids)))
    locked = (
        select(Memory.id).where(*conditions).order_by(Memory.id).with_for_update(key_share=True)
    )
    result = await db.execute(
        update(Memory)
        .where(Memory.id.in_(locked))
        .values(embedding_status="pending")
        .execution_options(synchronize_session=False)
    )
    return int(getattr(result, "rowcount", 0) or 0)


async def count_long_rows_waiting(
    db: AsyncSession, context_id: UUID, *, include_failed: bool
) -> int:
    """How many of the context's live long non-resource rows are not ``success`` yet.

    ``include_failed``: count ``failed`` rows too (what the search-config
    response serves as ``content_chunks_backfill_pending``); without it only
    the rows the sweep is about to embed (``pending`` / ``processing``).
    """
    status = (
        Memory.embedding_status != "success"
        if include_failed
        else Memory.embedding_status.in_(("pending", "processing"))
    )
    result = await db.execute(
        select(func.count()).select_from(Memory).where(*_long_plain_live(context_id), status)
    )
    return int(result.scalar_one() or 0)


async def record_content_chunk_gap(db: AsyncSession, context_id: Any, *, reason: str) -> bool:
    """Stamp a content chunk gap on the context (``content_chunks_gap_at``).

    Writes ``clock_timestamp()`` and resets the reconciler's cursor, unless
    the stored stamp is younger than :data:`GAP_STAMP_REFRESH_SECONDS` (one
    write a minute at most). ``updated_at`` is kept: the embedding rollback
    falls back to it as the switch time where no ``embedding_switched_at``
    was recorded (#2098).

    The due check reads on the caller's session (a SAVEPOINT, no lock), so
    most calls of a busy inactive context stop there. The write commits on its
    own session (``isolated_session``): its row lock is not held for the rest
    of the caller's transaction — a Sleep run is one long transaction — and a
    caller that rolls back after the chunk delete it stamps for does not take
    the stamp with it. It waits at most 5 s for the row's lock; the writers
    that hold it longer either clear the stamp anyway (a flag change) or
    stamp it themselves (a gated embedding switch). Fail-open: a failure is
    logged (error class only) and the caller's transaction stays usable; the
    memory's next write stamps again.

    Returns:
        True when the stamp was written.
    """
    if context_id is None:
        return False
    stamp = ContextSearchConfig.content_chunks_gap_at
    due = or_(
        stamp.is_(None),
        stamp < func.clock_timestamp() - _seconds(GAP_STAMP_REFRESH_SECONDS),
    )
    try:
        async with db.begin_nested():
            needed = await db.scalar(
                select(ContextSearchConfig.context_id).where(
                    ContextSearchConfig.context_id == context_id, due
                )
            )
        if needed is None:
            return False
        async with isolated_session(db) as session:
            await session.execute(_LOCK_TIMEOUT)
            result = await session.execute(
                update(ContextSearchConfig)
                .where(ContextSearchConfig.context_id == context_id, due)
                .values(**_stamp_values())
                .execution_options(synchronize_session=False)
            )
            await session.commit()
    except Exception as e:  # noqa: BLE001 — fail-open, logged
        logger.warning(
            "content_chunks_gap_stamp_failed",
            context_id=str(context_id),
            reason=reason,
            error_class=type(e).__name__,
        )
        return False
    written = int(getattr(result, "rowcount", 0) or 0) > 0
    if written:
        logger.info("content_chunks_gap_recorded", context_id=str(context_id), reason=reason)
    return written


async def seed_content_chunk_gaps(db: AsyncSession) -> int | None:
    """Stamp, once per deployment, the contexts whose earlier gaps were never recorded.

    Gaps made before #2107 left no stamp. Where the deployment gates content
    chunks by plan (``CONTENT_CHUNKS_REQUIRE_PLAN``) every context with the
    flag on and no stamp yet is stamped, so each gets one reconcile pass once
    active (chunk point reads; only rows without current chunks are
    re-queued). Without the gate no context is ever inactive and nothing is
    done — and nothing is recorded, so turning the gate on later stamps at the
    next start.

    One-time: the marker row (:data:`GAP_SEED_MARKER_KEY` in
    ``config_overrides``) is inserted in the same transaction as the stamps,
    ``ON CONFLICT DO NOTHING``; a later start, or a second leader racing this
    one, finds it and stamps nothing. The stamps lock their rows in context id
    order and wait at most 5 s per lock; ``updated_at`` is kept (the
    embedding rollback falls back to it). Commits; a failure raises with
    nothing written, and the caller retries.

    Returns:
        How many contexts were stamped, or None when nothing was to be done
        (gate off, no chunk points, or already done).
    """
    if not content_chunks_gated() or not chunk_points_supported():
        return None
    marker = await db.execute(
        pg_insert(ConfigOverride)
        .values(key=GAP_SEED_MARKER_KEY, value="done")
        .on_conflict_do_nothing(index_elements=["key"])
        .returning(ConfigOverride.id)
    )
    if marker.scalar_one_or_none() is None:
        await db.commit()
        return None
    # Rows locked in context id order, with a bounded wait: a timeout rolls
    # the marker back with the stamps, and the next tick tries again.
    await db.execute(_LOCK_TIMEOUT)
    locked = (
        select(ContextSearchConfig.context_id)
        .where(
            ContextSearchConfig.content_chunks_enabled.is_(True),
            ContextSearchConfig.content_chunks_gap_at.is_(None),
        )
        .order_by(ContextSearchConfig.context_id)
        .with_for_update(key_share=True)
    )
    result = await db.execute(
        update(ContextSearchConfig)
        .where(ContextSearchConfig.context_id.in_(locked))
        .values(**_stamp_values())
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    stamped = int(getattr(result, "rowcount", 0) or 0)
    logger.info("content_chunks_gap_seeded", contexts=stamped)
    return stamped


@dataclass
class GapReconcileResult:
    """What one reconcile tick did (for logs and tests)."""

    stamped: int = 0
    active: int = 0
    scanned: int = 0
    requeued: int = 0
    cleared: int = 0
    deferred: int = 0
    failed: int = 0


@dataclass
class _Rotation:
    """Where the next tick starts reading stamped contexts (context id order).

    Process-local: the job runs on the scheduler leader only, and a new
    leader starting from the beginning costs one extra round.
    """

    after: UUID | None = None


_rotation = _Rotation()


async def _stamped_contexts(
    db: AsyncSession, after: UUID | None
) -> list[tuple[UUID, datetime, UUID | None]]:
    """Settled stamps of live contexts with the flag stored on, by context id after ``after``."""
    stmt = (
        select(
            ContextSearchConfig.context_id,
            ContextSearchConfig.content_chunks_gap_at,
            ContextSearchConfig.content_chunks_gap_cursor,
        )
        .join(Context, Context.id == ContextSearchConfig.context_id)
        .where(
            ContextSearchConfig.content_chunks_gap_at.is_not(None),
            ContextSearchConfig.content_chunks_gap_at
            <= func.clock_timestamp() - _seconds(GAP_SETTLE_SECONDS),
            ContextSearchConfig.content_chunks_enabled.is_(True),
            Context.deleted_at.is_(None),
        )
    )
    if after is not None:
        stmt = stmt.where(ContextSearchConfig.context_id > after)
    rows = await db.execute(stmt.order_by(ContextSearchConfig.context_id).limit(STAMPED_READ_LIMIT))
    # gap_at IS NOT NULL in the WHERE above.
    return cast("list[tuple[UUID, datetime, UUID | None]]", list(rows.tuples().all()))


async def _global_backlog(db: AsyncSession) -> int:
    """Rows of the deployment waiting for the sweep (the unsettled partial index)."""
    result = await db.execute(
        select(func.count())
        .select_from(Memory)
        .where(
            Memory.deleted_at.is_(None),
            Memory.embedding_status.in_(("pending", "processing")),
        )
    )
    return int(result.scalar_one() or 0)


async def _move_stamp(
    db: AsyncSession, context_id: UUID, gap_at: datetime, *, cursor: UUID | None, clear: bool
) -> bool:
    """Advance the cursor, or clear the stamp, only if the stamp is still ``gap_at``.

    A gap stamped since (the context went inactive again) moved the stamp and
    reset the cursor; then nothing is written and the next pass starts over.
    """
    values: dict[str, Any] = {
        "content_chunks_gap_cursor": None if clear else cursor,
        "updated_at": ContextSearchConfig.updated_at,
    }
    if clear:
        values["content_chunks_gap_at"] = None
    result = await db.execute(
        update(ContextSearchConfig)
        .where(
            ContextSearchConfig.context_id == context_id,
            ContextSearchConfig.content_chunks_gap_at == gap_at,
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return int(getattr(result, "rowcount", 0) or 0) > 0


async def _without_current_chunks(db: AsyncSession, ids: list[UUID], collection: str) -> list[UUID]:
    """Of ``ids`` (one check batch), the long rows whose chunk point 0 is missing or stale.

    The content is read for these ids only, and the read transaction ends
    before the vector store call.
    """
    rows = (
        await db.execute(
            select(Memory.id, Memory.content).where(
                Memory.id.in_(ids),
                func.char_length(Memory.content) >= CONTENT_CHUNK_MIN_CONTENT_CHARS,
            )
        )
    ).all()
    await db.commit()
    digests = {
        memory_id: content_hash(content) for memory_id, content in rows if split_content(content)
    }
    if not digests:
        return []
    hashes = await chunk_points_content_hashes(list(digests), collection)
    missing: list[UUID] = []
    for memory_id, digest in digests.items():
        key = str(memory_id)
        if key not in hashes:
            missing.append(memory_id)
        # A chunk point without a hash predates the hash (#2091); an edit since,
        # in the collection then routed, rewrote it (active) or deleted it
        # (inactive). Rolled-back routes re-queue their own delta.
        elif hashes[key] is not None and hashes[key] != digest:
            missing.append(memory_id)
    return sorted(missing)


async def _reconcile_context(
    db: AsyncSession,
    context_id: UUID,
    gap_at: datetime,
    cursor: UUID | None,
    out: GapReconcileResult,
    *,
    global_room: int,
) -> int:
    """One page of one active stamped context: re-queue the rows without current chunks.

    Re-queues at most ``global_room`` rows and the room below the context's
    own backlog limit. Returns how many rows it re-queued.
    """
    from services.context_routing import resolve_collection_name

    waiting = await count_long_rows_waiting(db, context_id, include_failed=False)
    room = min(RECONCILE_BACKLOG_LIMIT - waiting, global_room)
    if room <= 0:
        await db.commit()
        out.deferred += 1
        return 0
    collection = await resolve_collection_name(db, context_id)
    # Cheap columns only: the length test (which reads the content) runs per
    # check batch below, not over the whole context on every page.
    stmt = select(Memory.id).where(
        Memory.context_id == context_id,
        Memory.deleted_at.is_(None),
        Memory.resource_id.is_(None),
        Memory.embedding_status == "success",
    )
    if cursor is not None:
        stmt = stmt.where(Memory.id > cursor)
    page = list(
        (await db.execute(stmt.order_by(Memory.id).limit(RECONCILE_SCAN_BATCH))).scalars().all()
    )
    await db.commit()
    out.scanned += len(page)

    missing: list[UUID] = []
    next_cursor = page[-1] if page else cursor
    done = len(page) < RECONCILE_SCAN_BATCH
    for i in range(0, len(page), RECONCILE_CHECK_BATCH):
        missing.extend(
            await _without_current_chunks(db, page[i : i + RECONCILE_CHECK_BATCH], collection)
        )
        if len(missing) >= room:
            # The backlog room is spent: resume right after the last re-queued
            # row, so the rest of this page is checked again next time.
            missing = missing[:room]
            next_cursor = missing[-1]
            done = False
            break
    requeued = 0
    if missing:
        # Bounded wait behind a row another writer holds (a Sleep run): a
        # timeout fails this context for the tick, stamp and cursor kept.
        await db.execute(_LOCK_TIMEOUT)
        requeued = await queue_content_chunk_backfill(db, context_id, memory_ids=missing)
        await db.commit()
        out.requeued += requeued
        logger.info(
            "content_chunks_gap_requeued",
            context_id=str(context_id),
            collection=collection,
            scanned=len(page),
            requeued=requeued,
        )

    moved = await _move_stamp(db, context_id, gap_at, cursor=next_cursor, clear=done)
    if done and moved:
        out.cleared += 1
        logger.info("content_chunks_gap_closed", context_id=str(context_id))
    return requeued


async def reconcile_content_chunk_gaps(db: AsyncSession) -> GapReconcileResult:
    """One tick: close the gaps of stamped contexts whose chunks are active now (#2107).

    Reads up to :data:`STAMPED_READ_LIMIT` settled stamps (older than
    :data:`GAP_SETTLE_SECONDS`) of live contexts with the flag stored on, in
    context id order from where the last tick stopped, keeps the active ones
    (one query, ``content_chunks_effective_ids``: store, plan and cap;
    inactive ones keep their stamp), and reconciles up to
    :data:`RECONCILE_CONTEXTS_PER_TICK` of them: one page of
    :data:`RECONCILE_SCAN_BATCH` rows each, from their cursor. The read goes
    round all stamped contexts, so contexts that stay inactive, or one that
    keeps failing or waiting on its backlog, never hold the others back. A
    context whose backlog of long rows waiting for the sweep is at
    :data:`RECONCILE_BACKLOG_LIMIT` is skipped this tick, and the tick
    re-queues no more than the room below :data:`RECONCILE_GLOBAL_BACKLOG_LIMIT`
    (the deployment's rows waiting for the sweep). Idempotent: a row
    whose first chunk point carries the hash of its current content is never
    re-queued, and the re-queue moves only ``success`` rows. A context that
    fails is logged and retried later; the others go on.

    A no-op on a store without chunk points.
    """
    out = GapReconcileResult()
    if not chunk_points_supported():
        return out
    stamped = await _stamped_contexts(db, _rotation.after)
    if not stamped and _rotation.after is not None:
        _rotation.after = None
        stamped = await _stamped_contexts(db, None)
    out.stamped = len(stamped)
    if not stamped:
        await db.commit()
        return out
    active = await content_chunks_effective_ids(db, [row[0] for row in stamped])
    await db.commit()
    global_room = RECONCILE_GLOBAL_BACKLOG_LIMIT - await _global_backlog(db)
    await db.commit()
    candidates = [row for row in stamped if row[0] in active]
    picked = candidates[:RECONCILE_CONTEXTS_PER_TICK]
    if len(candidates) > len(picked):
        _rotation.after = picked[-1][0]
    elif len(stamped) >= STAMPED_READ_LIMIT:
        _rotation.after = stamped[-1][0]
    else:
        _rotation.after = None
    out.active = len(picked)
    for context_id, gap_at, cursor in picked:
        try:
            global_room -= await _reconcile_context(
                db, context_id, gap_at, cursor, out, global_room=global_room
            )
        except Exception as e:  # noqa: BLE001 — one context must not stop the others
            await db.rollback()
            out.failed += 1
            logger.warning(
                "content_chunks_gap_reconcile_failed",
                context_id=str(context_id),
                error_class=type(e).__name__,
            )
    return out
