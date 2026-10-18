"""Move a context to another embedding model without a search outage (#1525).

Vectors are derived data: every memory's ``summary`` lives in Postgres and
``build_memory_point`` already knows how to rebuild a point from a row. This
module runs that against a *different* model and collection while the context
keeps serving from its current one, verifies the copy, and only then flips
routing — the dual-collection shape::

    plan -> reembed (routing untouched) -> verify -> switch -> [purge]

* ``switch`` is one transaction: the ``ContextSearchConfig`` row changes and
  every memory written since the re-embed started is re-queued
  (``embedding_status='pending'``) so the regular sweep lands that small delta
  in the new collection. There is no moment at which the context has no
  vectors.
* Nothing here deletes source points implicitly. They stay until the operator
  calls :func:`purge_source_points` — which is why
  :func:`rollback_context_embedding` (a switch back plus a re-queue of what
  was written since the switch) is a complete rollback. The *target*
  collection, on the other hand, is reconciled: ``verify`` and ``switch``
  drop target points whose memory was forgotten while the migration ran, so
  a hard-delete request is not undone by the copy.
* Every step is its own short transaction: ``reembed`` ends the read
  transaction after each page before it talks to the embedding provider or
  Qdrant, so a large context never holds one Postgres transaction open for
  the whole run.
* ``KAGURA_RECREATE_COLLECTIONS`` is never consulted; the target collection is
  created with ``ensure_kagura_memories_collection`` like any other.
* Qdrant only: :func:`verify_context_migration` retrieves by id, which the
  LanceDB preview store does not support (same limit as ``copy_context_points``).
* A row the resource indexer wrote is not rebuilt from its ``summary`` (only
  the label ``[resource] doc vN``) but the way the indexer built it (#1896):
  ``ResourceIndexer.rebuild_point`` writes the document's point under
  ``Memory.point_id`` with the resource payload, as the embedding sweep does
  (#1870). Every step therefore addresses a row's point through
  :func:`_migrated_point_id`, never through ``Memory.id`` alone. A resource
  row that cannot be rebuilt (schema gone, content no longer a JSON document)
  is skipped and reported, not given a label vector and not a reason to stop:
  it has no usable vector under any model until its document is ingested
  again.
* Content chunk points (#2015) of a context with ``content_chunks_enabled``
  are rebuilt in the target too: ``reembed`` writes each page's chunks with
  the target model right after its summary points
  (``rebuild_content_chunks``), ``verify`` reports memories whose chunk
  count does not match their content (without failing ``ok``: re-running the re-embed
  fills them, and :func:`rebuild_target_chunks` rebuilds just those before a
  switch) and removes the chunks of memories that are no longer live,
  and ``switch`` removes those of memories forgotten since the re-embed
  started, so a context serves chunk hits right after the switch.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import ColumnElement, and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from config.constants import EMBEDDING_MODEL_REGISTRY
from config.embedding_policy import is_embedding_model_allowed
from config.settings import get_settings
from db.qdrant import (
    add_memory_to_qdrant,
    chunk_points_supported,
    count_context_chunk_points,
    delete_chunk_points,
    delete_context_points,
    delete_points_from_qdrant,
    ensure_kagura_memories_collection,
    get_collection_name,
    get_qdrant_client,
    list_context_point_ids,
)
from models.auth import Context
from models.config import ContextSearchConfig
from models.memory import Memory
from repositories.config_repository import search_config_defaults
from services.content_chunk_writer import content_chunks_enabled, rebuild_content_chunks
from services.context_routing import (
    resolve_context_embedding,
    resolve_context_embedding_config,
)
from services.embedding_service import EmbeddingService
from services.memory_service import build_memory_point
from services.resource_indexer import ResourceIndexer, ResourceRebuildError, owns_resource_point
from utils.content_chunks import CONTENT_CHUNK_MIN_CONTENT_CHARS, expected_chunk_count
from utils.datetime import utcnow
from utils.exceptions import NotFoundException, ValidationError
from utils.logger import get_logger

logger = get_logger(__name__)

ProgressCallback = Callable[[int, int], None]
"""``(done, total)`` — called after every batch of :func:`reembed_context`."""


@dataclass(frozen=True)
class MigrationPlan:
    """Everything a migration needs, resolved once up front."""

    context_id: UUID
    workspace_id: UUID
    source_model: str
    source_dimensions: int
    source_collection: str
    target_model: str
    target_dimensions: int
    target_collection: str
    memory_count: int
    content_chunks_on: bool = False
    """The context's ``content_chunks_enabled`` when planned (#2015): the
    re-embed also rebuilds its chunk points in the target."""
    chunk_estimate: int = 0
    """With ``content_chunks_on``: about how many chunk points (= extra
    embedding inputs) the re-embed writes."""


@dataclass
class ReembedResult:
    embedded: int
    batches: int
    started_at: datetime
    """Pass to :func:`switch_context_embedding` as ``requeue_since``."""
    unrebuildable: list[UUID] = field(default_factory=list)
    """Resource-ingested memories skipped because their point cannot be
    rebuilt from the row (#1896); not counted in ``embedded``."""
    chunks_written: int = 0
    """Content chunk points written to the target (#2015)."""


@dataclass
class VerifyResult:
    expected: int
    present: int
    missing: list[UUID] = field(default_factory=list)
    stale_removed: int = 0
    """Target points deleted because their memory is no longer live."""
    unrebuildable: list[UUID] = field(default_factory=list)
    """Resource-ingested memories without a target point that no re-embed can
    give one (#1896). Reported, but not ``missing``: they do not fail ``ok``."""
    chunks_expected: int = 0
    """Chunk points the live memories' current content splits into (#2015;
    0 while the context has ``content_chunks_enabled`` off)."""
    chunks_present: int = 0
    """Chunk points the target holds for live memories."""
    chunks_missing: list[UUID] = field(default_factory=list)
    """Memories whose number of chunk points in the target differs from what
    their current content splits into (some missing, or a tail left from a
    longer content). Counts only: an edit that keeps the count is not seen
    here (an edit after the re-embed started is re-queued by the switch
    anyway). Reported, not part of
    ``ok``: a re-run of the re-embed, or :func:`rebuild_target_chunks`,
    rewrites them before the switch (the CLI does the latter before it
    switches), and a memory is still found through its summary point."""
    chunks_stale_removed: int = 0
    """Memories whose target chunk points were deleted: not live any more, or
    the context has ``content_chunks_enabled`` off."""

    @property
    def ok(self) -> bool:
        return not self.missing


@dataclass
class SwitchResult:
    previous_model: str
    previous_dimensions: int
    model: str
    dimensions: int
    requeued: int
    stale_removed: int = 0
    """Points dropped from the new collection for memories forgotten since
    ``requeue_since`` (closes the verify -> switch delete race)."""


async def list_migratable_context_ids(
    db: AsyncSession, *, workspace_id: UUID | None = None
) -> list[UUID]:
    """Live contexts, optionally narrowed to one workspace, in a stable order."""
    stmt = select(Context.id).where(Context.deleted_at.is_(None))
    if workspace_id is not None:
        stmt = stmt.where(Context.workspace_id == workspace_id)
    result = await db.execute(stmt.order_by(Context.created_at, Context.id))
    return list(result.scalars().all())


async def plan_context_migration(
    db: AsyncSession,
    context_id: UUID,
    target_model: str,
    *,
    allowlist_setting: str | None = None,
    source_model: str | None = None,
) -> MigrationPlan:
    """Resolve source and target for one context; refuse what cannot work.

    ``source_model`` overrides the resolved source for the one step that runs
    after routing has already moved: purging. After A -> B has switched, the
    context routes to B, so a plan derived from routing alone cannot name A;
    ``source_model="A"`` with ``target_model`` = the active model rebuilds the
    A -> B plan :func:`purge_source_points` needs. It is refused when it names
    the model the context still serves from.

    Raises:
        ValidationError: unknown target, target not offered by this deployment,
            target equal to the current model, or an explicit ``source_model``
            that is unknown, still active, or paired with a non-active target.
        NotFoundException: no live context with that id.
    """
    if target_model not in EMBEDDING_MODEL_REGISTRY:
        raise ValidationError(f"Unknown embedding model: {target_model!r}")
    if source_model is not None and source_model not in EMBEDDING_MODEL_REGISTRY:
        raise ValidationError(f"Unknown embedding model: {source_model!r}")
    if not is_embedding_model_allowed(target_model, allowlist_setting):
        raise ValidationError(
            f"Embedding model {target_model!r} is not offered by this deployment "
            "(EMBEDDING_MODEL_ALLOWLIST)"
        )

    result = await db.execute(
        select(Context).where(Context.id == context_id, Context.deleted_at.is_(None))
    )
    context = result.scalar_one_or_none()
    if context is None:
        raise NotFoundException("Context", str(context_id))

    current_model, current_dimensions, content_chunks_on = await resolve_context_embedding_config(
        db, context_id
    )
    if source_model is None:
        source_model, source_dimensions = current_model, current_dimensions
        if source_model == target_model:
            raise ValidationError(f"Context {context_id} already uses {target_model!r}")
    else:
        if source_model == current_model:
            raise ValidationError(
                f"Context {context_id} still routes to {source_model!r}; switch before purging"
            )
        if target_model != current_model:
            raise ValidationError(
                f"explicit source {source_model!r} must be paired with the active "
                f"model {current_model!r} as target, not {target_model!r}"
            )
        source_dimensions = EMBEDDING_MODEL_REGISTRY[source_model][0]
    target_dimensions = EMBEDDING_MODEL_REGISTRY[target_model][0]

    count_result = await db.execute(
        select(func.count())
        .select_from(Memory)
        .where(Memory.context_id == context_id, Memory.deleted_at.is_(None))
    )
    memory_count = int(count_result.scalar_one() or 0)

    chunk_estimate = 0
    if content_chunks_on and source_model == current_model:
        # The re-embed rebuilds chunk points (#2015): one extra embedding
        # input per chunk. A row that owns a resource point is never chunked
        # (the rule rebuild_content_chunks and verify apply).
        length = func.char_length(Memory.content)
        lengths = await db.execute(
            select(Memory, length)
            .options(load_only(*_POINT_ID_COLUMNS))
            .where(
                Memory.context_id == context_id,
                Memory.deleted_at.is_(None),
                length >= CONTENT_CHUNK_MIN_CONTENT_CHARS,
            )
        )
        chunk_estimate = sum(
            expected_chunk_count(int(n))
            for memory, n in lengths.all()
            if not owns_resource_point(memory)
        )
        db.expunge_all()

    return MigrationPlan(
        context_id=context_id,
        workspace_id=context.workspace_id,
        source_model=source_model,
        source_dimensions=source_dimensions,
        source_collection=get_collection_name(source_model, source_dimensions),
        target_model=target_model,
        target_dimensions=target_dimensions,
        target_collection=get_collection_name(target_model, target_dimensions),
        memory_count=memory_count,
        content_chunks_on=content_chunks_on,
        chunk_estimate=chunk_estimate,
    )


def _migrated_point_id(memory: Memory) -> UUID:
    """The id of the point the migration writes — and expects — for ``memory``.

    ``Memory.point_id`` for a row that owns a resource point (the uuid5 of its
    document, what ``rebuild_point`` writes), the row id for every other row
    (what ``add_memory_to_qdrant`` writes). The two differ from a plain
    ``Memory.point_id`` only for a row that names a resource point it does not
    own; the embedding sweep writes that one under its row id as well.
    """
    return memory.point_id if owns_resource_point(memory) else memory.id


_POINT_ID_COLUMNS = (
    Memory.id,
    Memory.summary_embedding_id,
    Memory.resource_id,
    Memory.resource_doc_id,
    Memory.resource_version,
)
"""What :func:`_migrated_point_id` reads; enough to address a row's point
without loading its content."""


async def _points_live_rows_name(
    db: AsyncSession,
    point_ids: Iterable[UUID],
    *,
    other_than: UUID | None = None,
    batch_size: int = 500,
) -> set[UUID]:
    """Those of ``point_ids`` a live row names as its point.

    A resource point's id is derived from the document, not the row (#1829):
    the same document version indexed into two contexts that share a
    collection is ONE point for TWO rows, and a tombstone and a live row of
    one document name the same point. A point is only the migration's to
    delete when no live row names it — the rule ``forget`` follows
    (``MemoryService._delete_memory_point``), and like it not scoped by
    context: the row to find may be in another one. ``other_than`` leaves one
    row out (the row whose point is being removed).
    """
    wanted = list(point_ids)
    named: set[UUID] = set()
    for i in range(0, len(wanted), batch_size):
        stmt = select(Memory.summary_embedding_id).where(
            Memory.summary_embedding_id.in_(wanted[i : i + batch_size]),
            Memory.deleted_at.is_(None),
        )
        if other_than is not None:
            stmt = stmt.where(Memory.id != other_than)
        named.update((await db.execute(stmt)).scalars().all())
    return named


def _as_uuid(point_id: str) -> UUID | None:
    try:
        return UUID(point_id)
    except ValueError:
        return None


class _Rebuildable(Exception):
    """Raised by :class:`_RebuildProbe`: the rebuild got as far as embedding."""


class _RebuildProbe:
    """An embedding service that embeds nothing.

    ``rebuild_point`` refuses a row it cannot rebuild before it embeds the
    document. Handing it this probe asks exactly that question — would the
    indexer rebuild this row? — without an embedding request or a write, and
    without a second copy of the indexer's conditions here.
    """

    async def embed(self, *args: Any, **kwargs: Any) -> list[float]:
        raise _Rebuildable


async def _can_rebuild(indexer: ResourceIndexer, memory: Memory, collection_name: str) -> bool:
    try:
        await indexer.rebuild_point(
            memory,
            collection_name=collection_name,
            embedding_service=cast(EmbeddingService, _RebuildProbe()),
        )
    except _Rebuildable:
        return True
    except ResourceRebuildError as exc:
        logger.warning(
            "context_embedding_resource_unrebuildable",
            memory_id=str(memory.id),
            context_id=str(memory.context_id),
            error=str(exc),
        )
        return False
    return True


def _group_by_user(rows: list[Memory]) -> Iterator[tuple[str, list[Memory]]]:
    """Consecutive runs of the same ``user_id``.

    ``embed_batch`` takes one ``user_id`` (credential lookup + audit), so a
    batch that spans users is embedded in per-user runs.
    """
    current: list[Memory] = []
    for memory in rows:
        if current and current[-1].user_id != memory.user_id:
            yield current[-1].user_id, current
            current = []
        current.append(memory)
    if current:
        yield current[-1].user_id, current


def _requeued_by_switch(requeue_since: datetime) -> ColumnElement[bool]:
    """The live rows :func:`switch_context_embedding` re-queues with ``requeue_since``:
    not embedded successfully, or written since."""
    return or_(
        Memory.embedding_status != "success",
        Memory.created_at >= requeue_since,
        Memory.updated_at >= requeue_since,
    )


def _left_by_switch(requeue_since: datetime) -> ColumnElement[bool]:
    """The exact complement of :func:`_requeued_by_switch` under SQL NULLs.

    ``updated_at`` is nullable (a memory never edited has none) and
    ``updated_at >= since`` is NULL for it, so the switch leaves such a row
    alone; a plain ``updated_at < since`` would drop it from this side too.
    ``embedding_status`` and ``created_at`` are NOT NULL.
    """
    return and_(
        Memory.embedding_status == "success",
        Memory.created_at < requeue_since,
        or_(Memory.updated_at.is_(None), Memory.updated_at < requeue_since),
    )


async def reembed_context(
    db: AsyncSession,
    plan: MigrationPlan,
    *,
    batch_size: int = 64,
    embedding_service: EmbeddingService | None = None,
    progress: ProgressCallback | None = None,
) -> ReembedResult:
    """Embed every live memory of the context with the target model into the
    target collection. Routing is not touched; the context keeps serving from
    the source collection throughout.

    A memory the resource indexer wrote goes through the indexer instead of
    the batch (#1896): its summary is only a label, so its point is rebuilt
    from the stored document, under ``Memory.point_id``, with the resource
    payload (``ResourceIndexer.rebuild_point``, one embedding request per
    document). One that cannot be rebuilt is skipped and listed in
    ``ReembedResult.unrebuildable``; the run goes on. A point an earlier
    migration left for such a row in the target collection is removed (unless
    another live row names it), so the outcome does not depend on what the
    collection held before: the row has no target vector, and
    :func:`verify_context_migration` reports it.

    Each page's content chunk points (#2015) are rebuilt in the target right
    after its summary points, with the target model
    (``rebuild_content_chunks``: the flag read fresh per page before the
    delete and before the upsert, so a flag turned off mid-run writes nothing
    more, and one turned on mid-run is picked up from the next page; the
    pages before it are what :func:`verify_context_migration` reports as
    without chunks). ``plan.content_chunks_on`` only feeds the plan's
    estimate. A chunk failure stops the run like a summary failure does.

    Idempotent: points are upserted by point id, so a rerun after a failure
    overwrites instead of duplicating.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")

    started_at = utcnow()
    await ensure_kagura_memories_collection(plan.target_dimensions, plan.target_collection)
    service = embedding_service or EmbeddingService(
        db, model=plan.target_model, dimensions=plan.target_dimensions
    )

    embedded = 0
    batches = 0
    chunks_written = 0
    unrebuildable: list[UUID] = []
    indexer: ResourceIndexer | None = None
    last_id: UUID | None = None
    while True:
        stmt = select(Memory).where(
            Memory.context_id == plan.context_id, Memory.deleted_at.is_(None)
        )
        if last_id is not None:
            stmt = stmt.where(Memory.id > last_id)
        stmt = stmt.order_by(Memory.id).limit(batch_size)
        rows = list((await db.execute(stmt)).scalars().all())
        # Rows are read-only here: drop them from the identity map so a large
        # context does not accumulate across pages, and end the read
        # transaction the SELECT opened before any embedding / Qdrant call.
        # Otherwise one Postgres transaction would stay open for the whole
        # run (hours on a big context), pinning locks and vacuum. The session
        # is expire_on_commit=False, so the materialised rows stay usable.
        db.expunge_all()
        await db.commit()
        if not rows:
            break

        resource_rows = [memory for memory in rows if owns_resource_point(memory)]
        resource_row_ids = {memory.id for memory in resource_rows}
        plain_rows = [memory for memory in rows if memory.id not in resource_row_ids]
        payloads: dict[UUID, dict[str, Any]] = {}

        for user_id, group in _group_by_user(plain_rows):
            vectors = await service.embed_batch(
                [memory.summary for memory in group],
                user_id,
                context_id=str(plan.context_id),
                workspace_id=str(plan.workspace_id),
            )
            if len(vectors) != len(group):
                raise RuntimeError(
                    f"embed_batch returned {len(vectors)} vectors for {len(group)} texts"
                )
            for memory, vector in zip(group, vectors, strict=True):
                payload, sparse_indices, sparse_values = build_memory_point(memory)
                payloads[memory.id] = payload
                await add_memory_to_qdrant(
                    user_id=memory.user_id,
                    memory_id=memory.id,
                    vector=vector,
                    payload=payload,
                    workspace_id=str(memory.workspace_id),
                    context_id=str(memory.context_id),
                    sparse_indices=sparse_indices,
                    sparse_values=sparse_values,
                    collection_name=plan.target_collection,
                )
                embedded += 1

        if plain_rows:
            # Not gated on plan.content_chunks_on: the writer reads the flag
            # fresh (one SELECT per page) and writes nothing while it is off.
            chunks_written += await rebuild_content_chunks(
                db,
                plain_rows,
                base_payloads=payloads,
                embed_svc=service,
                collection_name=plan.target_collection,
                context_id=plan.context_id,
            )

        for memory in resource_rows:
            if indexer is None:
                indexer = ResourceIndexer(db)
            try:
                await indexer.rebuild_point(
                    memory, collection_name=plan.target_collection, embedding_service=service
                )
                embedded += 1
            except ResourceRebuildError as exc:
                # Not a reason to stop the migration: the row has no usable
                # vector under the source model either once its point is
                # gone, and a label vector would be worse than none.
                unrebuildable.append(memory.id)
                logger.warning(
                    "context_embedding_resource_unrebuildable",
                    memory_id=str(memory.id),
                    context_id=str(plan.context_id),
                    error=str(exc),
                )
                # The target may hold this row's point from an earlier
                # migration to the same model (A -> B, back to A, A -> B
                # again). It was not built from what the row holds now and
                # verify would count it as present; remove it.
                if not await _points_live_rows_name(db, [memory.point_id], other_than=memory.id):
                    await delete_points_from_qdrant([str(memory.point_id)], plan.target_collection)
            # rebuild_point reads the context, the schema and the event time;
            # end that read transaction before the next row's embedding call,
            # as the page loop does.
            await db.commit()

        batches += 1
        last_id = rows[-1].id
        if progress is not None:
            progress(embedded + len(unrebuildable), plan.memory_count)

    logger.info(
        "context_embedding_reembedded",
        context_id=str(plan.context_id),
        target_model=plan.target_model,
        target_collection=plan.target_collection,
        embedded=embedded,
        batches=batches,
        unrebuildable=len(unrebuildable),
        chunks_written=chunks_written,
    )
    return ReembedResult(
        embedded=embedded,
        batches=batches,
        started_at=started_at,
        unrebuildable=unrebuildable,
        chunks_written=chunks_written,
    )


@dataclass
class ChunkRebuildResult:
    rebuilt: int = 0
    """Memories whose chunks were rebuilt (live plain rows of those asked for)."""
    chunks_written: int = 0
    still_missing: list[UUID] = field(default_factory=list)
    """Rebuilt memories whose chunk count in the target still differs from
    their content afterwards, leaving out those the switch re-queues anyway
    (changed since ``requeue_since``, or not embedded successfully)."""


async def rebuild_target_chunks(
    db: AsyncSession,
    plan: MigrationPlan,
    memory_ids: Iterable[UUID],
    *,
    requeue_since: datetime | None = None,
    batch_size: int = 64,
    embedding_service: EmbeddingService | None = None,
) -> ChunkRebuildResult:
    """Rebuild the content chunk points of ``memory_ids`` in the target (#2015).

    For the memories :func:`verify_context_migration` lists in
    ``chunks_missing``, before the switch: a flag turned on after the plan
    was made (``reembed_context`` wrote no chunks then), or a chunk count
    that no longer matches the content. The same writer as the re-embed
    (``rebuild_content_chunks``: the flag read fresh, delete first, then
    write), page by page; rows that are no longer live or own a resource
    point are skipped.

    Then counts the rebuilt memories' chunks in the target again (only the
    chunks: summary points of memories written meanwhile are the switch's
    re-queue's job, as they are after any verify). A memory the switch
    re-queues with this ``requeue_since`` is not reported: the sweep
    rewrites its chunks under the new routing.

    Only safe before the switch: the writer claims nothing, so in a routed
    collection it would race the embedding worker's chunk writes. Refused
    (``ValidationError``) when the context already routes to the target.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    out = ChunkRebuildResult()
    ids = sorted(set(memory_ids))
    if not ids:
        return out
    current_model, current_dimensions = await resolve_context_embedding(db, plan.context_id)
    if get_collection_name(current_model, current_dimensions) == plan.target_collection:
        raise ValidationError(
            f"Context {plan.context_id} already routes to {plan.target_collection!r}; "
            "rebuilding its chunks there is only safe before the switch"
        )
    service = embedding_service or EmbeddingService(
        db, model=plan.target_model, dimensions=plan.target_dimensions
    )
    rebuilt_ids: list[UUID] = []
    for i in range(0, len(ids), batch_size):
        stmt = select(Memory).where(
            Memory.id.in_(ids[i : i + batch_size]),
            Memory.context_id == plan.context_id,
            Memory.deleted_at.is_(None),
        )
        if requeue_since is not None:
            # What the switch re-queues gets its chunks from the sweep anyway.
            stmt = stmt.where(_left_by_switch(requeue_since))
        result = await db.execute(stmt.order_by(Memory.id))
        rows = [memory for memory in result.scalars().all() if not owns_resource_point(memory)]
        # As in the re-embed: end the read transaction before the embedding calls.
        db.expunge_all()
        await db.commit()
        if not rows:
            continue
        payloads = {memory.id: build_memory_point(memory)[0] for memory in rows}
        out.chunks_written += await rebuild_content_chunks(
            db,
            rows,
            base_payloads=payloads,
            embed_svc=service,
            collection_name=plan.target_collection,
            context_id=plan.context_id,
        )
        rebuilt_ids.extend(memory.id for memory in rows)
    out.rebuilt = len(rebuilt_ids)

    if rebuilt_ids and await content_chunks_enabled(db, plan.context_id):
        length = func.char_length(Memory.content)
        for i in range(0, len(rebuilt_ids), batch_size):
            page = rebuilt_ids[i : i + batch_size]
            # Only this page's chunk points, not every chunk of the context.
            stored = await count_context_chunk_points(
                str(plan.workspace_id),
                str(plan.context_id),
                plan.target_collection,
                memory_ids=page,
            )
            stmt = select(Memory.id, length).where(Memory.id.in_(page), Memory.deleted_at.is_(None))
            if requeue_since is not None:
                # What switch_context_embedding re-queues is left to the sweep.
                stmt = stmt.where(_left_by_switch(requeue_since))
            for memory_id, n in (await db.execute(stmt)).all():
                expected = expected_chunk_count(int(n or 0))
                if stored.get(str(memory_id), 0) != expected:
                    out.still_missing.append(memory_id)
        await db.commit()
        out.still_missing.sort()

    logger.info(
        "context_embedding_chunks_rebuilt",
        context_id=str(plan.context_id),
        target_collection=plan.target_collection,
        requested=len(ids),
        rebuilt=out.rebuilt,
        chunks_written=out.chunks_written,
        still_missing=len(out.still_missing),
    )
    return out


async def verify_context_migration(
    db: AsyncSession, plan: MigrationPlan, *, batch_size: int = 500
) -> VerifyResult:
    """Every live memory of the context must have a point in the target
    collection. Reports the missing ids rather than a bare count, so a caller
    can decide whether to re-run :func:`reembed_context` or investigate.

    A memory's point is looked up under :func:`_migrated_point_id` — the
    document's point id for a resource-ingested row (#1896), the row id
    otherwise. A resource-ingested row without a point that the indexer could
    not rebuild either is listed in ``unrebuildable`` instead of ``missing``:
    re-running the re-embed cannot fix it, so it does not hold the switch
    back. One that could be rebuilt (written after the re-embed passed it) is
    ``missing`` like any other row.

    Also reconciles the other direction: a memory forgotten *after* the
    re-embed copied it has a target point with no live row. ``forget`` only
    deletes from the collection the context routes to, so nothing else would
    ever remove that point; it is deleted here and counted in
    ``stale_removed`` — unless a live row of another context names it (a
    document two contexts ingested is one point in a shared collection). Deletes that land after this check are covered by
    :func:`switch_context_embedding`.

    Content chunk points (#2015) are reconciled as well
    (:func:`_reconcile_target_chunks`): with the context's
    ``content_chunks_enabled`` on, memories whose chunk count in the target
    differs from what their current content splits into are listed in
    ``chunks_missing`` (reported, not part of ``ok``); chunks of memories
    that are no longer live are deleted, and with the flag off every chunk
    point of the context in the target is.
    """
    result = await db.execute(
        select(Memory)
        .options(load_only(*_POINT_ID_COLUMNS))
        .where(Memory.context_id == plan.context_id, Memory.deleted_at.is_(None))
        .order_by(Memory.id)
    )
    # (memory id, point id, owns a resource point), then let go of the rows:
    # they are partially loaded, and the unrebuildable check below loads the
    # few it needs in full.
    expected = [
        (memory.id, str(_migrated_point_id(memory)), owns_resource_point(memory))
        for memory in result.scalars().all()
    ]
    db.expunge_all()

    client = get_qdrant_client()
    present = 0
    missing: list[UUID] = []
    missing_resource_rows: list[UUID] = []
    for i in range(0, len(expected), batch_size):
        batch = expected[i : i + batch_size]
        points = await client.retrieve(
            collection_name=plan.target_collection,
            ids=[point_id for _, point_id, _ in batch],
            with_payload=False,
            with_vectors=False,
        )
        found = {str(point.id) for point in points}
        for memory_id, point_id, is_resource_row in batch:
            if point_id in found:
                present += 1
            elif is_resource_row:
                missing_resource_rows.append(memory_id)
            else:
                missing.append(memory_id)

    unrebuildable: list[UUID] = []
    if missing_resource_rows:
        indexer = ResourceIndexer(db)
        for i in range(0, len(missing_resource_rows), batch_size):
            rows = await db.execute(
                select(Memory)
                .where(Memory.id.in_(missing_resource_rows[i : i + batch_size]))
                .order_by(Memory.id)
            )
            for memory in rows.scalars().all():
                if await _can_rebuild(indexer, memory, plan.target_collection):
                    missing.append(memory.id)
                else:
                    unrebuildable.append(memory.id)
        missing.sort()

    live = {point_id for _, point_id, _ in expected}
    stored = await list_context_point_ids(
        str(plan.workspace_id), str(plan.context_id), plan.target_collection
    )
    stale = [point_id for point_id in stored if point_id not in live]
    if stale:
        # ... and no live row of another context either: contexts that share
        # the collection share the point of a document both ingested.
        candidates = {parsed for point_id in stale if (parsed := _as_uuid(point_id)) is not None}
        named_elsewhere = {
            str(point_id) for point_id in await _points_live_rows_name(db, candidates)
        }
        stale = [point_id for point_id in stale if point_id not in named_elsewhere]
    if stale:
        await delete_points_from_qdrant(stale, plan.target_collection)
        logger.info(
            "context_embedding_stale_points_removed",
            context_id=str(plan.context_id),
            target_collection=plan.target_collection,
            count=len(stale),
        )

    chunks = await _reconcile_target_chunks(
        db, plan, {memory_id for memory_id, _, is_resource_row in expected if not is_resource_row}
    )

    return VerifyResult(
        expected=len(expected),
        present=present,
        missing=missing,
        stale_removed=len(stale),
        unrebuildable=unrebuildable,
        chunks_expected=chunks.expected,
        chunks_present=chunks.present,
        chunks_missing=chunks.missing,
        chunks_stale_removed=chunks.stale_removed,
    )


@dataclass
class _ChunkReconciliation:
    expected: int = 0
    present: int = 0
    missing: list[UUID] = field(default_factory=list)
    stale_removed: int = 0


async def _reconcile_target_chunks(
    db: AsyncSession, plan: MigrationPlan, live_plain_ids: set[UUID], *, batch_size: int = 500
) -> _ChunkReconciliation:
    """Compare the target's chunk points with the live memories (#2015).

    The expected count of each memory comes from ``char_length(content)``
    (:func:`expected_chunk_count`), so no content is loaded. Chunk points
    whose memory is not a live plain row of the context are deleted — after
    asking Postgres again, so a memory written since the first read (verify
    re-run after the switch, when the sweep writes to the target) keeps its
    chunks. With the flag off nothing is expected and every chunk point of
    the context in the target is deleted ("flag off ⇒ no chunk points").
    A no-op on a store without chunk points.
    """
    out = _ChunkReconciliation()
    if not chunk_points_supported():
        return out
    chunks_on = await content_chunks_enabled(db, plan.context_id)
    stored = await count_context_chunk_points(
        str(plan.workspace_id), str(plan.context_id), plan.target_collection
    )

    expected_counts: dict[UUID, int] = {}
    if chunks_on:
        length = func.char_length(Memory.content)
        rows = await db.execute(
            select(Memory.id, length).where(
                Memory.context_id == plan.context_id,
                Memory.deleted_at.is_(None),
                length >= CONTENT_CHUNK_MIN_CONTENT_CHARS,
            )
        )
        expected_counts = {
            memory_id: expected_chunk_count(int(n))
            for memory_id, n in rows.all()
            if memory_id in live_plain_ids
        }
        out.expected = sum(expected_counts.values())
        out.present = sum(n for key, n in stored.items() if _as_uuid(key) in live_plain_ids)
        out.missing = sorted(
            memory_id
            for memory_id in live_plain_ids
            if stored.get(str(memory_id), 0) != expected_counts.get(memory_id, 0)
        )

    stale_keys = [key for key in stored if not chunks_on or _as_uuid(key) not in live_plain_ids]
    if stale_keys and chunks_on:
        # A memory written after the first read is live, but not in the set.
        # The same rule as that read: a live plain row of this context.
        candidates = [u for key in stale_keys if (u := _as_uuid(key)) is not None]
        live_now: set[str] = set()
        for i in range(0, len(candidates), batch_size):
            result = await db.execute(
                select(Memory)
                .options(load_only(*_POINT_ID_COLUMNS))
                .where(
                    Memory.id.in_(candidates[i : i + batch_size]),
                    Memory.context_id == plan.context_id,
                    Memory.deleted_at.is_(None),
                )
            )
            live_now.update(
                str(memory.id)
                for memory in result.scalars().all()
                if not owns_resource_point(memory)
            )
        db.expunge_all()
        stale_keys = [key for key in stale_keys if key not in live_now]
    if stale_keys:
        for i in range(0, len(stale_keys), batch_size):
            await delete_chunk_points(stale_keys[i : i + batch_size], plan.target_collection)
        out.stale_removed = len(stale_keys)
        logger.info(
            "context_embedding_stale_chunks_removed",
            context_id=str(plan.context_id),
            target_collection=plan.target_collection,
            memories=len(stale_keys),
            chunks_on=chunks_on,
        )
    if out.missing:
        logger.info(
            "context_embedding_chunks_missing",
            context_id=str(plan.context_id),
            target_collection=plan.target_collection,
            memories=len(out.missing),
        )
    return out


async def switch_context_embedding(
    db: AsyncSession,
    context_id: UUID,
    model: str,
    dimensions: int,
    *,
    requeue_since: datetime | None = None,
) -> SwitchResult:
    """Point the context at ``model`` and, in the same transaction, re-queue
    the memories the bulk re-embed could not have seen.

    ``requeue_since`` is the :attr:`ReembedResult.started_at` of the run that
    filled the target collection (or, for :func:`rollback_context_embedding`,
    the moment of the switch being undone). Rows created or updated since
    then — plus any row whose embedding was not ``success`` to begin with — go
    back to ``pending`` so the regular sweep embeds them under the new
    routing. Rows *forgotten* since then are removed from the new collection
    after the flip, closing the window between the last verify and the
    routing change — each under the point id the migration wrote it with
    (:func:`_migrated_point_id`, #1896), and never a point a live row of any
    context still names — together with their content chunk points (#2015).
    With ``None`` this is a pure routing flip.

    A worker that claimed a row before the flip resolved the old routing;
    ``process_pending_embedding`` only marks ``success`` while it still owns
    the ``processing`` claim, so the re-queue here wins and the sweep
    re-embeds that row under the new routing.
    """
    if model not in EMBEDDING_MODEL_REGISTRY:
        raise ValidationError(f"Unknown embedding model: {model!r}")
    expected_dimensions = EMBEDDING_MODEL_REGISTRY[model][0]
    if dimensions != expected_dimensions:
        raise ValidationError(f"{model!r} has {expected_dimensions} dimensions, not {dimensions}")

    previous_model, previous_dimensions = await resolve_context_embedding(db, context_id)

    result = await db.execute(
        select(ContextSearchConfig).where(ContextSearchConfig.context_id == context_id)
    )
    config = result.scalar_one_or_none()
    if config is None:
        # Legacy context that never materialised a config row: creating it
        # here is what moves it off the hardcoded fallback in context_routing.
        # The reranker columns take the #1572 deployment default, exactly as
        # the lazy create_or_get() on the recall path would — the row must not
        # depend on which path happened to materialise it first.
        config = ContextSearchConfig(
            context_id=context_id,
            embedding_model=model,
            embedding_dimensions=dimensions,
            **search_config_defaults(get_settings()),
        )
        db.add(config)
    else:
        config.embedding_model = model
        config.embedding_dimensions = dimensions

    requeued = 0
    if requeue_since is not None:
        requeue = await db.execute(
            update(Memory)
            .where(
                Memory.context_id == context_id,
                Memory.deleted_at.is_(None),
                _requeued_by_switch(requeue_since),
            )
            .values(embedding_status="pending", embedding_error=None, embedding_retry_count=0)
        )
        requeued = int(getattr(requeue, "rowcount", 0) or 0)

    await db.commit()

    stale_removed = 0
    if requeue_since is not None:
        # Routing now points at ``model``; forgets from here on delete from
        # its collection themselves. Forgets between the last verify and the
        # commit above only hit the previous collection — drop those points
        # from the new one. Hard-deleted rows (no tombstone) cannot be found
        # this way; only the soft-delete path (``deleted_at``) is covered.
        forgotten = await db.execute(
            select(Memory)
            .options(load_only(*_POINT_ID_COLUMNS))
            .where(Memory.context_id == context_id, Memory.deleted_at >= requeue_since)
        )
        forgotten_rows = list(forgotten.scalars().all())
        forgotten_points = {_migrated_point_id(memory) for memory in forgotten_rows}
        # #2015: their content chunk points too (chunk_of = the memory id;
        # resource rows have none). A no-op when there are none.
        forgotten_chunk_owners = sorted(
            str(memory.id) for memory in forgotten_rows if not owns_resource_point(memory)
        )
        if forgotten_points:
            # A live row — of this context or of another one sharing the
            # collection — that names the same point keeps it.
            forgotten_points -= await _points_live_rows_name(db, forgotten_points)
        if forgotten_points:
            await delete_points_from_qdrant(
                sorted(str(point_id) for point_id in forgotten_points),
                get_collection_name(model, dimensions),
            )
            stale_removed = len(forgotten_points)
        # After the summary points, so a failed chunk delete never keeps a
        # forgotten memory's summary point in the collection now served.
        if forgotten_chunk_owners:
            await delete_chunk_points(
                forgotten_chunk_owners, get_collection_name(model, dimensions)
            )

    logger.info(
        "context_embedding_switched",
        context_id=str(context_id),
        previous_model=previous_model,
        model=model,
        dimensions=dimensions,
        requeued=requeued,
        stale_removed=stale_removed,
    )
    return SwitchResult(
        previous_model=previous_model,
        previous_dimensions=previous_dimensions,
        model=model,
        dimensions=dimensions,
        requeued=requeued,
        stale_removed=stale_removed,
    )


async def rollback_context_embedding(
    db: AsyncSession,
    context_id: UUID,
    model: str,
    *,
    requeue_since: datetime | None = None,
) -> SwitchResult:
    """Route the context back to ``model`` without losing what was written
    since it left.

    A bare routing flip is not a rollback: after A -> B, memories created,
    updated or forgotten on B exist only in B's collection, so flipping to A
    would make them unsearchable, stale, or resurrect them. This re-queues
    the delta since the switch — by default the ``updated_at`` of the
    ``ContextSearchConfig`` row, which the switch stamped — and refuses when
    ``model``'s collection does not exist (nothing to serve from).

    The re-queue and the removal of forgotten points are
    :func:`switch_context_embedding`'s, so resource-ingested rows are handled
    as they are there: re-queued rows are rebuilt by the sweep under
    ``Memory.point_id`` (#1870), forgotten ones removed under it (#1896).

    Raises:
        ValidationError: unknown model, the model the context already routes
            to, no collection for it, or no recorded switch time and no
            explicit ``requeue_since``.
    """
    if model not in EMBEDDING_MODEL_REGISTRY:
        raise ValidationError(f"Unknown embedding model: {model!r}")
    dimensions = EMBEDDING_MODEL_REGISTRY[model][0]

    current_model, _ = await resolve_context_embedding(db, context_id)
    if current_model == model:
        raise ValidationError(f"Context {context_id} already routes to {model!r}")

    if requeue_since is None:
        result = await db.execute(
            select(ContextSearchConfig.updated_at).where(
                ContextSearchConfig.context_id == context_id
            )
        )
        requeue_since = result.scalar_one_or_none()
        if requeue_since is None:
            raise ValidationError(
                f"Context {context_id} has no recorded switch time; pass the time of "
                "the switch being undone as requeue_since (--requeue-since)"
            )

    collection = get_collection_name(model, dimensions)
    if not await get_qdrant_client().collection_exists(collection):
        raise ValidationError(
            f"Collection {collection!r} for {model!r} does not exist; "
            "there is nothing to route back to (was it purged?)"
        )

    return await switch_context_embedding(
        db, context_id, model, dimensions, requeue_since=requeue_since
    )


async def purge_source_points(db: AsyncSession, plan: MigrationPlan) -> int:
    """Delete the context's points from the source collection.

    Refuses while the context still routes to the source model — that would
    delete the vectors it is serving from. Returns the number of points
    removed (counted before deletion, per ``delete_context_points``).

    Deletes by the ``workspace_id`` + ``context_id`` payload every point of
    the context carries, not by id, so resource points (stored under the
    document's point id, #1896) go with the rest.
    """
    current_model, _ = await resolve_context_embedding(db, plan.context_id)
    if current_model == plan.source_model:
        raise ValidationError(
            f"Context {plan.context_id} still routes to {plan.source_model!r}; "
            "switch before purging"
        )
    if plan.source_collection == plan.target_collection:
        raise ValidationError("source and target collection are the same; nothing to purge")
    return await delete_context_points(
        str(plan.workspace_id), str(plan.context_id), plan.source_collection
    )
