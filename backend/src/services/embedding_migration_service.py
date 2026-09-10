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
* Nothing here deletes anything implicitly. The source points stay until the
  operator calls :func:`purge_source_points` — which is also why a ``switch``
  back to the source model is a complete rollback.
* ``KAGURA_RECREATE_COLLECTIONS`` is never consulted; the target collection is
  created with ``ensure_kagura_memories_collection`` like any other.
* Qdrant only: :func:`verify_context_migration` retrieves by id, which the
  LanceDB preview store does not support (same limit as ``copy_context_points``).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from config.constants import EMBEDDING_MODEL_REGISTRY
from config.embedding_policy import is_embedding_model_allowed
from db.qdrant import (
    add_memory_to_qdrant,
    delete_context_points,
    ensure_kagura_memories_collection,
    get_collection_name,
    get_qdrant_client,
)
from models.auth import Context
from models.config import ContextSearchConfig
from models.memory import Memory
from services.context_routing import resolve_context_embedding
from services.embedding_service import EmbeddingService
from services.memory_service import build_memory_point
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


@dataclass
class ReembedResult:
    embedded: int
    batches: int
    started_at: datetime
    """Pass to :func:`switch_context_embedding` as ``requeue_since``."""


@dataclass
class VerifyResult:
    expected: int
    present: int
    missing: list[UUID] = field(default_factory=list)

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
) -> MigrationPlan:
    """Resolve source and target for one context; refuse what cannot work.

    Raises:
        ValidationError: unknown target, target not offered by this deployment,
            or target equal to the current model.
        NotFoundException: no live context with that id.
    """
    if target_model not in EMBEDDING_MODEL_REGISTRY:
        raise ValidationError(f"Unknown embedding model: {target_model!r}")
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
        raise NotFoundException(f"Context not found: {context_id}")

    source_model, source_dimensions = await resolve_context_embedding(db, context_id)
    if source_model == target_model:
        raise ValidationError(f"Context {context_id} already uses {target_model!r}")
    target_dimensions = EMBEDDING_MODEL_REGISTRY[target_model][0]

    count_result = await db.execute(
        select(func.count())
        .select_from(Memory)
        .where(Memory.context_id == context_id, Memory.deleted_at.is_(None))
    )
    memory_count = int(count_result.scalar_one() or 0)

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
    )


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

    Idempotent: points are upserted by memory id, so a rerun after a failure
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
    last_id: UUID | None = None
    while True:
        stmt = select(Memory).where(
            Memory.context_id == plan.context_id, Memory.deleted_at.is_(None)
        )
        if last_id is not None:
            stmt = stmt.where(Memory.id > last_id)
        stmt = stmt.order_by(Memory.id).limit(batch_size)
        rows = list((await db.execute(stmt)).scalars().all())
        if not rows:
            break

        for user_id, group in _group_by_user(rows):
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

        batches += 1
        last_id = rows[-1].id
        # Rows are read-only here; drop them from the identity map so a large
        # context does not accumulate in memory across batches.
        db.expunge_all()
        if progress is not None:
            progress(embedded, plan.memory_count)

    logger.info(
        "context_embedding_reembedded",
        context_id=str(plan.context_id),
        target_model=plan.target_model,
        target_collection=plan.target_collection,
        embedded=embedded,
        batches=batches,
    )
    return ReembedResult(embedded=embedded, batches=batches, started_at=started_at)


async def verify_context_migration(
    db: AsyncSession, plan: MigrationPlan, *, batch_size: int = 500
) -> VerifyResult:
    """Every live memory of the context must have a point in the target
    collection. Reports the missing ids rather than a bare count, so a caller
    can decide whether to re-run :func:`reembed_context` or investigate.
    """
    result = await db.execute(
        select(Memory.id)
        .where(Memory.context_id == plan.context_id, Memory.deleted_at.is_(None))
        .order_by(Memory.id)
    )
    ids = list(result.scalars().all())

    client = get_qdrant_client()
    present = 0
    missing: list[UUID] = []
    for i in range(0, len(ids), batch_size):
        batch = ids[i : i + batch_size]
        points = await client.retrieve(
            collection_name=plan.target_collection,
            ids=[str(memory_id) for memory_id in batch],
            with_payload=False,
            with_vectors=False,
        )
        found = {str(point.id) for point in points}
        for memory_id in batch:
            if str(memory_id) in found:
                present += 1
            else:
                missing.append(memory_id)

    return VerifyResult(expected=len(ids), present=present, missing=missing)


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
    filled the target collection. Rows created or updated since then — plus
    any row whose embedding was not ``success`` to begin with — go back to
    ``pending`` so the regular sweep embeds them under the new routing. With
    ``None`` this is a pure routing flip (used for rollback when the source
    collection is still complete).
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
        config = ContextSearchConfig(
            context_id=context_id, embedding_model=model, embedding_dimensions=dimensions
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
                or_(
                    Memory.embedding_status != "success",
                    Memory.created_at >= requeue_since,
                    Memory.updated_at >= requeue_since,
                ),
            )
            .values(embedding_status="pending", embedding_error=None, embedding_retry_count=0)
        )
        requeued = int(getattr(requeue, "rowcount", 0) or 0)

    await db.commit()
    logger.info(
        "context_embedding_switched",
        context_id=str(context_id),
        previous_model=previous_model,
        model=model,
        dimensions=dimensions,
        requeued=requeued,
    )
    return SwitchResult(
        previous_model=previous_model,
        previous_dimensions=previous_dimensions,
        model=model,
        dimensions=dimensions,
        requeued=requeued,
    )


async def purge_source_points(db: AsyncSession, plan: MigrationPlan) -> int:
    """Delete the context's points from the source collection.

    Refuses while the context still routes to the source model — that would
    delete the vectors it is serving from. Returns the number of points
    removed (counted before deletion, per ``delete_context_points``).
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
