"""Counts-first historical erasure repair (#2391).

python -m src.cli.cleanup_erased_vectors              # plan, no writes
python -m src.cli.cleanup_erased_vectors --apply --yes # suppress and queue

Uses only finalized erasures and captured UUID scopes. Reports aggregate
counts, never raw identifiers, payloads, emails, document names or point IDs.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from cli._oneshot import configure_logging, run_plan_apply
from config.constants import ERASED_AUTHOR_EMBEDDING_ERROR, MAX_EMBEDDING_RETRIES
from db.qdrant import erasure_point_identities, list_memory_collections
from models.auth import User
from models.memory import Memory
from models.vector_erasure import VectorErasureJob
from services.erasure_details import erasure_pseudonym, historical_erasure_boundaries
from services.vector_erasure import capture_vector_erasure, drain_vector_erasures
from utils.datetime import utcnow


@dataclass
class CleanupResult:
    survivors: int = 0
    scopes: int = 0
    pending: int = 0
    ambiguous: int = 0

    @property
    def changes(self) -> int:
        return self.survivors + self.scopes + self.pending


async def cleanup_erased_vectors(db: AsyncSession, dry_run: bool = True) -> CleanupResult:
    boundaries = await historical_erasure_boundaries(db)
    by_subject = {subject: value[0] for subject, value in boundaries.items()}
    out = CleanupResult()
    if not by_subject:
        return out
    trusted_ids = set(
        (
            await db.execute(
                select(VectorErasureJob.memory_id).where(
                    VectorErasureJob.subject_pseudonym.in_(by_subject),
                    VectorErasureJob.captured_before_erasure.is_(True),
                )
            )
        ).scalars()
    )
    # Explicit restoration is durable and must survive subsequent repairs.
    conditions = (
        or_(
            *(
                and_(
                    Memory.user_id == subject,
                    or_(Memory.created_at <= cutoff, Memory.id.in_(trusted_ids)),
                )
                for subject, (_request, cutoff) in boundaries.items()
            )
        ),
        Memory.embedding_suppressed_at.is_(None),
        Memory.embedding_erasure_restored_at.is_(None),
    )
    ids = list(
        (await db.execute(select(Memory.id).where(*conditions).order_by(Memory.id))).scalars()
    )
    out.survivors = len(ids)
    existing = set(
        (
            await db.execute(
                select(
                    VectorErasureJob.request_id,
                    VectorErasureJob.memory_id,
                    VectorErasureJob.point_id,
                )
            )
        ).all()
    )
    scopes: set[tuple[UUID, UUID, UUID, str]] = set()
    for collection in await list_memory_collections():
        async for batch in erasure_point_identities(collection):
            for point_id, author, memory_id, written_at in batch:
                subject = author if author in by_subject else erasure_pseudonym(author)
                if subject not in by_subject:
                    continue
                try:
                    memory_uuid, point_uuid = UUID(memory_id or point_id), UUID(point_id)
                except (ValueError, TypeError):
                    continue
                # Chunk scopes must name the row's summary point too; the
                # scanner matches every chunk_of for that UUID at drain time.
                current = (
                    await db.execute(
                        select(
                            Memory.created_at,
                            Memory.summary_embedding_id,
                            Memory.user_id,
                            Memory.embedding_suppressed_at,
                            Memory.embedding_erasure_restored_at,
                        ).where(Memory.id == memory_uuid, Memory.deleted_at.is_(None))
                    )
                ).one_or_none()
                if current is not None:
                    if (
                        current.user_id == subject
                        and current.created_at > boundaries[subject][1]
                        and memory_uuid not in trusted_ids
                    ):
                        out.ambiguous += 1
                        continue
                    if current.embedding_erasure_restored_at is not None:
                        continue
                    if (
                        current.user_id == author
                        and current.embedding_suppressed_at is None
                        and author != subject
                        and await db.scalar(select(User.id).where(User.user_id == author))
                        is not None
                    ):
                        continue
                    point_uuid = current.summary_embedding_id or memory_uuid
                elif (
                    author == subject
                    and memory_uuid not in trusted_ids
                    and (written_at is None or written_at > boundaries[subject][1])
                ):
                    # No row/UUID evidence and no pre-erasure timestamp: leave
                    # ambiguous new indexer data alone.
                    out.ambiguous += 1
                    continue
                scopes.add((by_subject[subject], memory_uuid, point_uuid, subject))
    new_scopes = [scope for scope in scopes if scope[:3] not in existing]
    out.scopes = len(new_scopes)
    out.pending = len(
        list(
            (
                await db.execute(
                    select(VectorErasureJob.id).where(VectorErasureJob.completed_at.is_(None))
                )
            ).scalars()
        )
    )
    if dry_run:
        await db.rollback()
        return out
    if ids:
        locked = (
            select(Memory.id)
            .where(Memory.id.in_(ids), *conditions)
            .order_by(Memory.id)
            .with_for_update(key_share=True)
        )
        await db.execute(
            update(Memory)
            .where(Memory.id.in_(locked))
            .values(
                embedding_suppressed_at=utcnow(),
                embedding_status="failed",
                embedding_error=ERASED_AUTHOR_EMBEDDING_ERROR,
                embedding_retry_count=MAX_EMBEDDING_RETRIES,
                embedding_attempted_at=utcnow(),
            )
            .execution_options(synchronize_session=False)
        )
    for subject, request_id in by_subject.items():
        await capture_vector_erasure(
            db, request_id, subject, pseudonym=True, existed_at=boundaries[subject][1]
        )
    for request_id, memory_id, point_id, subject in new_scopes:
        await db.execute(
            insert(VectorErasureJob)
            .values(
                id=uuid4(),
                request_id=request_id,
                memory_id=memory_id,
                point_id=point_id,
                subject_pseudonym=subject,
                captured_before_erasure=False,
            )
            .on_conflict_do_nothing(constraint="uq_vector_erasure_scope")
        )
    await db.commit()
    result = await drain_vector_erasures(db)
    out.pending = result["pending"]
    return out


async def _main(args: argparse.Namespace) -> int:
    # Fixed verbosity: payload/SQL adapter debug output can expose identifiers.
    configure_logging("ERROR")
    if args.yes and not args.apply:
        print("error: --yes needs --apply", file=sys.stderr)
        return 2

    async def run(db, dry_run):
        try:
            return await cleanup_erased_vectors(db, dry_run=dry_run)
        except Exception as exc:
            raise RuntimeError(type(exc).__name__) from None

    try:
        return await run_plan_apply(
            run=run,
            print_plan=lambda result: print(
                f"survivors={result.survivors} scopes={result.scopes} "
                f"pending={result.pending} ambiguous={result.ambiguous}"
            ),
            changes=lambda result: result.changes,
            noun="erasure repair",
            apply=args.apply,
            assume_yes=args.yes,
        )
    except Exception as exc:
        print(f"error_class={type(exc).__name__}", file=sys.stderr)
        return 1


def _parse(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--yes", action="store_true")
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main(_parse())))
