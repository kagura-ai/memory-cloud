"""One-shot supersede detection for rows the pipeline never checked (#2256).

The post-embed supersede detection runs inside the embedding pipeline only,
and since #2092 a re-embed of unchanged text runs none. Rows embedded before
the detection existed (#1403), before the judge was on (#2016), or while the
judge was capped carry no candidate and will never be checked unless their
text changes. This pass runs the same detection in process for one context:
the row's stored dense vector is read back from the vector store (no
embedding call) and handed to ``_create_knn_seed_edges`` with
``mechanical_reembed=False, seed=False`` — the detection and #2059's clear,
no seed edge, no point write.

Bounded by ``limit`` rows and by ``max_judge_calls``: the workspace's judge
counter (the Redis key the daily cap counts on) is read before each row and
the run stops once it has advanced that much. The pass shares the daily cap
with live ingest; past the cap a pair at 0.85 or above is stored unverified
and a pair below is skipped, as on ingest. A row whose nearest neighbour is
not near enough stays without a candidate and is planned again by a run over
the same range (nothing records "checked"); a run reports the creation time
of the last row it reached, and ``after`` resumes past it.

Run by hand (``python -m src.cli.supersede_catch_up``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Select, Text, and_, func, literal, or_, select, tuple_
from sqlalchemy import cast as sql_cast
from sqlalchemy.ext.asyncio import AsyncSession

from models.memory import Memory
from utils.datetime import to_utc_iso
from utils.logger import get_logger

logger = get_logger(__name__)

# Vectors read per vector-store round trip.
VECTOR_BATCH = 64

STOPPED_JUDGE_BUDGET = "judge_budget"
STOPPED_SUGGESTIONS_OFF = "suggestions_off"


@dataclass
class SupersedeCatchUp:
    """What a run would check (plan) or did (apply).

    ``judge_calls`` is how far the workspace's judge counter advanced during
    the run (``None`` when the counter could not be read). It counts every
    judge attempt in the workspace in that time — live ingest included, and
    attempts past the daily cap, which are counted but not billed.
    """

    dry_run: bool
    eligible: int = 0  # rows matching the filter, before ``limit``
    planned: int = 0  # rows this run checks (at most ``limit``)
    processed: int = 0  # rows the detection ran on
    detected: int = 0  # a live candidate appeared
    updated: int = 0  # a live candidate was stored again (new target or verdict)
    cleared: int = 0  # a live candidate was removed
    unchanged: int = 0
    failed: int = 0  # the detection failed (logged as knn_seeding_failed)
    no_vector: int = 0  # no stored dense vector for the row's point
    # the row changed after the plan (gone, re-queued, got a candidate, or its
    # text / embedding moved after its vector was read)
    skipped: int = 0
    judge_calls: int | None = None
    stopped: str | None = None  # STOPPED_* or None
    # ``(created_at, id)`` of the last row reached; pass them as
    # ``after`` / ``after_id`` to resume (the rows' order).
    last_created_at: datetime | None = None
    last_id: UUID | None = None


def _candidate_predicate(include_unverified: bool) -> Any:
    """Rows whose ``supersede_candidate`` says nothing was ever detected.

    "No candidate" is SQL NULL or the JSON ``null`` the ORM also reads as
    None (#2022). With ``include_unverified`` also a live candidate without a
    verdict (stored before the judge was on, or past its cap). The column is
    ``NullableJSON`` (json, not jsonb): ``->>`` only, never ``?``.
    """
    column = Memory.supersede_candidate
    no_candidate = or_(column.is_(None), sql_cast(column, Text) == "null")
    if not include_unverified:
        return no_candidate
    unverified = and_(
        column["memory_id"].as_string().is_not(None),
        column["verification"].as_string().is_(None),
    )
    return or_(no_candidate, unverified)


_NIL_UUID = UUID(int=0)


def _row_filter(
    context_id: UUID,
    *,
    before: datetime | None,
    include_unverified: bool,
    after: datetime | None = None,
    after_id: UUID | None = None,
) -> list[Any]:
    clauses: list[Any] = [
        Memory.context_id == context_id,
        Memory.deleted_at.is_(None),
        Memory.embedding_suppressed_at.is_(None),
        Memory.resource_id.is_(None),
        Memory.embedding_status == "success",
        _candidate_predicate(include_unverified),
    ]
    if before is not None:
        clauses.append(Memory.created_at < before)
    if after is not None:
        # The resume cursor compares like the ORDER BY: rows created in the
        # same instant as the last one reached are not skipped. Without an
        # id the instant itself is included.
        clauses.append(
            tuple_(Memory.created_at, Memory.id)
            > tuple_(
                literal(after, Memory.created_at.type),
                literal(after_id or _NIL_UUID, Memory.id.type),
            )
        )
    return clauses


def catch_up_rows_query(
    context_id: UUID,
    *,
    before: datetime | None,
    include_unverified: bool,
    limit: int,
    after: datetime | None = None,
    after_id: UUID | None = None,
) -> Select[UUID, UUID | None, datetime]:
    """The rows a run checks, oldest first: ``(id, summary_embedding_id, created_at)``.

    Args:
        context_id: The context to walk.
        before: Only rows created before this naive-UTC instant.
        include_unverified: Also rows whose live candidate has no verdict.
        limit: At most this many rows.
        after: Resume past this ``(created_at, id)`` cursor (naive UTC).
        after_id: The cursor's id; without it rows created at ``after``
            itself are included.
    """
    return (
        select(Memory.id, Memory.summary_embedding_id, Memory.created_at)
        .where(
            *_row_filter(
                context_id,
                before=before,
                include_unverified=include_unverified,
                after=after,
                after_id=after_id,
            )
        )
        .order_by(Memory.created_at, Memory.id)
        .limit(limit)
    )


Revision = tuple[str | None, str]


def _revision(memory: Any) -> Revision:
    """The row's embedding stamp and the digest of its current text."""
    from services.supersede_verify import embedded_text_digest

    return (
        memory.embedded_text_digest,
        embedded_text_digest(memory.summary or "", memory.content or ""),
    )


async def _revisions(db: AsyncSession, ids: list[UUID]) -> dict[UUID, Revision]:
    """:func:`_revision` of each row, read before its vector is."""
    rows = (
        await db.execute(
            select(Memory.id, Memory.summary, Memory.content, Memory.embedded_text_digest).where(
                Memory.id.in_(ids)
            )
        )
    ).all()
    return {row.id: _revision(row) for row in rows}


def _verification(candidate: Any) -> Any:
    """The stored verdict of a candidate (``None`` when it has none)."""
    return candidate.get("verification") if isinstance(candidate, dict) else None


async def _read_counter(key: str) -> int | None:
    """The judge counter at ``key`` (0 when absent); ``None`` when unreadable."""
    from db.redis import get_redis_client

    try:
        value = await get_redis_client().get(key)
        return int(value or 0)
    except Exception as exc:  # noqa: BLE001 — reported by the caller, never raised
        logger.debug("supersede_catch_up_counter_read_failed", key=key, error=str(exc))
        return None


class _JudgeCounter:
    """How far a workspace's judge counter advanced since :meth:`start`.

    The counter is one Redis key per workspace per UTC day; a run that spans
    midnight carries the old day's advance over to the new key.
    """

    def __init__(self, workspace_id: str) -> None:
        self._workspace_id = workspace_id
        self._key = ""
        self._base = 0
        self._carried = 0

    async def start(self) -> bool:
        from services.supersede_verify import counter_key

        key = counter_key(self._workspace_id)
        value = await _read_counter(key)
        if value is None:
            return False
        self._key, self._base, self._carried = key, value, 0
        return True

    async def used(self) -> int | None:
        from services.supersede_verify import counter_key

        key = counter_key(self._workspace_id)
        if key != self._key:
            previous = await _read_counter(self._key)
            if previous is None:
                return None
            self._carried += previous - self._base
            self._key, self._base = key, 0
        value = await _read_counter(key)
        if value is None:
            return None
        return self._carried + value - self._base


async def run_supersede_catch_up(
    db: AsyncSession,
    context_id: UUID,
    *,
    dry_run: bool,
    before: datetime | None = None,
    include_unverified: bool = False,
    limit: int = 200,
    max_judge_calls: int = 50,
    after: datetime | None = None,
    after_id: UUID | None = None,
) -> SupersedeCatchUp:
    """Run the post-embed supersede detection on rows that never had it.

    Args:
        db: The session. Each detection write commits on its own; nothing is
            held across rows.
        context_id: The context to walk (required: no cross-context mode).
        dry_run: Count the rows and run nothing (no Redis read either).
        before: Only rows created before this naive-UTC instant.
        include_unverified: Also re-check rows whose live candidate has no
            verdict (stored before the judge was on, or past its cap).
        limit: At most this many rows.
        max_judge_calls: Stop once the workspace's judge counter advanced
            this much during the run.
        after: Resume past ``(after, after_id)``: the ``last_created_at``
            / ``last_id`` of an earlier run, to move past rows that stay
            without a candidate. Without ``after_id`` rows created at
            ``after`` itself are included.
        after_id: See ``after``.

    Returns:
        The tally.

    Raises:
        ValueError: the context does not exist.
        NotImplementedError: the vector store cannot return stored vectors.
    """
    from db.qdrant import get_collection_name, retrieve_dense_vectors
    from models.auth import Context
    from neural.config import NeuralMemoryConfig
    from services.context_routing import resolve_context_embedding
    from services.memory_service import _create_knn_seed_edges, _live_candidate_target

    workspace_id = (
        await db.execute(select(Context.workspace_id).where(Context.id == context_id))
    ).scalar_one_or_none()
    if workspace_id is None:
        raise ValueError(f"context {context_id} not found")

    filters = _row_filter(
        context_id,
        before=before,
        include_unverified=include_unverified,
        after=after,
        after_id=after_id,
    )
    eligible = int(
        (await db.execute(select(func.count()).select_from(Memory).where(*filters))).scalar_one()
    )
    rows = (
        await db.execute(
            catch_up_rows_query(
                context_id,
                before=before,
                include_unverified=include_unverified,
                limit=limit,
                after=after,
                after_id=after_id,
            )
        )
    ).all()
    result = SupersedeCatchUp(dry_run=dry_run, eligible=eligible, planned=len(rows))
    if dry_run or not rows:
        return result

    config = await NeuralMemoryConfig.from_db(db)
    if not config.supersede_suggest_enabled:
        result.stopped = STOPPED_SUGGESTIONS_OFF
        logger.warning(
            "supersede_catch_up_suggestions_off",
            context_id=str(context_id),
            hint="SUPERSEDE_SUGGEST_ENABLED is off; the detection would store nothing",
        )
        return result

    model, dims = await resolve_context_embedding(db, context_id)
    collection = get_collection_name(model, dims)

    counter = _JudgeCounter(str(workspace_id))
    enforce = await counter.start()
    if not enforce:
        logger.warning(
            "supersede_catch_up_counter_unreadable",
            workspace_id=str(workspace_id),
            hint=(
                "the judge budget is not enforced for this run; the judge itself "
                "skips its call while the counter is unreachable"
            ),
        )

    for start in range(0, len(rows), VECTOR_BATCH):
        batch = rows[start : start + VECTOR_BATCH]
        point_ids = {row.id: str(row.summary_embedding_id or row.id) for row in batch}
        # Read before the vectors: a row edited and re-embedded after this
        # point has a vector that may not match the text loaded later.
        revisions = await _revisions(db, list(point_ids))
        vectors = await retrieve_dense_vectors(collection, list(point_ids.values()))
        for row in batch:
            if enforce:
                used = await counter.used()
                if used is None:
                    enforce = False
                    logger.warning(
                        "supersede_catch_up_counter_unreadable",
                        workspace_id=str(workspace_id),
                        hint="the judge budget is not enforced for the rest of this run",
                    )
                elif used >= max_judge_calls:
                    result.stopped = STOPPED_JUDGE_BUDGET
                    break
            result.last_created_at, result.last_id = row.created_at, row.id
            memory = (
                await db.execute(
                    select(Memory)
                    .where(Memory.id == row.id, *filters)
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if memory is None or _revision(memory) != revisions.get(row.id):
                result.skipped += 1
                continue
            vector = vectors.get(point_ids[row.id])
            if vector is None:
                result.no_vector += 1
                continue
            seen = memory.supersede_candidate
            seen_target = _live_candidate_target(seen)
            ok = await _create_knn_seed_edges(
                db,
                memory,
                vector,
                collection,
                model,
                mechanical_reembed=False,
                seed=False,
            )
            result.processed += 1
            if not ok:
                result.failed += 1
                continue
            now = memory.supersede_candidate
            now_target = _live_candidate_target(now)
            if now_target is not None and seen_target is None:
                result.detected += 1
            elif now_target is None and seen_target is not None:
                result.cleared += 1
            elif now_target is not None and (
                now_target != seen_target or _verification(now) != _verification(seen)
            ):
                result.updated += 1
            else:
                result.unchanged += 1
            logger.debug(
                "supersede_catch_up_row",
                memory_id=str(row.id),
                before=seen_target,
                after=now_target,
            )
        if result.stopped is not None:
            break

    if enforce:
        result.judge_calls = await counter.used()
    logger.info(
        "supersede_catch_up_completed",
        context_id=str(context_id),
        planned=result.planned,
        processed=result.processed,
        detected=result.detected,
        updated=result.updated,
        cleared=result.cleared,
        unchanged=result.unchanged,
        failed=result.failed,
        no_vector=result.no_vector,
        skipped=result.skipped,
        judge_calls=result.judge_calls,
        stopped=result.stopped,
        last_created_at=to_utc_iso(result.last_created_at),
        last_id=str(result.last_id) if result.last_id else None,
    )
    return result
