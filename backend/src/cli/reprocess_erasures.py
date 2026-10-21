"""Re-process account erasures that ran before the #2235 fix (#2243).

Erasures completed on v0.113.0 through v0.120.x could leave an erased
member's ``details`` behind on memories other members authored: in the
prior of the next ``details`` edit after theirs in the revision log (the
#2235 correlation bug), and as the live value where the erased member made
the newest ``details`` edit (fixed by #2244 for erasures from v0.123.0 on).
This command applies today's rules to every completed erasure, by the
pseudonym the request row stores. Runs where the API runs (same env:
DATABASE_URL, QDRANT_*), e.g. inside the API container::

    python -m src.cli.reprocess_erasures --all                     # dry run (default, read-only)
    python -m src.cli.reprocess_erasures --all --apply --yes       # write
    python -m src.cli.reprocess_erasures --request <uuid> --apply  # one erasure request

Per completed erasure request, one transaction:

1. where the erased member wrote the live ``details`` of a memory someone
   else authored, the last value someone else wrote is put back (a new
   version, one ``erasure`` revision row, the vector payload's ``location``
   patched before the commit);
2. the erased member's ``details`` leave every revision prior that still
   holds them; on memories they authored, only rows that existed when the
   erasure completed are touched — what other members wrote there since
   stays as it is.

A request that changed gets one ``account_erasure_reprocess`` audit row
(counts only). The command reads the pseudonym stored on the request and
never derives one; a request that still stores a raw account id (completed
before v0.58.1, before the revision log existed) is skipped and counted.
Running it again changes nothing. The dry run writes nothing and makes no
vector-store call; it prints request ids and counts, never values.

Exit codes: 0 ok · 1 error (a vector-store failure rolls back that request
and stops the run; the requests before it stay done, and a re-run picks up
the rest).
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from cli._oneshot import add_log_level_argument, configure_logging, run_plan_apply  # noqa: E402
from models.auth import AuditLog  # noqa: E402
from models.erasure import STATUS_COMPLETE, ErasureRequest  # noqa: E402
from services.erasure_details import restore_editor_details, strip_subject_details  # noqa: E402
from utils.datetime import to_utc_iso  # noqa: E402
from utils.hashing import SHA256_HEX_PATTERN  # noqa: E402
from utils.logger import get_logger  # noqa: E402

logger = get_logger(__name__)

AUDIT_ACTION = "account_erasure_reprocess"
# The erasure's own audit row: its user_email is the email pseudonym.
ERASURE_AUDIT_ACTION = "account_erasure"
TOOL = "cli.reprocess_erasures"
SKIPPED_NOT_PSEUDONYMIZED = "not pseudonymized (completed before v0.58.1)"
SKIPPED_NO_COMPLETION_TIME = "no completion time"

_PSEUDONYM = re.compile(SHA256_HEX_PATTERN)


@dataclass
class RequestChange:
    """What re-processing one completed erasure request changed (or would)."""

    request_id: UUID
    completed_at: datetime
    details_restored: int
    details_restored_unavailable: int
    details_stripped: int
    scrub_marked: int
    payload_synced: int = 0
    payload_skipped: int = 0
    embedding_requeued: int = 0

    @property
    def changes(self) -> int:
        return (
            self.details_restored
            + self.details_restored_unavailable
            + self.details_stripped
            + self.scrub_marked
        )


@dataclass
class ReprocessResult:
    """One pass over the completed erasure requests in scope."""

    dry_run: bool
    scanned: int = 0
    changed: list[RequestChange] = field(default_factory=list)
    skipped: list[tuple[UUID, str]] = field(default_factory=list)

    @property
    def changes(self) -> int:
        return sum(change.changes for change in self.changed)


def _naive_utc(value: datetime) -> datetime:
    """``memory_revisions.at`` is naive UTC; ``completed_at`` is aware."""
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


async def _email_pseudonym(db: AsyncSession, pseudonym: str) -> str:
    """The email pseudonym the erasure's audit row carries, else ``pseudonym``."""
    email = (
        await db.execute(
            select(AuditLog.user_email)
            .where(AuditLog.action == ERASURE_AUDIT_ACTION, AuditLog.user_id == pseudonym)
            .order_by(AuditLog.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return email or pseudonym


async def _reprocess_one(
    db: AsyncSession, request_id: UUID, pseudonym: str, completed_at: datetime, dry_run: bool
) -> RequestChange:
    """Restore, then strip, by ``pseudonym``; commit (or roll back a dry run).

    The order of the live erasure (``_delete_postgres``). Never the
    pseudonymize step: with the pseudonym as the subject it would hash the
    hash.
    """
    try:
        restore = await restore_editor_details(db, pseudonym, actor=pseudonym, dry_run=dry_run)
        strip = await strip_subject_details(
            db, pseudonym, existed_at=_naive_utc(completed_at), dry_run=dry_run
        )
        change = RequestChange(
            request_id=request_id,
            completed_at=completed_at,
            details_restored=restore.restored,
            details_restored_unavailable=restore.unavailable,
            details_stripped=strip.details_stripped,
            scrub_marked=strip.scrub_marked,
            payload_synced=restore.payload_synced,
            payload_skipped=restore.payload_skipped,
            embedding_requeued=restore.embedding_requeued,
        )
        if dry_run or not change.changes:
            await db.rollback()
            return change
        db.add(
            AuditLog(
                user_email=await _email_pseudonym(db, pseudonym),
                user_id=pseudonym,
                action=AUDIT_ACTION,
                resource=f"user_pseudonym:{pseudonym[:16]}",
                user_metadata={
                    "request_id": str(request_id),
                    "details_restored": change.details_restored,
                    "details_restored_unavailable": change.details_restored_unavailable,
                    "details_stripped": change.details_stripped,
                    "scrub_marked": change.scrub_marked,
                    "details_payload_synced": change.payload_synced,
                    "details_payload_skipped": change.payload_skipped,
                    "details_embedding_requeued": change.embedding_requeued,
                    "tool": TOOL,
                },
            )
        )
        await db.commit()
    except BaseException:
        await db.rollback()
        raise
    logger.info(
        "erasure_reprocessed",
        request_id=str(request_id),
        details_restored=change.details_restored,
        details_restored_unavailable=change.details_restored_unavailable,
        details_stripped=change.details_stripped,
        scrub_marked=change.scrub_marked,
    )
    return change


async def reprocess_erasures(
    db: AsyncSession, *, request_id: UUID | None = None, dry_run: bool = True
) -> ReprocessResult:
    """Re-apply today's erasure rules to completed erasure requests.

    One transaction per request, oldest first. Idempotent: a second run
    changes 0 rows, writes no revision or audit row and makes no
    vector-store call.

    Args:
        db: The session. Each request's work is committed (or, on a dry run
            or with nothing to change, rolled back) before the next.
        request_id: Only this request; None for every completed one.
        dry_run: Count what would change; write nothing, call no vector
            store.

    Returns:
        The requests with changes, the skipped ones, and how many were read.

    Raises:
        LookupError: ``request_id`` is not a completed erasure request.
        QdrantError: A payload patch failed; that request was rolled back.
    """
    stmt = (
        select(ErasureRequest.id, ErasureRequest.user_id, ErasureRequest.completed_at)
        .where(ErasureRequest.status == STATUS_COMPLETE)
        .order_by(ErasureRequest.completed_at, ErasureRequest.id)
    )
    if request_id is not None:
        stmt = stmt.where(ErasureRequest.id == request_id)
    requests = (await db.execute(stmt)).all()
    await db.rollback()
    if request_id is not None and not requests:
        raise LookupError(f"{request_id} is not a completed erasure request")

    result = ReprocessResult(dry_run=dry_run, scanned=len(requests))
    for request in requests:
        if not _PSEUDONYM.fullmatch(request.user_id):
            result.skipped.append((request.id, SKIPPED_NOT_PSEUDONYMIZED))
            continue
        if request.completed_at is None:
            result.skipped.append((request.id, SKIPPED_NO_COMPLETION_TIME))
            continue
        change = await _reprocess_one(
            db, request.id, request.user_id, request.completed_at, dry_run
        )
        if change.changes:
            result.changed.append(change)
    return result


def _print_plan(result: ReprocessResult) -> None:
    if result.changed:
        print(f"{'request':36}  {'completed at':20}  restored  unavailable  stripped  scrub_marked")
    for c in result.changed:
        print(
            f"{str(c.request_id):36}  {to_utc_iso(c.completed_at):20}  "
            f"{c.details_restored:8}  {c.details_restored_unavailable:11}  "
            f"{c.details_stripped:8}  {c.scrub_marked:12}"
        )
    for request_id, reason in result.skipped:
        print(f"skipped  {request_id}  {reason}")
    verb = "would change" if result.dry_run else "changed"
    print(
        f"{result.scanned} completed erasure(s): {verb} {len(result.changed)}, "
        f"skipped {len(result.skipped)}"
    )


def _print_applied(result: ReprocessResult) -> None:
    for c in result.changed:
        if c.details_restored or c.details_restored_unavailable:
            print(
                f"payload  {c.request_id}  synced {c.payload_synced}, "
                f"skipped {c.payload_skipped}, re-queued for embedding {c.embedding_requeued}"
            )


def _summary(result: ReprocessResult) -> str:
    restored = sum(c.details_restored for c in result.changed)
    unavailable = sum(c.details_restored_unavailable for c in result.changed)
    stripped = sum(c.details_stripped for c in result.changed)
    marked = sum(c.scrub_marked for c in result.changed)
    return (
        f"{len(result.changed)} erasure request(s): {restored} memory details restored, "
        f"{unavailable} cleared (unavailable), {stripped} revision prior(s) stripped, "
        f"{marked} marked scrubbed"
    )


async def _main(args: argparse.Namespace) -> int:
    configure_logging(args.log_level)

    async def run(db: AsyncSession, dry_run: bool) -> ReprocessResult:
        return await reprocess_erasures(db, request_id=args.request, dry_run=dry_run)

    return await run_plan_apply(
        run=run,
        print_plan=_print_plan,
        changes=lambda result: result.changes,
        noun="erasure request",
        summary=_summary,
        print_applied=_print_applied,
        apply=args.apply,
        assume_yes=args.yes,
    )


def _parse(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--all", action="store_true", help="every completed erasure request")
    scope.add_argument("--request", type=UUID, help="one completed erasure request (its id)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--plan",
        "--dry-run",
        action="store_true",
        help="dry run: print what would change, write nothing (default)",
    )
    mode.add_argument("--apply", action="store_true", help="write the changes")
    parser.add_argument("--yes", action="store_true", help="no confirmation prompt")
    add_log_level_argument(parser)
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(_parse())))
