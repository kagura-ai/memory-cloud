"""Retryable vector erasure without retaining raw account identities (#2391)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from db.qdrant import erase_point_author, erasure_scope_points, list_memory_collections
from models.auth import User
from models.memory import Memory
from models.vector_erasure import VectorErasureJob
from services.erasure_details import erasure_pseudonym
from utils.datetime import utcnow
from utils.logger import get_logger

logger = get_logger(__name__)


class EmbeddingAuthor(Protocol):
    id: UUID
    user_id: str


@dataclass(frozen=True)
class CleanupScope:
    id: UUID
    request_id: UUID
    memory_id: UUID
    point_id: UUID
    subject_pseudonym: str
    generation: int
    captured_before_erasure: bool


async def capture_vector_erasure(
    db: AsyncSession,
    request_id: UUID,
    subject: str,
    *,
    pseudonym: bool = False,
    existed_at: datetime | None = None,
) -> int:
    """Capture UUID scopes before workspace/tombstone cascades, in their transaction.

    No raw subject, email, content or document key is stored. Completed scopes
    stay as evidence; ON CONFLICT makes historical cleanup reruns idempotent.
    """
    statement = select(Memory.id, Memory.summary_embedding_id).where(Memory.user_id == subject)
    if pseudonym:
        statement = statement.where(Memory.embedding_erasure_restored_at.is_(None))
    if existed_at is not None:
        statement = statement.where(Memory.created_at <= existed_at)
    rows = (await db.execute(statement.order_by(Memory.id))).all()
    actor = subject if pseudonym else erasure_pseudonym(subject)
    for memory_id, summary_id in rows:
        await db.execute(
            insert(VectorErasureJob)
            .values(
                id=uuid4(),
                request_id=request_id,
                memory_id=memory_id,
                point_id=summary_id or memory_id,
                subject_pseudonym=actor,
            )
            .on_conflict_do_nothing(constraint="uq_vector_erasure_scope")
        )
    return len(rows)


async def embedding_author_current(db: AsyncSession, memory: EmbeddingAuthor) -> bool:
    """Fresh scalar lookup: ORM identity-map state cannot authorize a write."""
    async with db.begin_nested():
        current = await db.scalar(
            select(Memory.id).where(
                Memory.id == memory.id,
                Memory.user_id == memory.user_id,
                Memory.deleted_at.is_(None),
                Memory.embedding_suppressed_at.is_(None),
            )
        )
    return current is not None


async def _drain_scope(db: AsyncSession, job: CleanupScope, collections: list[str]) -> None:
    # Author changes require UUID scopes AND author comparison. A resource
    # point can have other live holders, including a newly registered account.
    holders = (
        await db.execute(
            select(Memory.id, Memory.user_id, Memory.embedding_suppressed_at).where(
                Memory.deleted_at.is_(None),
                (Memory.id == job.memory_id) | (Memory.summary_embedding_id == job.point_id),
            )
        )
    ).all()
    shared = job.point_id != job.memory_id and any(row.id != job.memory_id for row in holders)
    restored = any(
        row.id == job.memory_id
        and row.user_id == job.subject_pseudonym
        and row.embedding_suppressed_at is None
        for row in holders
    )
    live_authors = set(
        (
            await db.execute(
                select(User.user_id).where(User.user_id.in_([row.user_id for row in holders]))
            )
        ).scalars()
    )
    registered_again = any(
        row.embedding_suppressed_at is None
        and row.user_id in live_authors
        and row.user_id != job.subject_pseudonym
        and erasure_pseudonym(row.user_id) == job.subject_pseudonym
        for row in holders
    )
    own_registered_again = not job.captured_before_erasure and any(
        row.id == job.memory_id
        and row.user_id in live_authors
        and row.embedding_suppressed_at is None
        and row.user_id != job.subject_pseudonym
        and erasure_pseudonym(row.user_id) == job.subject_pseudonym
        for row in holders
    )
    for collection in collections:
        for point_id, author in await erasure_scope_points(collection, job.memory_id, job.point_id):
            raw_subject = (
                author != job.subject_pseudonym
                and erasure_pseudonym(author) == job.subject_pseudonym
            )
            if not raw_subject and author != job.subject_pseudonym:
                continue
            if own_registered_again:
                continue
            if registered_again and point_id == str(job.point_id):
                continue
            if point_id == str(job.point_id) and shared:
                if raw_subject:
                    await erase_point_author(
                        collection, point_id, author, replacement=job.subject_pseudonym
                    )
            elif raw_subject or not restored:
                await erase_point_author(collection, point_id, author)


async def _repair_resurrected_scope(db: AsyncSession, job: CleanupScope) -> None:
    """A recovery may restore a pre-erasure snapshot under its original UUID.

    Captured UUID evidence remains authoritative even when the raw subject
    registered again. Only explicit pseudonym restoration is allowed. Repair
    BEFORE the global vector lock so row-locking writers cannot deadlock it.
    """
    from config.constants import ERASED_AUTHOR_EMBEDDING_ERROR, MAX_EMBEDDING_RETRIES
    from services.account_erasure_service import _trigger_only_details
    from services.content_chunk_gaps import ROW_LOCK_TIMEOUT

    row = (
        await db.execute(
            select(Memory.user_id).where(Memory.id == job.memory_id, Memory.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if (
        row is None
        or row == job.subject_pseudonym
        or erasure_pseudonym(row) != job.subject_pseudonym
    ):
        await db.commit()
        return
    conditions = [Memory.id == job.memory_id, Memory.user_id == row]
    if not job.captured_before_erasure:
        conditions.append(~select(User.id).where(User.user_id == row).exists())
    await db.execute(ROW_LOCK_TIMEOUT)
    await db.execute(
        update(Memory)
        .where(*conditions)
        .values(
            user_id=job.subject_pseudonym,
            details=_trigger_only_details(),
            embedding_suppressed_at=utcnow(),
            embedding_erasure_restored_at=None,
            embedding_status="failed",
            embedding_error=ERASED_AUTHOR_EMBEDDING_ERROR,
            embedding_retry_count=MAX_EMBEDDING_RETRIES,
            embedding_attempted_at=utcnow(),
        )
        .execution_options(synchronize_session=False)
    )
    await db.commit()


async def drain_vector_erasures(
    db: AsyncSession,
    *,
    request_id: UUID | None = None,
    limit: int = 500,
    verify_completed: bool = False,
) -> dict[str, int]:
    """Drain regardless of request status; failures leave durable scopes pending.

    Vector calls are idempotent and compare the observed author atomically.
    Generation fencing prevents a late writer's reopened job being acknowledged
    by an older drain. No memory/job locks span vector I/O.
    """
    stmt = select(VectorErasureJob)
    if request_id is not None:
        stmt = stmt.where(VectorErasureJob.request_id == request_id)
    if verify_completed:
        pending = list(
            (
                await db.execute(
                    stmt.where(VectorErasureJob.completed_at.is_(None))
                    .order_by(VectorErasureJob.id)
                    .limit(max(1, limit // 2))
                )
            ).scalars()
        )
        retained = list(
            (
                await db.execute(
                    stmt.where(VectorErasureJob.completed_at.is_not(None))
                    .order_by(VectorErasureJob.completed_at, VectorErasureJob.id)
                    .limit(max(1, limit - len(pending)))
                )
            ).scalars()
        )
        rows = pending + retained
    else:
        rows = list(
            (
                await db.execute(
                    stmt.where(VectorErasureJob.completed_at.is_(None))
                    .order_by(VectorErasureJob.id)
                    .limit(limit)
                )
            ).scalars()
        )
    jobs = [
        CleanupScope(
            id=job.id,
            request_id=job.request_id,
            memory_id=job.memory_id,
            point_id=job.point_id,
            subject_pseudonym=job.subject_pseudonym,
            generation=job.generation,
            captured_before_erasure=job.captured_before_erasure,
        )
        for job in rows
    ]
    await db.commit()
    done = failed = 0
    if not jobs:
        return {"completed": done, "pending": failed}
    try:
        collections = await list_memory_collections()
    except Exception as exc:
        logger.warning(
            "vector_erasure_collections_failed", error_class=type(exc).__name__, scopes=len(jobs)
        )
        return {"completed": 0, "pending": len(jobs)}
    for job in jobs:
        generation = job.generation
        try:
            await _repair_resurrected_scope(db, job)
            from db.point_writer_lock import wait_for_point_writers

            if not await wait_for_point_writers(db, timeout_seconds=1):
                failed += 1
                continue
            await _drain_scope(db, job, collections)
            await db.execute(
                update(VectorErasureJob)
                .where(
                    VectorErasureJob.id == job.id,
                    VectorErasureJob.generation == generation,
                )
                .values(completed_at=utcnow())
                .execution_options(synchronize_session=False)
            )
            await db.commit()
            done += 1
        except Exception as exc:
            await db.rollback()
            failed += 1
            logger.warning(
                "vector_erasure_scope_failed",
                request_id=str(job.request_id),
                error_class=type(exc).__name__,
            )
    return {"completed": done, "pending": failed}


async def reconcile_erased_writer(db: AsyncSession, memory: EmbeddingAuthor) -> bool:
    """Reopen durable scopes after a stale writer finishes, then remove raw points.

    Called after a summary/chunk/resource upsert. The repaired current author
    is queued only when explicitly restored; suppression itself never clears.
    """
    if await embedding_author_current(db, memory):
        return False
    from db.base import isolated_session
    from services.content_chunk_writer import requeue_embedding

    async with isolated_session(db) as session:
        await session.execute(
            update(VectorErasureJob)
            .where(
                VectorErasureJob.memory_id == memory.id,
                VectorErasureJob.subject_pseudonym == erasure_pseudonym(memory.user_id),
            )
            .values(completed_at=None, generation=VectorErasureJob.generation + 1)
            .execution_options(synchronize_session=False)
        )
        await session.commit()
        await drain_vector_erasures(session)
        current = (
            await session.execute(
                select(Memory.user_id, Memory.embedding_suppressed_at).where(
                    Memory.id == memory.id, Memory.deleted_at.is_(None)
                )
            )
        ).one_or_none()
        if current is not None and current.embedding_suppressed_at is None:
            await requeue_embedding(
                session,
                memory.id,
                reason="erasure_stale_writer",
                include_processing=True,
                expected_author=current.user_id,
            )
            await session.commit()
    return True


async def _delete_subject_points(db: AsyncSession, subject: str) -> dict[str, int]:
    """Fail-closed first delete, retaining resource points shared by live rows."""
    from sqlalchemy.orm import aliased

    from services.account_erasure_service import delete_user_points

    other = aliased(Memory)
    protected = list(
        (
            await db.execute(
                select(Memory.summary_embedding_id)
                .where(
                    Memory.user_id == subject,
                    Memory.summary_embedding_id != Memory.id,
                    select(other.id)
                    .where(
                        other.summary_embedding_id == Memory.summary_embedding_id,
                        other.id != Memory.id,
                        other.deleted_at.is_(None),
                    )
                    .exists(),
                )
                .distinct()
            )
        ).scalars()
    )
    result = (
        await delete_user_points(
            subject, protected_point_ids=[str(point_id) for point_id in protected]
        )
        if protected
        else await delete_user_points(subject)
    )
    if protected:
        pseudonym = erasure_pseudonym(subject)
        for collection in await list_memory_collections():
            for point_id in protected:
                await erase_point_author(collection, str(point_id), subject, replacement=pseudonym)
    return result


async def delete_subject_points(db: AsyncSession, subject: str) -> dict[str, int]:
    """Serialize the first vector pass without pinning its lock over PG erasure."""
    from db.base import isolated_session
    from db.point_writer_lock import wait_for_point_writers
    from utils.exceptions import QdrantError

    async with isolated_session(db) as session:
        if not await wait_for_point_writers(session, timeout_seconds=5):
            raise QdrantError("Vector erasure deferred while point writers are active")
        result = await _delete_subject_points(session, subject)
        await session.commit()
        return result


async def has_vector_erasure_scope(db: AsyncSession, memory_id: UUID) -> bool:
    return (
        await db.scalar(
            select(VectorErasureJob.id).where(VectorErasureJob.memory_id == memory_id).limit(1)
        )
        is not None
    )
