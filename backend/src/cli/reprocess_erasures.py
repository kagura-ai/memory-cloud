"""Re-process account erasures that ran before the #2235 fix (#2243).

Erasures completed on v0.113.0 through v0.122.x could leave an erased
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

Per erasure request that ran in Postgres (``complete``, or ``failed`` after
its Postgres step committed), oldest first, one transaction:

1. where the erased member wrote the live ``details`` of a memory someone
   else authored, the last value someone else wrote is put back (a new
   version, one ``erasure`` revision row; the vector payload's ``location``
   is patched right after the commit, and a patch that fails re-queues the
   memory for embedding, which rewrites the payload);
2. the erased member's ``details`` leave every revision prior that still
   holds them; on memories they authored, only rows that existed when the
   erasure locked them (``memory_scrub_at`` in the request's summary, since
   v0.124.0; else its completion time) are touched — what other members
   wrote there since stays as it is.

The walk of 1 goes back past every erased member's edits (#2268) to the last
value a surviving member wrote, so the result does not depend on which of
two erasures that edited the same memory is processed first.

A request that changed gets one ``account_erasure_reprocess`` audit row
(counts only). The command uses the pseudonym stored on the request. The one
exception is a request that failed after its Postgres step on a version
before v0.124.0 and still stores the account id: it is ``repaired`` — its
pseudonym is derived with the deployment's salt (the erasure's own
derivation), stored on the row with the request metadata cleared, and the
request is then processed by it. Two kinds of completed request are
skipped and counted: one the sweep marked complete without running it (the
user row was already gone; it stores the account id, or since v0.125.0 its
pseudonym, #2286), and one finalized before v0.58.1 that stores the account
id (before the revision log of v0.113.0 anyway). A request the sweep found
interrupted after its Postgres step (``after_postgres_commit: interrupted:
sweep``, #2286) ran and is processed like any other. A request that failed
before its Postgres step erased nothing and is not listed. On a skipped note
row the sweep pseudonymized (since v0.125.0) the subject's audit rows that
still carry the account id are pseudonymized all the same (below): a run
interrupted on v0.124.x after its Postgres commit, before its audit-log step,
ends up there.

The subject's own ``audit_logs`` rows that still carry the account id
(#2287: the audit-log step failed after the Postgres commit, on v0.124.x or
before; since v0.125.0 that step is part of the Postgres transaction) are
pseudonymized in the request's transaction: an ``audit_logs.user_id`` no
user row has whose pseudonym is the request's is the subject's account id,
and the emails are those rows' ``user_email`` values whose sha256 is the
``user_email_hash`` of one of the account's requests (left as they are when
none is). An account id that belongs to a user again (re-registered) is
never matched: its rows are kept (``user_id`` stays) and the request is
listed — but a row of theirs that carries the erased address in
``user_email`` (not in use) is matched by the mention sweep below: that
address becomes its pseudonym and the row's IP / user agent are cleared. Running it again changes
nothing.

Other actors' rows that name the subject (#2298) — an admin acting on them
(``resource = 'user:'||email``), or the subject acting as an admin on
someone else (``user_email = email``) — get the same pseudonyms, by exact
value only, once per pseudonym per run, in the request's transaction. The
emails are the account's: those whose sha256 is the ``user_email_hash`` of
one of its requests, found among the ``user_email`` values and the
``user:`` resources of the table. An email a user row holds again (the
address re-registered) is never rewritten: rows naming it may be the new
account's; the request is listed. The dry run writes and locks nothing and makes no
vector-store call; it prints request ids and counts, never values or
account ids.

The subject's request metadata (#2345): the audit rows that carry the
pseudonym lose their IP and user agent, and the ``account_erasure`` row's
``initiated_by`` becomes the pseudonym where it was the subject's own
account id (an admin's id stays) — once per pseudonym per run, for requests
that store the pseudonym (the repaired legacy row included; the skipped
ones are not).

Exit codes: 0 ok · 1 error (the run stops at that request; the requests
before it stay done — each is logged to stderr as ``erasure_reprocessed``
and has its audit row — and a re-run picks up the rest) · 2 usage (``--yes``
without ``--apply``). A vector-store failure is not an error: the memory is
re-queued for embedding.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import defaultdict
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import exists, func, select, update  # noqa: E402
from sqlalchemy.engine import CursorResult  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from cli._oneshot import add_log_level_argument, configure_logging, run_plan_apply  # noqa: E402
from models.auth import AuditLog, User  # noqa: E402
from models.erasure import (  # noqa: E402
    FAILURE_AFTER_POSTGRES_COMMIT,
    STATUS_COMPLETE,
    ErasureRequest,
)
from models.memory import Memory, MemoryRevision  # noqa: E402
from services.erasure_details import (  # noqa: E402
    ERASURE_AUDIT_ACTION,
    RestoreReport,
    StripCounts,
    SubjectDetailsErasure,
    clear_audit_request_metadata,
    count_audit_mentions,
    erase_subject_details,
    erased_pseudonyms,
    erasure_pseudonym,
    finalized_erasure,
    is_pseudonym,
    note_row_pseudonymized,
    payload_counts,
    pseudonymize_audit_trail,
    pseudonymize_erasure_audit_initiator,
    pseudonymized_request_values,
    sync_restored_payloads,
    unrepaired_erasure,
)
from utils.datetime import to_naive_utc, to_utc_iso  # noqa: E402
from utils.hashing import sha256_hex  # noqa: E402
from utils.logger import get_logger  # noqa: E402

logger = get_logger(__name__)

AUDIT_ACTION = "account_erasure_reprocess"
TOOL = "cli.reprocess_erasures"
# Neither ``user_id`` is used or printed: the account id (the second one, and
# the first one before v0.125.0) or a pseudonym of an erasure that never ran.
SKIPPED_NOT_RUN = "never ran: marked complete because the user row was already gone"
SKIPPED_NOT_PSEUDONYMIZED = "stores the account id: finalized before v0.58.1"
# #2268: never prints the account id it replaces
REPAIRED = "failed after its Postgres step: the account id is replaced by its pseudonym"
REPAIRED_REASON = "failed before v0.124.0; the original reason is removed (it may name the account)"
# the derived pseudonym is on no row: none survived, or the salt is not the
# erasure's — the account id is not overwritten with a value nothing carries
SKIPPED_NO_ROWS = (
    "failed after its Postgres step: its summary counts memory or revision rows it "
    "pseudonymized, but none carries the derived pseudonym (the salt changed?); "
    "left as it is"
)
# #2287: never prints the account id or the email
AUDIT_KEPT = "rows kept: the account id is in use again"
# #2298: never prints the email
MENTIONS_KEPT = "rows kept: the email is in use again"
# the ``resource`` prefix of a row about a member (``user:{email}``)
USER_RESOURCE = "user:"
# the sweep's crash cutoff (``sweep_pending_erasures``): a run is over by then
STALE_RUN = timedelta(hours=1)


@dataclass
class RequestChange:
    """What re-processing one erasure request changed (or would): a completed one, or one that failed after its Postgres step."""

    request_id: UUID
    # None on a request that failed after its Postgres step (never completed)
    completed_at: datetime | None
    details_restored: int
    details_restored_unavailable: int
    details_stripped: int
    scrub_marked: int
    embedding_requeued: int = 0
    # #2266: ``sync_restored_payloads``'s outcome after the commit (the
    # ``details_payload_*`` keys of the erasure summary); empty on a dry run
    payload: dict[str, int] = field(default_factory=dict)
    # #2268: the request row stored the account id and gets the pseudonym
    repaired: bool = False
    # #2287: the subject's audit rows that still carried the account id
    audit_logs_pseudonymized: int = 0
    # whether their email was found (``_recover_email``); None: no such rows
    audit_email_recovered: bool | None = None
    # #2298: other actors' rows that named the subject's email or sub
    audit_mentions_pseudonymized: int = 0
    # an email of the subject's is in use again: rows naming it were kept
    audit_mentions_kept: bool = False
    # #2345: rows of the pseudonym whose IP / user agent were cleared, plus
    # the ``account_erasure`` rows whose ``initiated_by`` was pseudonymized
    audit_metadata_cleared: int = 0

    @property
    def changes(self) -> int:
        return (
            self.details_restored
            + self.details_restored_unavailable
            + self.details_stripped
            + self.scrub_marked
            + int(self.repaired)
            + self.audit_logs_pseudonymized
            + self.audit_mentions_pseudonymized
            + self.audit_metadata_cleared
        )


@dataclass
class ReprocessResult:
    """One pass over the erasure requests in scope."""

    dry_run: bool
    scanned: int = 0
    changed: list[RequestChange] = field(default_factory=list)
    skipped: list[tuple[UUID, str]] = field(default_factory=list)
    # #2287: requests whose account id is in use again: audit rows kept
    audit_kept: list[UUID] = field(default_factory=list)
    # #2298: requests with an email in use again: the rows naming it kept
    mentions_kept: list[UUID] = field(default_factory=list)

    @property
    def changes(self) -> int:
        return sum(change.changes for change in self.changed)


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


@dataclass(frozen=True)
class AuditSubs:
    """The ``audit_logs.user_id`` values, keyed by their pseudonym (#2287)."""

    # no user row has them: an erased subject's residue is found here
    orphans: dict[str, str]
    # a user row has them: never matched, even when an erasure's pseudonym is theirs
    in_use: frozenset[str]


async def _audit_subs(db: AsyncSession) -> AuditSubs:
    """Every distinct ``audit_logs.user_id``, keyed by its pseudonym (the salt
    in use). A request's subject whose audit rows still carry the account id
    is found by the request's pseudonym among the orphans; an account id in
    use again (re-registered) is not an orphan, so a live account's rows are
    never rewritten. Pseudonymized values are orphans too (the hash of a
    hash) and match nothing; with a salt other than the erasure's, nothing
    matches either."""
    rows = await db.execute(
        select(
            AuditLog.user_id,
            exists().where(User.user_id == AuditLog.user_id).label("in_use"),
        ).distinct()
    )
    orphans: dict[str, str] = {}
    in_use: set[str] = set()
    for sub, live in rows.all():
        pseudonym = erasure_pseudonym(sub)
        if live:
            in_use.add(pseudonym)
        else:
            orphans[pseudonym] = sub
    return AuditSubs(orphans, frozenset(in_use))


async def _count_audit_rows(db: AsyncSession, account_id: str) -> int:
    """What ``pseudonymize_audit_logs(..., orphaned_only=True)`` would change."""
    return (
        await db.execute(
            select(func.count(AuditLog.id)).where(
                AuditLog.user_id == account_id,
                ~exists().where(User.user_id == AuditLog.user_id),
            )
        )
    ).scalar_one()


async def _recover_emails(
    db: AsyncSession, account_id: str, email_hashes: Collection[str]
) -> list[str]:
    """The subject's emails among the ``user_email`` values of their own audit
    rows: those whose unsalted sha256 is the ``user_email_hash`` of one of
    the account's requests (the email at each request's time; two when the
    account was erased, re-created with the same id under another email and
    erased again). Empty when no row carries one (the subject never acted
    under it, or changed it before the request) — then ``user_email`` stays
    and only the account id is replaced. Rows written under an address no
    request was made under keep it."""
    candidates = (
        (
            await db.execute(
                select(AuditLog.user_email).distinct().where(AuditLog.user_id == account_id)
            )
        )
        .scalars()
        .all()
    )
    return sorted(email for email in candidates if sha256_hex(email) in email_hashes)


def _sha256_hex_sql(value: Any) -> Any:
    """``sha256_hex(value)`` (no salt) in SQL: what ``user_email_hash`` stores."""
    return func.encode(func.sha256(func.convert_to(value, "UTF8")), "hex")


async def _mentioned_emails(db: AsyncSession, email_hashes: Collection[str]) -> dict[str, str]:
    """The emails audit rows name whose unsalted sha256 is among
    ``email_hashes`` (every in-scope request's ``user_email_hash``), keyed by
    that hash (#2298). Read once per run: the ``user_email`` values and the
    ``user:`` resources, hashed and matched in the database (only matches
    come back). A pseudonym-shaped value hashes to no request's hash;
    nothing is printed or logged."""
    if not email_hashes:
        return {}
    wanted = sorted(set(email_hashes))
    email = AuditLog.user_email
    named = func.substr(AuditLog.resource, len(USER_RESOURCE) + 1)
    values = [
        *(
            await db.execute(select(email).distinct().where(_sha256_hex_sql(email).in_(wanted)))
        ).scalars(),
        *(
            await db.execute(
                select(named)
                .distinct()
                .where(
                    AuditLog.resource.startswith(USER_RESOURCE, autoescape=True),
                    _sha256_hex_sql(named).in_(wanted),
                )
            )
        ).scalars(),
    ]
    # the database matched them; the digest is the key
    return {sha256_hex(value): value for value in values}


async def _live_emails(db: AsyncSession, emails: Collection[str]) -> set[str]:
    """``emails`` a user row holds (the address re-registered, #2298)."""
    if not emails:
        return set()
    return set(
        (await db.execute(select(User.email).where(User.email.in_(sorted(emails))))).scalars()
    )


def _existed_at(
    summary: dict[str, Any] | None,
    completed_at: datetime | None,
    started_at: datetime | None = None,
) -> datetime | None:
    """The bound of the authored arm of the strip (``_authored_by``), naive UTC.

    ``memory_scrub_at`` (#2268): when the erasure locked the subject's
    memories — every row it saw is older, every later edit waited for its
    commit. A request from before v0.124.0 has only ``completed_at``, a few
    statements later (a row written in between holds at most the scrubbed
    value; erring that way only marks it). A request that failed after its
    Postgres step before v0.124.0 has neither: its ``started_at`` plus an
    hour (#2268 review) — the sweep's own crash cutoff, so the scrub ran
    before it and every row the erasure saw is older. Rows surviving members
    wrote on the subject's memories within that hour are stripped and marked
    too; later ones are left alone. Without ``started_at``, no bound.
    """
    postgres = (summary or {}).get("postgres")
    scrubbed = postgres.get("memory_scrub_at") if isinstance(postgres, dict) else None
    if isinstance(scrubbed, str):
        return to_naive_utc(datetime.fromisoformat(scrubbed))
    if completed_at is not None:
        return to_naive_utc(completed_at)
    if started_at is not None:
        return to_naive_utc(started_at) + STALE_RUN
    return None


def _wrote_pseudonym(summary: dict[str, Any] | None) -> bool:
    """The erasure's summary says it wrote the pseudonym on memory or revision
    rows (#2268 review). When it wrote none, no row can carry it and the
    repair goes ahead: the account id must not stay on the row."""
    postgres = (summary or {}).get("postgres")
    if not isinstance(postgres, dict):
        return False
    return any(
        isinstance(postgres.get(key), int) and postgres[key] > 0
        for key in ("memories_scrubbed", "memory_revisions_pseudonymized")
    )


async def _carries(db: AsyncSession, pseudonym: str) -> bool:
    """Some memory or revision row carries ``pseudonym`` (#2268 review): the
    derivation matches what the erasure wrote (same salt)."""
    memory = select(Memory.id).where(Memory.user_id == pseudonym).limit(1)
    revision = select(MemoryRevision.id).where(MemoryRevision.actor_user_id == pseudonym).limit(1)
    for probe in (memory, revision):
        if (await db.execute(probe)).first() is not None:
            return True
    return False


async def _repair(db: AsyncSession, request_id: UUID, account_id: str) -> bool:
    """Give a request that failed after its Postgres step (before v0.124.0)
    what the failure handler writes since #2268: the pseudonym instead of
    the account id, no request metadata, and the marker in
    ``failure_reason``. Scoped to the one row, by its id and stored value.
    The values are ``pseudonymized_request_values``'s (#2288); the row is not
    read, so ``initiated_by`` is decided in SQL.

    The old ``failure_reason`` is replaced, not kept: an exception text (a
    constraint's ``DETAIL``, a Redis key) can name the account.

    Returns:
        Whether the row was repaired (False when another run got there first).
    """
    result = await db.execute(
        update(ErasureRequest)
        .where(ErasureRequest.id == request_id, ErasureRequest.user_id == account_id)
        .values(
            **pseudonymized_request_values(account_id, None),
            failure_reason=FAILURE_AFTER_POSTGRES_COMMIT + REPAIRED_REASON,
        )
    )
    return bool(cast(CursorResult[Any], result).rowcount)


async def _reprocess_one(
    db: AsyncSession,
    request_id: UUID,
    pseudonym: str,
    completed_at: datetime | None,
    existed_at: datetime | None,
    dry_run: bool,
    erased: frozenset[str],
    *,
    account_id: str | None = None,
    audit_account_id: str | None = None,
    email_hashes: Collection[str] = (),
    audit_only: bool = False,
    mentioned: dict[str, str] | None = None,
    swept_emails: set[str] | None = None,
) -> RequestChange:
    """Repair the row (``account_id``), restore, then strip, by ``pseudonym``,
    then the audit rows (the live erasure's lock order: memories first);
    commit (or roll back a dry run); then patch the restored memories'
    payloads (``sync_restored_payloads``).

    The order of the live erasure (``_delete_postgres``, then ``_execute``'s
    payload step): no vector-store call while the memories are locked. Never the
    pseudonymize step: with the pseudonym as the subject it would hash the
    hash. ``erased`` are every erased member's pseudonyms
    (``erased_pseudonyms``): the restore walks past their edits too.
    ``account_id`` is set only for a request ``unrepaired_erasure`` matches:
    its row gets the pseudonym in the same transaction.
    ``audit_account_id`` (#2287) is the subject's account id found among the
    orphaned audit subs (``_audit_subs``): their audit rows are pseudonymized
    in the same transaction, the emails recovered by ``email_hashes`` (every
    request of the account's). ``audit_only`` (#2286, a note row the sweep
    pseudonymized): that audit scrub alone — the request never reached the
    Postgres step as far as its row says, so there is nothing to restore or
    strip by its pseudonym. ``mentioned`` (#2298, the run's
    ``_mentioned_emails``; None once this pseudonym's sweep ran this run):
    other actors' rows naming one of the account's emails (``email_hashes``)
    or its account id are pseudonymized in the same transaction, under one
    lock with the subject's own rows; an email a user row holds is left.
    The same first time (#2345), the rows that carry ``pseudonym`` lose
    their request metadata (``clear_audit_request_metadata``,
    ``pseudonymize_erasure_audit_initiator``) — every caller passes
    ``mentioned`` for a request that stores (or, repaired, gets) the
    pseudonym, and only then. ``swept_emails`` (the run's): emails an
    earlier request of the run already swept are left out of the mentions.
    """
    try:
        repaired = account_id is not None
        if repaired and not dry_run:
            repaired = await _repair(db, request_id, cast(str, account_id))
        # the live erasure's order (#2298 review): the subject's memories are
        # locked first (``erase_subject_details``), the audit rows after —
        # one cross-table order for both paths
        details = (
            SubjectDetailsErasure(RestoreReport(), StripCounts(0, 0))
            if audit_only
            else await erase_subject_details(
                db,
                pseudonym,
                actor=pseudonym,
                erased_actors=erased - {pseudonym},
                existed_at=existed_at,
                dry_run=dry_run,
            )
        )
        audited, mentions, email_recovered, mentions_kept = 0, 0, None, False
        emails: list[str] = []
        if audit_account_id is not None:
            emails = await _recover_emails(db, audit_account_id, email_hashes)
        mention_emails: set[str] = set()
        if mentioned is not None:
            mention_emails = set(emails) | {mentioned[h] for h in email_hashes if h in mentioned}
            # the subject's own rows (by the account id) take every email of
            # theirs, so no own row is left for the mention UPDATE (the dry
            # run's count leaves them all out)
            emails = sorted(mention_emails)
            live = await _live_emails(db, mention_emails)
            mention_emails -= live
            mentions_kept = bool(live)
            if swept_emails is not None:
                # an address two erased accounts held: swept under the first
                # request of the run, in the plan as when applied
                mention_emails -= swept_emails
                swept_emails |= mention_emails
        subs = [audit_account_id] if audit_account_id is not None and mentioned is not None else []
        if dry_run:
            if audit_account_id is not None:
                audited = await _count_audit_rows(db, audit_account_id)
            if mentioned is not None:
                # the apply rewrites the own rows first: none is counted twice
                mentions = await count_audit_mentions(
                    db, mention_emails, subs=subs, exclude_user_id=audit_account_id
                )
        else:
            # one lock phase over every audit row this request rewrites
            audited, mentions = await pseudonymize_audit_trail(
                db,
                audit_account_id,
                emails,
                orphaned_only=True,
                pseudonym=pseudonym if mentioned is not None else None,
                mention_emails=mention_emails,
                mention_subs=subs,
            )
        metadata_cleared = 0
        if mentioned is not None:
            # rows, not edits: one row can lose its IP and get the initiator
            cleared = await clear_audit_request_metadata(db, pseudonym, dry_run=dry_run)
            initiators = await pseudonymize_erasure_audit_initiator(db, pseudonym, dry_run=dry_run)
            metadata_cleared = len(cleared | initiators)
        if audited:
            email_recovered = bool(emails)
        restore, strip = details.restore, details.strip
        change = RequestChange(
            request_id=request_id,
            completed_at=completed_at,
            details_restored=restore.restored,
            details_restored_unavailable=restore.unavailable,
            details_stripped=strip.details_stripped,
            scrub_marked=strip.scrub_marked,
            embedding_requeued=restore.embedding_requeued,
            repaired=repaired,
            audit_logs_pseudonymized=audited,
            audit_email_recovered=email_recovered,
            audit_mentions_pseudonymized=mentions,
            audit_mentions_kept=mentions_kept,
            audit_metadata_cleared=metadata_cleared,
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
                    "details_embedding_requeued": change.embedding_requeued,
                    "request_repaired": change.repaired,
                    "audit_logs_pseudonymized": change.audit_logs_pseudonymized,
                    "audit_mentions_pseudonymized": change.audit_mentions_pseudonymized,
                    "audit_metadata_cleared": change.audit_metadata_cleared,
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
        request_repaired=change.repaired,
        audit_logs_pseudonymized=change.audit_logs_pseudonymized,
        audit_mentions_pseudonymized=change.audit_mentions_pseudonymized,
        audit_metadata_cleared=change.audit_metadata_cleared,
    )
    try:
        await sync_restored_payloads(db, restore.to_sync, restore)
    except BaseException:
        await db.rollback()
        raise
    change.payload = payload_counts(restore)
    logger.info("erasure_reprocessed_payloads", request_id=str(request_id), **change.payload)
    return change


async def reprocess_erasures(
    db: AsyncSession, *, request_id: UUID | None = None, dry_run: bool = True
) -> ReprocessResult:
    """Re-apply today's erasure rules to the erasure requests that ran.

    In scope: every ``complete`` request, and every ``failed`` one whose
    Postgres step committed (``deleted_data_summary.postgres``); a request
    that failed before it erased nothing. One transaction per request,
    oldest first. Idempotent: a second run changes 0 rows, writes no
    revision or audit row and makes no vector-store call.

    Args:
        db: The session. Each request's work is committed (or, on a dry run
            or with nothing to change, rolled back) before the next.
        request_id: Only this request; None for every one in scope.
        dry_run: Count what would change; write nothing, call no vector
            store.

    The subject's audit rows that still carry the account id are
    pseudonymized in the request's transaction (#2287, ``_audit_subs``),
    once per account id even when two requests share it; other actors' rows
    naming the subject (#2298, ``_mentioned_emails``), once per pseudonym.

    Returns:
        The requests with changes, the skipped ones, and how many were read.

    Raises:
        LookupError: ``request_id`` is not an erasure request in scope.
    """
    stmt = (
        select(
            ErasureRequest.id,
            ErasureRequest.user_id,
            ErasureRequest.completed_at,
            ErasureRequest.started_at,
            ErasureRequest.deleted_data_summary,
            ErasureRequest.user_email_hash,
            finalized_erasure().label("finalized"),
            unrepaired_erasure().label("unrepaired"),
            note_row_pseudonymized().label("note_pseudonymized"),
        )
        .where(
            (ErasureRequest.status == STATUS_COMPLETE) | unrepaired_erasure() | finalized_erasure()
        )
        .order_by(
            func.coalesce(
                ErasureRequest.completed_at,
                ErasureRequest.started_at,
                ErasureRequest.requested_at,
            ),
            ErasureRequest.id,
        )
    )
    # #2287 review: every request in scope, also under ``--request``: the
    # account's other requests carry its other emails, and once its audit
    # rows carry the pseudonym no later run can reach them again
    email_hashes: dict[str, set[str]] = defaultdict(set)
    for user_id, email_hash, unrepaired in (
        await db.execute(
            select(
                ErasureRequest.user_id,
                ErasureRequest.user_email_hash,
                unrepaired_erasure().label("unrepaired"),
            ).where(stmt.whereclause)
        )
    ).all():
        email_hashes[erasure_pseudonym(user_id) if unrepaired else user_id].add(email_hash)
    if request_id is not None:
        stmt = stmt.where(ErasureRequest.id == request_id)
    requests = (await db.execute(stmt)).all()
    erased = await erased_pseudonyms(db)
    audit_subs = await _audit_subs(db)
    mentioned = await _mentioned_emails(db, {h for hs in email_hashes.values() for h in hs})
    await db.rollback()
    if request_id is not None and not requests:
        raise LookupError(
            f"{request_id} is not an erasure request that ran in Postgres "
            "(complete, or failed after its Postgres step)"
        )

    result = ReprocessResult(dry_run=dry_run, scanned=len(requests))
    # the one request that pseudonymizes an account id's rows recovers every
    # email of the account (``email_hashes``, by the subject's pseudonym)
    audited: set[str] = set()
    # #2298: the pseudonyms whose mention sweep ran (once per run), and the
    # emails swept (an address two erased accounts held: once per run)
    swept: set[str] = set()
    swept_emails: set[str] = set()

    def mentions_of(pseudonym: str) -> dict[str, str] | None:
        """The run's mention map the first time ``pseudonym`` is processed."""
        if pseudonym in swept:
            return None
        swept.add(pseudonym)
        return mentioned

    def record(change: RequestChange) -> None:
        if change.audit_mentions_kept:
            result.mentions_kept.append(change.request_id)
        if change.changes:
            result.changed.append(change)

    def audit_account_id(request_id: UUID, pseudonym: str) -> str | None:
        """The subject's account id on audit rows, once per account id."""
        if pseudonym in audit_subs.in_use:
            result.audit_kept.append(request_id)
            return None
        account_id = audit_subs.orphans.get(pseudonym)
        if account_id is None or account_id in audited:
            return None
        audited.add(account_id)
        return account_id

    for request in requests:
        existed_at = _existed_at(
            request.deleted_data_summary, request.completed_at, request.started_at
        )
        if request.unrepaired:
            # #2268: failed after its Postgres step before v0.124.0; the
            # subject's rows carry the pseudonym, the request row the account id
            pseudonym = erasure_pseudonym(request.user_id)
            carries = await _carries(db, pseudonym)
            await db.rollback()
            if not carries and _wrote_pseudonym(request.deleted_data_summary):
                result.skipped.append((request.id, SKIPPED_NO_ROWS))
                continue
            change = await _reprocess_one(
                db,
                request.id,
                pseudonym,
                request.completed_at,
                existed_at,
                dry_run,
                erased,
                account_id=request.user_id,
                audit_account_id=audit_account_id(request.id, pseudonym),
                email_hashes=email_hashes[pseudonym],
                mentioned=mentions_of(pseudonym),
                swept_emails=swept_emails,
            )
        else:
            # Both checks before the user_id is used as a subject: only a row
            # an erasure run finalized stores the pseudonym (``finalized_erasure``).
            if not request.finalized:
                # #2286: a note row the sweep pseudonymized stores a real
                # pseudonym, so the orphan lookup is sound for it: audit rows
                # a run interrupted on v0.124.x (before its audit-log step)
                # left raw are found and scrubbed, and the rows naming the
                # subject (#2298) — nothing else is done. A legacy note row
                # stores the raw sub: never looked up.
                if request.note_pseudonymized:
                    change = await _reprocess_one(
                        db,
                        request.id,
                        request.user_id,
                        request.completed_at,
                        existed_at,
                        dry_run,
                        erased,
                        audit_account_id=audit_account_id(request.id, request.user_id),
                        email_hashes=email_hashes[request.user_id],
                        audit_only=True,
                        mentioned=mentions_of(request.user_id),
                        swept_emails=swept_emails,
                    )
                    record(change)
                result.skipped.append((request.id, SKIPPED_NOT_RUN))
                continue
            if not is_pseudonym(request.user_id):
                result.skipped.append((request.id, SKIPPED_NOT_PSEUDONYMIZED))
                continue
            change = await _reprocess_one(
                db,
                request.id,
                request.user_id,
                request.completed_at,
                existed_at,
                dry_run,
                erased,
                audit_account_id=audit_account_id(request.id, request.user_id),
                email_hashes=email_hashes[request.user_id],
                mentioned=mentions_of(request.user_id),
                swept_emails=swept_emails,
            )
        record(change)
    return result


def _print_plan(result: ReprocessResult) -> None:
    if result.changed:
        print(f"{'request':36}  {'completed at':20}  restored  unavailable  stripped  scrub_marked")
    for c in result.changed:
        print(
            f"{str(c.request_id):36}  {to_utc_iso(c.completed_at) or '-':20}  "
            f"{c.details_restored:8}  {c.details_restored_unavailable:11}  "
            f"{c.details_stripped:8}  {c.scrub_marked:12}"
        )
    for c in result.changed:
        if c.repaired:
            # The plan always runs as a dry run (``_oneshot.run_plan_apply``).
            print(f"would repair  {c.request_id}  {REPAIRED}")
    for c in result.changed:
        if c.audit_logs_pseudonymized:
            print(f"would pseudonymize audit rows  {c.request_id}  {_audit_rows(c)}")
    for c in result.changed:
        if c.audit_mentions_pseudonymized:
            print(
                f"would pseudonymize audit mentions  {c.request_id}  "
                f"{c.audit_mentions_pseudonymized} row(s)"
            )
    for c in result.changed:
        if c.audit_metadata_cleared:
            print(
                f"would clear audit request metadata  {c.request_id}  "
                f"{c.audit_metadata_cleared} row(s)"
            )
    for request_id in result.audit_kept:
        print(f"audit  {request_id}  {AUDIT_KEPT}")
    for request_id in result.mentions_kept:
        print(f"audit mentions  {request_id}  {MENTIONS_KEPT}")
    for request_id, reason in result.skipped:
        print(f"skipped  {request_id}  {reason}")
    verb = "would change" if result.dry_run else "changed"
    print(
        f"{result.scanned} completed erasure(s): {verb} {len(result.changed)}, "
        f"skipped {len(result.skipped)}"
    )


def _print_applied(result: ReprocessResult) -> None:
    for c in result.changed:
        if c.repaired:
            print(f"repaired  {c.request_id}  {REPAIRED}")
    for c in result.changed:
        if c.audit_logs_pseudonymized:
            print(f"pseudonymized audit rows  {c.request_id}  {_audit_rows(c)}")
    for c in result.changed:
        if c.audit_mentions_pseudonymized:
            print(
                f"pseudonymized audit mentions  {c.request_id}  "
                f"{c.audit_mentions_pseudonymized} row(s)"
            )
    for c in result.changed:
        if c.audit_metadata_cleared:
            print(
                f"cleared audit request metadata  {c.request_id}  {c.audit_metadata_cleared} row(s)"
            )
    for c in result.changed:
        if c.details_restored or c.details_restored_unavailable:
            p = c.payload
            skipped = {
                "tombstone": p.get("details_payload_skipped_tombstone", 0),
                "shared": p.get("details_payload_skipped_shared", 0),
                "edited since": p.get("details_payload_skipped_moved", 0),
            }
            # re-queued: in the restore's transaction, plus after a failed patch;
            # "already queued" are failed patches on rows the first set covers
            requeued = c.embedding_requeued + p.get("details_payload_requeued_after_error", 0)
            line = (
                f"payload  {c.request_id}  synced {p.get('details_payload_synced', 0)}, "
                f"skipped {sum(skipped.values())} "
                f"({', '.join(f'{k} {v}' for k, v in skipped.items())}), "
                f"re-queued for embedding {requeued}"
            )
            if p.get("details_payload_skipped_requeued"):
                line += f", already queued {p['details_payload_skipped_requeued']}"
            if p.get("details_payload_requeue_failed"):
                line += (
                    f", NOT re-queued {p['details_payload_requeue_failed']} "
                    "(see the erasure_details_payload_* warnings for the memory ids)"
                )
            print(line)


def _audit_rows(change: RequestChange) -> str:
    line = f"{change.audit_logs_pseudonymized} row(s)"
    if change.audit_email_recovered is False:
        line += ", email not recovered"
    return line


def _summary(result: ReprocessResult) -> str:
    restored = sum(c.details_restored for c in result.changed)
    unavailable = sum(c.details_restored_unavailable for c in result.changed)
    stripped = sum(c.details_stripped for c in result.changed)
    marked = sum(c.scrub_marked for c in result.changed)
    repaired = sum(c.repaired for c in result.changed)
    audited = sum(c.audit_logs_pseudonymized for c in result.changed)
    mentions = sum(c.audit_mentions_pseudonymized for c in result.changed)
    cleared = sum(c.audit_metadata_cleared for c in result.changed)
    return (
        (
            f"{len(result.changed)} erasure request(s): {restored} memory details restored, "
            f"{unavailable} cleared (unavailable), {stripped} revision prior(s) stripped, "
            f"{marked} marked scrubbed"
        )
        + (f", {repaired} request row(s) repaired" if repaired else "")
        + (f", {audited} audit row(s) pseudonymized" if audited else "")
        + (f", {mentions} audit mention(s) pseudonymized" if mentions else "")
        + (f", {cleared} audit row(s) request metadata cleared" if cleared else "")
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
    scope.add_argument(
        "--all",
        action="store_true",
        help="every completed erasure request, and every request that failed after its Postgres step",
    )
    scope.add_argument("--request", type=UUID, help="one such erasure request (its id)")
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
