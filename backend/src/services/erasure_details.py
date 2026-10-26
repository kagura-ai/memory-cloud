"""Revision-log erasure: strip an erased subject's ``details`` (#2043, #2242).

An ``updated`` row of ``memory_revisions`` holds in ``prior`` the values its
edit OVERWROTE, so the ``details`` a subject wrote sit in the prior of the
next edit that changed ``details`` after theirs — not in the subject's own
row, whose prior is the previous writer's.

Module functions, no service instance, so that account erasure
(``AccountErasureService._erase_memory_revisions``), restoring an erased
editor's live details on another author's memory (#2244) and re-processing
past erasures by pseudonym (#2243) share one definition:

- ``strip_subject_details(db, subject, *, existed_at, dry_run)`` — a
  function of ONE subject string: the raw sub during erasure, the stored
  salted pseudonym afterwards (``memories.user_id`` and
  ``memory_revisions.actor_user_id`` both carry it once erasure ran), with
  ``existed_at`` = the erasure's ``memory_scrub_at`` (or completion) on such
  a re-run. Idempotent: a re-run matches no row.
- ``pseudonymize_revision_actor(db, user_id, pseudonym)`` — takes the raw sub
  only; a re-run by pseudonym never calls it (it would hash the hash).
- ``restore_editor_details(db, subject, *, actor, dry_run)`` (#2244) — where
  the subject wrote the live ``details`` of a memory someone else authored,
  put back the last value someone else wrote. Like the strip, a function of
  one subject string; ``actor`` is always the pseudonym.
- ``erase_subject_details(db, subject, ...)`` (#2268) — the restore, then
  the strip, from one read of the logs under the memory locks: what the live
  erasure and the re-run call.
- ``sync_restored_payloads(db, syncs, report)`` (#2266) — the restored
  memories' vector payloads, patched after the caller's commit: no
  vector-store call happens while the subject's memories are locked.
- ``erased_pseudonyms(db)`` (#2243, #2268) — every member whose erasure ran
  in Postgres, the restore's ``erased_actors``.
- ``pseudonymize_audit_logs(db, user_id, emails)`` (#2287) — the subject's
  own ``audit_logs`` rows: called inside the erasure's Postgres transaction,
  and by the re-run for rows an erasure on v0.124.x or before left behind.
- ``erasure_pseudonym(value)`` and ``pseudonymized_request_values(account_id,
  initiated_by)`` (#2288) — the one salted pseudonym, and the values every
  writer of an erasure request row sets once its subject is erased (the
  live erasure's handler, sweep and finalize, and the re-run's repair).

Scope and locks (#2242): every statement reaches the log only through the
subject's own rows (``subject_memory_ids``), never by scanning every
tenant's ``prior``. The subject's memories are locked first, in id order
(``lock_subject_memories``); then each revision statement locks its rows in
ascending ``id`` through an ordered ``FOR UPDATE`` subquery
(``.claude/rules/backend.md``), waiting on a held row rather than skipping
it.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    ColumnElement,
    CompoundSelect,
    Select,
    any_,
    bindparam,
    case,
    exists,
    func,
    or_,
    select,
    type_coerce,
    union,
    update,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.orm.util import AliasedClass

from db.qdrant import update_memory_payload_in_qdrant
from models.auth import AuditLog, Context, User
from models.erasure import (
    FAILURE_AFTER_POSTGRES_COMMIT,
    NOTE_PSEUDONYMIZED_KEY,
    STATUS_COMPLETE,
    STATUS_FAILED,
    ErasureRequest,
)
from models.memory import (
    MEMORY_TYPE_TIME,
    REVISION_KIND_UPDATED,
    REVISION_WRITER_ERASURE,
    REVISION_WRITER_ROLLBACK,
    Memory,
    MemoryRevision,
)
from services.memory_versions import (
    PRIOR_ERASED_KEY,
    PRIOR_RESTORED_FROM_KEY,
    PRIOR_SCRUBBED_KEY,
    record_revision,
    revision_changed_field,
)
from services.write_provenance import WriteProvenance
from utils.datetime import to_utc_iso, utcnow
from utils.exceptions import QdrantError
from utils.hashing import SHA256_HEX_PATTERN, sha256_hex
from utils.logger import get_logger
from utils.time_trigger import TriggerValidationError, normalize_trigger, trigger_window

logger = get_logger(__name__)

_PSEUDONYM = re.compile(SHA256_HEX_PATTERN)


def is_pseudonym(value: str) -> bool:
    """``value`` has the shape of the salted sha256 pseudonym (64 hex)."""
    return _PSEUDONYM.fullmatch(value) is not None


def erasure_pseudonym(value: str) -> str:
    """The salted sha256 pseudonym an erasure stores for ``value`` (a raw sub).

    The one derivation: ``AccountErasureService._pseudonym`` delegates here,
    as do the request row's values (``pseudonymized_request_values``) and the
    audit-log hashes of the subject's sub and email (``pseudonymize_audit_logs``,
    the ``account_erasure`` audit row; #2288); ``reprocess_erasures`` derives
    the pseudonym of a request row that failed after its Postgres step on a
    version before v0.124.0 and still stores the raw sub (#2268). The salt is
    looked up through
    ``services.account_erasure_service._audit_salt`` at call time (a lazy
    import: that module imports this one).
    """
    from services import account_erasure_service

    return sha256_hex(value, salt=account_erasure_service._audit_salt())


def pseudonymized_request_values(account_id: str, initiated_by: str | None) -> dict[str, Any]:
    """The UPDATE values that pseudonymize an erasure request row (#2288).

    One definition for every writer that pseudonymizes a request row once
    its subject is erased: ``_finalize``, ``_execute``'s failure handler
    after the Postgres commit (#2268), the sweep's interrupted / user-row-gone
    branch (#2286) and ``reprocess_erasures``'s ``_repair``.

    ``user_id`` (always the subject, ``account_id``) becomes
    ``erasure_pseudonym(account_id)`` and the request metadata
    (``ip_address``/``user_agent``) is cleared. ``initiated_by`` becomes the
    pseudonym only when the subject started the request (self-service); an
    admin's id is accountability evidence and stays.

    Args:
        account_id: The subject's raw sub, as the row stores it.
        initiated_by: The row's ``initiated_by`` when the caller has read it:
            the decision is made here, and an admin's id is left out of the
            values. ``None`` when the caller has not read the row (the
            re-run's repair): the column decides in SQL by the same rule
            (``initiated_by`` is NOT NULL, so the ``coalesce`` never sees a
            NULL of its own).

    Returns:
        The values for ``update(ErasureRequest)``.
    """
    pseudonym = erasure_pseudonym(account_id)
    values: dict[str, Any] = {"user_id": pseudonym, "ip_address": None, "user_agent": None}
    if initiated_by is None:
        values["initiated_by"] = func.coalesce(
            func.nullif(ErasureRequest.initiated_by, account_id), pseudonym
        )
    elif initiated_by == account_id:
        values["initiated_by"] = pseudonym
    return values


async def pseudonymize_audit_logs(
    db: AsyncSession,
    user_id: str,
    emails: Collection[str] = (),
    *,
    orphaned_only: bool = False,
) -> int:
    """Pseudonymize the ``audit_logs`` rows ``user_id`` acted in (#2287).

    One UPDATE over ``audit_logs.user_id = user_id`` (the raw sub; never the
    pseudonym, which would hash the hash). The rows are kept (legal
    retention); the link to the account is broken:

    - ``user_id`` → ``erasure_pseudonym(user_id)``, so the rows of one erased
      member still correlate with each other and with their request row.
    - ``user_email`` → its salted pseudonym only where it equals one of
      ``emails`` (the subject's). In this codebase ``user_email`` is often
      the *actor's* email on a row about someone else
      (``RoleManager.assign_role``, ``SystemAdminService.promote``/``demote``);
      a blind overwrite would misattribute it.
    - ``resource`` (conventionally ``user:{email}``): every occurrence of
      each email (first: longer, more specific), then of the raw sub, is
      replaced by its pseudonym. The result is cut to the column's length:
      a pseudonym is longer than most subs and emails, and a value that no
      longer fits would fail the statement — and with it the erasure's
      whole Postgres step, on every retry.

    The live erasure passes the subject's one email. The re-run passes every
    email it recovered for the account (one per request of it), or none:
    then ``user_email`` and email occurrences in ``resource`` stay and only
    the sub is replaced. ``orphaned_only`` (the re-run): rows whose
    ``user_id`` belongs to a user row are never touched, even when that user
    row appeared after the run read the audit subs (a re-registration).

    Out of scope, here as before: rows where another actor (an admin) acted
    on the subject — their ``user_id`` is the admin's, and a ``resource``
    naming the subject's email stays. Rows of every other ``user_id`` are
    never touched. The rows are locked in id order first (``locked_ids``).

    Returns:
        The rowcount.
    """
    user_pseudonym = erasure_pseudonym(user_id)
    values: dict[str, Any] = {"user_id": user_pseudonym}
    resource: ColumnElement[str] = AuditLog.resource.expression
    pseudonyms = {
        email: erasure_pseudonym(email) for email in sorted(set(emails), key=lambda e: (-len(e), e))
    }
    for email, email_pseudonym in pseudonyms.items():
        resource = func.replace(resource, email, email_pseudonym)
    if pseudonyms:
        values["user_email"] = case(
            *((AuditLog.user_email == e, p) for e, p in pseudonyms.items()),
            else_=AuditLog.user_email,
        )
    values["resource"] = func.left(
        func.replace(resource, user_id, user_pseudonym), AuditLog.resource.type.length
    )

    def own_rows(row: Any) -> tuple[ColumnElement[bool], ...]:
        predicate: tuple[ColumnElement[bool], ...] = (row.user_id == user_id,)
        if orphaned_only:
            predicate += (~exists().where(User.user_id == row.user_id),)
        return predicate

    result = await db.execute(
        update(AuditLog)
        .where(AuditLog.id.in_(locked_ids(AuditLog, own_rows)), *own_rows(AuditLog))
        .values(values)
    )
    return cast(CursorResult[Any], result).rowcount or 0


def _ran_in_postgres() -> ColumnElement[bool]:
    """The request's Postgres step committed: only a run writes
    ``deleted_data_summary.postgres`` (since #360) — after that commit
    before v0.125.0, inside its transaction since (#2286)."""
    return func.coalesce(ErasureRequest.deleted_data_summary.has_key("postgres"), False)


def _never_ran() -> ColumnElement[bool]:
    """A ``complete`` request without ``postgres`` in its summary: the
    sweep's ``user_row_already_absent`` rows, its only writer (#2286; the
    table and the ``postgres`` summary both came with #360)."""
    return (ErasureRequest.status == STATUS_COMPLETE) & ~_ran_in_postgres()


def note_row_pseudonymized() -> ColumnElement[bool]:
    """The sweep pseudonymized this note row (since v0.125.0, #2286): its
    ``user_id`` is the pseudonym, not the raw sub."""
    return func.coalesce(ErasureRequest.deleted_data_summary.has_key(NOTE_PSEUDONYMIZED_KEY), False)


def _failed_after_commit_marked() -> ColumnElement[bool]:
    return func.coalesce(
        ErasureRequest.failure_reason.startswith(FAILURE_AFTER_POSTGRES_COMMIT, autoescape=True),
        False,
    )


def finalized_erasure() -> ColumnElement[bool]:
    """An ``erasure_requests`` row whose erasure ran and that stores the pseudonym.

    ``status='complete'`` alone is not enough: the sweep marks a request
    whose user row is already gone ``complete`` without running it, and
    before v0.125.0 that row kept the raw account id (which an IdP may issue
    as 64 hex). Only a run writes ``deleted_data_summary.postgres`` (since
    #360), and only ``_finalize`` stores the pseudonym in ``user_id`` (since
    v0.58.1) — or, since #2268, the failure handler of a request that failed
    after its Postgres step committed (``FAILURE_AFTER_POSTGRES_COMMIT``),
    and since #2286 the sweep for a run interrupted after that commit
    (``INTERRUPTED_AFTER_POSTGRES_COMMIT``, the same prefix).
    """
    return _ran_in_postgres() & (
        (ErasureRequest.status == STATUS_COMPLETE)
        | ((ErasureRequest.status == STATUS_FAILED) & _failed_after_commit_marked())
    )


def unrepaired_erasure() -> ColumnElement[bool]:
    """A request that failed after its Postgres step on a version before
    v0.124.0 (#2268): the subject is erased in Postgres, but the row still
    stores the raw sub (no failure handler pseudonymized it then).

    ``reprocess_erasures`` repairs it (``erasure_pseudonym``); until then
    ``erased_pseudonyms`` derives its pseudonym the same way.
    """
    return (
        (ErasureRequest.status == STATUS_FAILED)
        & _ran_in_postgres()
        & ~_failed_after_commit_marked()
    )


async def erased_pseudonyms(db: AsyncSession) -> frozenset[str]:
    """The pseudonyms of every member whose erasure ran in Postgres (#2243, #2268).

    What ``restore_editor_details`` takes as ``erased_actors``, so a restore
    walks past every edit one of them made — in a live erasure as in a
    re-run. A finalized row (``finalized_erasure``) gives its stored
    pseudonym when it has the pseudonym's shape (a row finalized before
    v0.58.1 stores the raw sub, and that member's edits predate the revision
    log); a row ``unrepaired_erasure`` matches gives the pseudonym derived
    from its raw sub, which is what the subject's rows carry.

    A row the sweep marked ``complete`` with the ``user_row_already_absent``
    note (``_never_ran``, #2286) may stand for an erasure that did run:
    interrupted after its Postgres commit on a version that wrote the
    summary only after it. Such a row written before v0.125.0 keeps the raw
    sub and gives the pseudonym derived from it, with no shape test (the
    stored value can be a raw sub with the pseudonym's shape); one written
    since stores the pseudonym (``pseudonymized`` in its summary) and gives
    it as is. For a request that really never ran either value is one no row
    carries: a no-op.
    """
    never_ran = _never_ran()
    rows = await db.execute(
        select(
            ErasureRequest.user_id,
            (unrepaired_erasure() | (never_ran & ~note_row_pseudonymized())).label("derive"),
        ).where(finalized_erasure() | unrepaired_erasure() | never_ran)
    )
    pseudonyms: set[str] = set()
    for user_id, derive in rows.all():
        if derive:
            pseudonyms.add(erasure_pseudonym(user_id))
        elif is_pseudonym(user_id):
            pseudonyms.add(user_id)
    return frozenset(pseudonyms)


@dataclass(frozen=True)
class StripCounts:
    """Rowcounts of ``strip_subject_details``.

    Attributes:
        details_stripped: Revision rows whose ``prior.details`` was replaced
            by the erased marker.
        scrub_marked: ``updated`` rows of the subject's memories that got the
            scrubbed marker.
    """

    details_stripped: int
    scrub_marked: int


def revision_edited_details(
    row: AliasedClass[MemoryRevision] | type[MemoryRevision],
) -> ColumnElement[bool]:
    """A revision that changed ``details`` (``revision_changed_field``).

    Its prior still holds the overwritten ``details``, or an earlier erasure
    stripped it (the prior lists ``details`` under ``PRIOR_ERASED_KEY``): it
    changed details all the same (#2235).
    """
    return revision_changed_field(row, "details")


def subject_memory_ids(subject: str) -> CompoundSelect[UUID]:
    """Memories ``subject`` authored or has a revision on.

    The union, not either arm alone: memories created before the revision
    log existed have no rows in it, and an author may never have edited
    their memory. Each arm is index-driven (``memories.user_id`` and
    ``idx_memory_revisions_actor``), so no other tenant's rows are scanned.
    The raw sub before erasure and the pseudonym after it address the same
    memories.
    """
    authored_by = aliased(Memory)
    edited_by = aliased(MemoryRevision)
    return union(
        select(authored_by.id).where(authored_by.user_id == subject),
        select(edited_by.memory_id).where(edited_by.actor_user_id == subject),
    )


def _authored_by(
    row: AliasedClass[MemoryRevision], subject: str, existed_at: datetime | None
) -> ColumnElement[bool]:
    """``row`` is on a memory ``subject`` authored (and, with ``existed_at``,
    was written by then).

    ``existed_at`` bounds a re-run by pseudonym (#2243 review S1) to
    the rows the erasure saw. The memory scrub NULLed the live ``details``
    of every memory the subject authored, so no row written after it can
    hold a value of theirs; without the bound, a re-run would strip and mark
    the rows surviving members wrote on those memories since. Since #2268
    the erasure stores ``memory_scrub_at`` (when it locked the subject's
    memories: every row it saw is older, every later edit waited for its
    commit). A request from before that has only ``completed_at``, stamped
    a few statements after the scrub commits, so a row written in between
    still counts as existing: it holds at most the scrubbed NULL, and erring
    that way only marks it, never keeps a value of the subject's. A request
    with neither (failed after its Postgres step before v0.124.0) is not
    bounded: no value of the subject's is kept, but the history surviving
    members wrote on those memories since is stripped and marked too.
    """
    authored = row.memory_id.in_(select(Memory.id).where(Memory.user_id == subject))
    if existed_at is None:
        return authored
    return authored & (row.at <= existed_at)


def strip_predicate(
    row: AliasedClass[MemoryRevision],
    subject: str,
    held_ids: Sequence[int],
    *,
    existed_at: datetime | None = None,
) -> tuple[ColumnElement[bool], ...]:
    """Revision rows whose ``prior.details`` is a value ``subject`` wrote, or
    any ``prior.details`` on a memory ``subject`` authored.

    ``row`` is the row under test. ``held_ids`` are the rows whose prior
    holds a value the subject wrote on a memory they did not author
    (``held_details_ids``, read under the memory locks). ``subject_memory_ids``
    is a pre-filter that drives the plan from the subject's own rows; it
    contains both arms by construction. ``prior ? 'details'`` makes a re-run
    match nothing. ``existed_at`` bounds the authored arm only
    (``_authored_by``): a row written after the erasure that holds the
    subject's value — the next edit over a value they left live before
    #2244 — is stripped whenever it was written.
    """
    return (
        row.memory_id.in_(subject_memory_ids(subject)),
        row.prior.has_key("details"),
        or_(
            row.id
            == any_(bindparam("held_ids", list(held_ids), type_=ARRAY(BigInteger), unique=True)),
            _authored_by(row, subject, existed_at),
        ),
    )


def scrub_mark_predicate(
    row: AliasedClass[MemoryRevision], subject: str, *, existed_at: datetime | None = None
) -> tuple[ColumnElement[bool], ...]:
    """Every ``updated`` row of a memory ``subject`` authored that does not
    carry the scrubbed marker yet (and, with ``existed_at``, was written by
    then — ``_authored_by``).

    The marker test leaves an erasure's result set as it was (no row of a
    memory still authored under the raw sub carries it) and makes a re-run
    by pseudonym (#2243) touch, lock and count no row twice. It is a
    containment test, not ``NOT (prior->'scrubbed' ? 'details')``: that one
    is NULL on a prior without the key and would exclude every unmarked row.
    """
    return (
        _authored_by(row, subject, existed_at),
        row.kind == REVISION_KIND_UPDATED,
        ~row.prior.contains({PRIOR_SCRUBBED_KEY: ["details"]}),
    )


def locked_ids(
    model: type[Memory] | type[Context] | type[MemoryRevision] | type[AuditLog],
    predicate_on: Callable[[Any], Sequence[ColumnElement[bool]]],
) -> Select[Any]:
    """Ids of the ``model`` rows ``predicate_on`` matches, locked in id order.

    ``predicate_on`` receives an alias of ``model``. Postgres does not pull
    up a sub-select that carries a locking clause, so the rows are locked on
    the sorted output, ascending ``id``, before the UPDATE / DELETE that
    reaches its rows through it rewrites them — two statements over
    overlapping rows queue instead of deadlocking. ``FOR UPDATE`` waits: a
    skipped row would silently keep personal data, so never ``SKIP LOCKED`` /
    ``NOWAIT`` here. Row locks last until the transaction commits, so a later
    statement re-locking a row is free. Used for ``memory_revisions``
    (``locked_revision_ids``, #2242), for ``memories`` / ``contexts``
    (#2263) and for ``audit_logs`` (#2287).
    """
    row = aliased(model)
    return select(row.id).where(*predicate_on(row)).order_by(row.id).with_for_update()


def locked_revision_ids(
    predicate_on: Callable[[AliasedClass[MemoryRevision]], Sequence[ColumnElement[bool]]],
) -> Select[int]:
    """Ids of the revision rows ``predicate_on`` matches, locked in id order
    (``locked_ids`` over ``memory_revisions``)."""
    return locked_ids(MemoryRevision, predicate_on)


async def _count(
    db: AsyncSession,
    predicate_on: Callable[[AliasedClass[MemoryRevision]], Sequence[ColumnElement[bool]]],
) -> int:
    """How many revision rows ``predicate_on`` matches (a dry run's count)."""
    row = aliased(MemoryRevision)
    return (await db.execute(select(func.count(row.id)).where(*predicate_on(row)))).scalar_one()


async def lock_subject_memories(db: AsyncSession, subject: str, *, deleted_by: bool = False) -> int:
    """Lock ``subject``'s memories (``subject_memory_ids``) in id order.

    Taken before any revision row, so the erasure holds every ``memories``
    row whose log it rewrites before it touches that log — the order edits
    (``MemoryService._lock_for_edit``, then an INSERT) and session rollback
    (memories in id order, then ``undone_by``) use. That closes three gaps a
    per-statement order leaves (#2242 review):

    - an edit cannot slip between the strip's snapshot and the memory scrub
      and copy the subject's live ``details`` into a fresh ``prior``: it
      waits for the erasure to commit and copies the scrubbed value;
    - a rollback holding a memory and waiting on its events, and an erasure
      holding those events and waiting on the memory, cannot form a cycle;
    - two erasures whose subjects edited each other's memories queue on the
      memory rows instead of deadlocking across the revision statements
      (every revision row an erasure locks belongs to a memory it holds).

    Re-locking a row this transaction already holds is free, so calling it
    again (``strip_subject_details`` does) costs one index-driven read.

    With ``deleted_by`` (the live erasure, #2263) the same ordered statement
    also locks the memories whose ``deleted_by`` is ``subject`` — other
    members' tombstones the erasure clears later. Locking them in a second
    phase would let two erasures, each holding its own memories, wait on each
    other's. ``memories.deleted_by`` has no index: that arm is a sequential
    scan, as the clear itself is.

    Args:
        db: The session; the caller owns the transaction.
        subject: The raw sub, or the stored salted pseudonym (#2243).
        deleted_by: Also lock the memories ``subject`` deleted.

    Returns:
        How many memories were locked (logged by the restore, #2266).
    """
    scope: CompoundSelect[UUID] = subject_memory_ids(subject)
    if deleted_by:
        forgotten_by = aliased(Memory)
        scope = union(
            *scope.selects,
            select(forgotten_by.id).where(forgotten_by.deleted_by == subject),
        )
    memory = aliased(Memory)
    rows = await db.execute(
        select(memory.id).where(memory.id.in_(scope)).order_by(memory.id).with_for_update()
    )
    return len(rows.all())


async def strip_subject_details(
    db: AsyncSession,
    subject: str,
    *,
    existed_at: datetime | None = None,
    dry_run: bool = False,
    rows: Sequence[DetailsEdit] | None = None,
) -> StripCounts:
    """Strip ``subject``'s ``details`` out of the revision log.

    Two UPDATEs, in this order:

    1. ``prior.details`` is removed from every revision of a memory the
       subject authored and from every revision that holds a ``details`` the
       subject wrote; the key is replaced by ``{"erased": ["details"]}``
       (``PRIOR_ERASED_KEY``) so a rebuilt earlier version reports its
       details as unavailable instead of passing another value off as it.
       The overwritten text stays, as the memory text itself does.
    2. every ``updated`` row of a memory the subject authored gets
       ``{"scrubbed": ["details"]}`` (``PRIOR_SCRUBBED_KEY``): the live
       ``details`` that a rebuild starts from is NULLed by the memory scrub,
       so no version below those events can read it either.

    Which prior holds a value the subject wrote is decided by provenance,
    the walk ``restore_targets`` uses (``held_details_ids``): a session
    rollback or an erasure row puts back an older value and writes none of
    its own, so the prior of the next edit above it is the value of whoever
    wrote that older one — not the rollback's requester's (review B3).

    It first locks the subject's memories (``lock_subject_memories``), so
    no edit adds a revision to their logs until the caller commits, and
    reads their logs under those locks. During erasure this runs BEFORE the
    subject's events are pseudonymized (1 finds their edits by
    ``actor_user_id``) and before the memory scrub (1 and 2 find the
    authored memories by ``memories.user_id``).

    Args:
        db: The session; the caller owns the transaction.
        subject: The raw sub during erasure, or the stored salted pseudonym
            when re-run over a past erasure (#2243).
        existed_at: On a re-run by pseudonym, the erasure's
            ``memory_scrub_at`` (or, before #2268, its completion): the
            authored arm of both statements then reaches only rows written
            by then (``_authored_by``), so what surviving members wrote on
            the scrubbed memories since stays as it is. None during erasure
            (every row predates it).
        dry_run: Count the rows the two UPDATEs would change; write and
            lock nothing.
        rows: The subject's ``_updated_rows``, read by the caller under the
            memory locks (``erase_subject_details``, #2268); the caller
            locked, so nothing is locked or read here. None reads them.

    Returns:
        The rowcounts of the two UPDATEs (or the counts they would have);
        both 0 on a re-run.
    """
    if rows is None:
        if not dry_run:
            await lock_subject_memories(db, subject)
        rows = await _updated_rows(db, subject)
    held = held_details_ids(rows, subject)
    if dry_run:
        return StripCounts(
            details_stripped=await _count(
                db, lambda row: strip_predicate(row, subject, held, existed_at=existed_at)
            ),
            scrub_marked=await _count(
                db, lambda row: scrub_mark_predicate(row, subject, existed_at=existed_at)
            ),
        )
    stripped = cast(
        CursorResult[Any],
        await db.execute(
            update(MemoryRevision)
            .where(
                MemoryRevision.id.in_(
                    locked_revision_ids(
                        lambda row: strip_predicate(row, subject, held, existed_at=existed_at)
                    )
                )
            )
            .values(
                prior=MemoryRevision.prior.delete_path(["details"]).op("||", return_type=JSONB)(
                    type_coerce({PRIOR_ERASED_KEY: ["details"]}, JSONB)
                )
            )
        ),
    )
    marked = cast(
        CursorResult[Any],
        await db.execute(
            update(MemoryRevision)
            .where(
                MemoryRevision.id.in_(
                    locked_revision_ids(
                        lambda row: scrub_mark_predicate(row, subject, existed_at=existed_at)
                    )
                )
            )
            .values(
                prior=MemoryRevision.prior.op("||", return_type=JSONB)(
                    type_coerce({PRIOR_SCRUBBED_KEY: ["details"]}, JSONB)
                )
            )
        ),
    )
    return StripCounts(details_stripped=stripped.rowcount or 0, scrub_marked=marked.rowcount or 0)


async def pseudonymize_revision_actor(db: AsyncSession, user_id: str, pseudonym: str) -> int:
    """Give the subject's own events ``pseudonym`` as their actor.

    ``actor_user_id`` becomes the salted pseudonym (the same one as the
    memories, so the log still lines up) and ``session_id`` NULL (the
    ``memory_access_events`` rule: an opaque session token has no
    post-erasure value).

    Args:
        db: The session; the caller owns the transaction.
        user_id: The raw sub. Never the pseudonym: a re-run over a past
            erasure (#2243) does not call this.
        pseudonym: The salted pseudonym of ``user_id``.

    Returns:
        How many revision rows were pseudonymized.
    """
    result = cast(
        CursorResult[Any],
        await db.execute(
            update(MemoryRevision)
            .where(
                MemoryRevision.id.in_(
                    locked_revision_ids(lambda row: (row.actor_user_id == user_id,))
                )
            )
            .values(actor_user_id=pseudonym, session_id=None)
        ),
    )
    return result.rowcount or 0


# ---------------------------------------------------------------------------
# #2244: the live details an erased member wrote on another author's memory
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DetailsEdit:
    """One ``updated`` revision of a memory's log (``_updated_rows``).

    Attributes:
        memory_id / version / id: The row's position in its memory's log.
        actor_user_id: Who wrote the edit (raw sub, or a pseudonym once erased).
        writer: One of ``REVISION_WRITERS``.
        details: ``prior.details`` — the value the edit overwrote (None when
            it was NULL, when an erasure stripped it, or when the edit did not
            change ``details``).
        erased: An earlier erasure stripped ``prior.details`` (it lists
            ``details`` under ``PRIOR_ERASED_KEY``): the value is unavailable.
        undone_by: The session rollback that reversed this edit, if any
            (``MemoryRevision.undone_by``).
        restored_from: On a put-back row (``erasure``, and ``rollback`` since
            #2266), ``prior.restored_from``: the lowest version of the block
            of edits it reverted.
        details_changed: The edit changed ``details``
            (``revision_edited_details``). A row that did not is still in the
            walk (#2266): it ends a rollback block like any foreign edit, but
            it wrote no ``details`` value.
    """

    memory_id: UUID
    version: int
    id: int
    actor_user_id: str
    writer: str
    details: Any
    erased: bool
    undone_by: int | None = None
    restored_from: int | None = None
    details_changed: bool = True


@dataclass(frozen=True)
class RestoreTarget:
    """What ``restore_editor_details`` puts back on one memory.

    Attributes:
        memory_id: The memory.
        details: The last ``details`` a surviving member wrote (or the
            creation value), or None when ``unavailable``.
        unavailable: That value was itself removed by an earlier erasure.
        from_version: The version of the lowest edit in the trailing run of
            erased members' details edits (the row whose prior is
            ``details``); the erasure row records it as ``restored_from``.
    """

    memory_id: UUID
    details: Any
    unavailable: bool
    from_version: int


@dataclass(frozen=True)
class PayloadSync:
    """A restored memory whose vector payload ``sync_restored_payloads`` patches.

    Attributes:
        memory_id: The memory.
        version: Its version right after the restore; a row at another
            version by the time of the patch was edited since, and that
            edit synced its own payload.
    """

    memory_id: UUID
    version: int


@dataclass
class RestoreReport:
    """What ``restore_editor_details`` changed (or would change, on a dry run),
    and what ``sync_restored_payloads`` did after the commit (#2266).

    Attributes:
        restored: Memories whose ``details`` went back to another member's
            value (NULL included, when that value was NULL).
        unavailable: Memories whose ``details`` became NULL (a time memory:
            only its live trigger) because the value to go back to was
            removed by an earlier erasure.
        embedding_requeued: Restored live memories whose embedding had not
            succeeded, re-queued in the restore's transaction so the next
            embed writes the payload from the restored row. Always 0 on a
            dry run.
        payload_synced: Points whose payload was patched after the commit.
        payload_skipped_tombstone: Restored memories that are tombstones (or
            gone, or without a context) at patch time: their point is gone
            or going, as ``forget`` leaves it.
        payload_skipped_shared: Restored memories whose point another live
            row shares, or whose patch had nothing left to write: left as it
            is (``metadata_patch_target`` returned None).
        payload_skipped_moved: Restored memories edited between the commit
            and the patch: that edit synced its own payload.
        payload_skipped_requeued: Patches that failed on a row already
            queued for embedding (not ``success``): the next embed rewrites
            the payload.
        payload_requeued_after_error: Patches that failed on an embedded row:
            the row was re-queued for embedding instead.
        payload_requeue_failed: Rows whose patch failed and whose re-queue
            failed too (or whose group could not be read): logged with their
            ids; the point may keep the erased member's ``location`` until
            the row is re-embedded.
        memory_ids: The restored memories, in id order.
        to_sync: The restored memories, for ``sync_restored_payloads``.
    """

    restored: int = 0
    unavailable: int = 0
    embedding_requeued: int = 0
    payload_synced: int = 0
    payload_skipped_tombstone: int = 0
    payload_skipped_shared: int = 0
    payload_skipped_moved: int = 0
    payload_skipped_requeued: int = 0
    payload_requeued_after_error: int = 0
    payload_requeue_failed: int = 0
    memory_ids: list[UUID] = field(default_factory=list)
    to_sync: list[PayloadSync] = field(default_factory=list)


def payload_counts(report: RestoreReport) -> dict[str, int]:
    """The summary keys of ``sync_restored_payloads``'s outcome, as the erasure summary and
    the re-run report name them (#2266)."""
    return {
        "details_payload_synced": report.payload_synced,
        "details_payload_skipped_tombstone": report.payload_skipped_tombstone,
        "details_payload_skipped_shared": report.payload_skipped_shared,
        "details_payload_skipped_moved": report.payload_skipped_moved,
        "details_payload_skipped_requeued": report.payload_skipped_requeued,
        "details_payload_requeued_after_error": report.payload_requeued_after_error,
        "details_payload_requeue_failed": report.payload_requeue_failed,
    }


def _past_put_back(edits: Sequence[DetailsEdit], i: int) -> int:
    """The index past the put-back row ``edits[i]`` and the block it reverted.

    ``edits`` are one memory's ``updated`` rows, newest first — every one,
    not only the details edits (#2266): a foreign edit of another field ends
    a rollback block. A put-back row writes no content of its own: it puts
    back the value from before a block of edits right below it.

    - A row with ``restored_from`` (every ``erasure`` row, #2244; every
      ``rollback`` row written since #2266) reverted every edit from that
      version up.
    - A ``rollback`` row without it (written before #2266) reverted the
      contiguous rows right below it that share the ``undone_by`` of the
      first of them — a session rollback reverses a memory's whole session
      block or skips it, and a block is the top of the log when it is
      reverted. Not exact: a rollback record id is reused across calls, so
      an event the same record skipped earlier can sit right below the
      block with the same mark. With no ``undone_by`` there (nothing to tie
      it to), the row alone is skipped.
    - An ``erasure`` row without it (none is written without it): the row
      alone is skipped.
    """
    edit = edits[i]
    i += 1
    if edit.restored_from is not None:
        while i < len(edits) and edits[i].version >= edit.restored_from:
            i += 1
    elif edit.writer == REVISION_WRITER_ROLLBACK:
        mark = edits[i].undone_by if i < len(edits) else None
        if mark is not None:
            while i < len(edits) and edits[i].undone_by == mark:
                i += 1
    return i


def _is_put_back(edit: DetailsEdit) -> bool:
    return edit.writer in (REVISION_WRITER_ERASURE, REVISION_WRITER_ROLLBACK)


def _authored_edits(edits: Sequence[DetailsEdit]) -> list[DetailsEdit]:
    """The details edits whose written value can be live, newest first.

    ``edits`` are one memory's ``updated`` rows, newest first. Each put-back
    row is dropped together with the block it reverted (``_past_put_back``),
    and so is every row that changed no ``details``, so the newest edit left
    is the one that wrote the live value, and the prior of each edit left is
    the value the next one left below it wrote.
    """
    authored: list[DetailsEdit] = []
    i = 0
    while i < len(edits):
        if _is_put_back(edits[i]):
            i = _past_put_back(edits, i)
        else:
            if edits[i].details_changed:
                authored.append(edits[i])
            i += 1
    return authored


def _writer_of_value_below(edits: Sequence[DetailsEdit], i: int) -> DetailsEdit | None:
    """The edit that wrote the value live just above ``edits[i]`` (and
    below), or None when it is the creation value.

    The first edit of ``_authored_edits(edits[i:])``, without building the
    list.
    """
    while i < len(edits):
        if _is_put_back(edits[i]):
            i = _past_put_back(edits, i)
        elif edits[i].details_changed:
            return edits[i]
        else:
            i += 1  # wrote no details value
    return None


def held_details_ids(rows: Iterable[DetailsEdit], subject: str) -> list[int]:
    """The details edits whose prior is a value ``subject`` wrote.

    Pure. ``rows`` are the ``updated`` rows of memories the subject edited
    and did not author (``_updated_rows``), in any order; only details edits
    can hold a value. An edit's prior is
    the live value right before it, written by the newest authored edit
    below it once put-back rows are dropped with their blocks — the walk of
    ``restore_targets`` (``_authored_edits``). A rollback the subject
    requested is therefore not their edit: the value it put back, and the
    prior of the edit after it, is whoever wrote that value (review B3). An
    erasure row is not either: the prior above it is the value of the edit
    below the block it reverted. A put-back row's own prior is the value it
    replaced, attributed the same way.

    Returns:
        The row ids, ascending. A row whose prior no longer holds
        ``details`` (``erased``: stripped already, or an erasure row) is
        never among them.
    """
    by_memory: dict[UUID, list[DetailsEdit]] = defaultdict(list)
    for row in rows:
        by_memory[row.memory_id].append(row)
    held: list[int] = []
    for memory_rows in by_memory.values():
        edits = sorted(memory_rows, key=lambda e: (e.version, e.id), reverse=True)
        for i, edit in enumerate(edits):
            if not edit.details_changed or edit.erased:
                continue  # no details prior, stripped already, or an erasure row
            writer = _writer_of_value_below(edits, i + 1)
            if writer is not None and writer.actor_user_id == subject:
                held.append(edit.id)
    return sorted(held)


def restore_targets(
    rows: Iterable[DetailsEdit], subject: str, *, erased_actors: Collection[str] = frozenset()
) -> list[RestoreTarget]:
    """Where ``subject`` wrote a memory's live ``details``: the value to put back.

    Pure. ``rows`` are the ``updated`` rows of the candidate memories, in
    any order (``_updated_rows``). Per memory, newest first by ``(version, id)``, put-back rows are
    dropped with the blocks they reverted (``_authored_edits``): what is
    left are the edits whose value was written by their actor, and the
    newest of them wrote the live value. That edit must be the subject's,
    else the live value is not theirs and the memory is left alone — a
    rollback the subject requested put back someone else's value, and an
    erasure row put back the value of the edit below the block it names.
    The walk goes down the trailing run of edits by erased members — the
    subject and every one of ``erased_actors`` (edits of other fields are
    not among them); the prior of the run's lowest edit is the last value a
    surviving member wrote — or the creation value, when every authored
    details edit is by an erased member.

    Whole-``details`` revert equals the per-key one: ``prior`` holds the
    whole previous value, and every value written above that lowest edit
    is an erased member's or one put back over theirs, so a key differs
    between the value put back and the live one only where an erased member
    wrote it.

    When that prior was stripped by an earlier erasure, the value is
    unavailable: the target is ``None`` with ``unavailable=True``. That is
    the case only when the prior below the whole run is gone — the author
    was erased too (their erasure strips every prior of the memories they
    authored), or an erasure stripped it before its writer's edit was known
    to be an erased member's.

    ``erased_actors`` (#2243, #2268): pseudonyms of other members whose
    erasure ran in Postgres (``erased_pseudonyms``). Their edits are walked
    past like the subject's, so a value one of them wrote never becomes live
    again (an erasure from before the #2235 fix left such priors behind),
    and the result is the same whichever erasure is processed first: A then
    B, or B then A, ends with the surviving member's value live and one
    ``erasure`` row — the second finds the first one's row on top, drops
    the block it names (``restored_from`` = the lowest version of the whole
    run) and sees no edit of its subject left as the newest.

    Returns:
        One target per memory to restore, in memory id order.
    """
    by_memory: dict[UUID, list[DetailsEdit]] = defaultdict(list)
    for row in rows:
        by_memory[row.memory_id].append(row)
    erased = {subject, *erased_actors}
    targets: list[RestoreTarget] = []
    for memory_id in sorted(by_memory):
        edits = _authored_edits(
            sorted(by_memory[memory_id], key=lambda e: (e.version, e.id), reverse=True)
        )
        if not edits or edits[0].actor_user_id != subject:
            continue  # the live value must be the subject's
        lowest = edits[0]
        for edit in edits[1:]:
            if edit.actor_user_id not in erased:
                break  # a surviving member wrote the value in lowest's prior
            lowest = edit
        # its prior was stripped: the value from before the run is gone
        unavailable = lowest.erased
        targets.append(
            RestoreTarget(
                memory_id=memory_id,
                details=None if unavailable else lowest.details,
                unavailable=unavailable,
                from_version=lowest.version,
            )
        )
    return targets


async def _updated_rows(db: AsyncSession, subject: str) -> list[DetailsEdit]:
    """The ``updated`` rows of the memories ``subject`` edited and did not author.

    The input of both walks: ``restore_targets`` and ``held_details_ids``.
    Every ``updated`` row, ``details_changed`` set on those that changed
    ``details`` (#2266): a rollback block ends at the first row below it
    that is not part of it, whatever field that row changed.

    Index-driven: the subject's rows by ``idx_memory_revisions_actor``, then
    each memory's log by ``idx_memory_revisions_memory``. A legacy row
    without a context has no log. Tombstones are included: ``forget`` keeps
    the log, and an un-forget would bring the subject's value back.
    """
    edited = select(MemoryRevision.memory_id).where(MemoryRevision.actor_user_id == subject)
    rows = (
        await db.execute(
            select(
                MemoryRevision.memory_id,
                MemoryRevision.version,
                MemoryRevision.id,
                MemoryRevision.actor_user_id,
                MemoryRevision.writer,
                MemoryRevision.prior["details"].label("details"),
                MemoryRevision.prior[PRIOR_ERASED_KEY].has_key("details").label("erased"),
                MemoryRevision.undone_by,
                MemoryRevision.prior[PRIOR_RESTORED_FROM_KEY].as_integer().label("restored_from"),
                revision_edited_details(MemoryRevision).label("details_changed"),
            )
            .join(Memory, Memory.id == MemoryRevision.memory_id)
            .where(
                MemoryRevision.memory_id.in_(edited),
                Memory.user_id != subject,
                Memory.context_id.is_not(None),
                MemoryRevision.kind == REVISION_KIND_UPDATED,
            )
            .order_by(MemoryRevision.memory_id, MemoryRevision.version, MemoryRevision.id)
        )
    ).all()
    return [
        DetailsEdit(
            memory_id=row.memory_id,
            version=row.version,
            id=row.id,
            actor_user_id=row.actor_user_id,
            writer=row.writer,
            details=row.details,
            erased=bool(row.erased),
            undone_by=row.undone_by,
            restored_from=row.restored_from,
            details_changed=bool(row.details_changed),
        )
        for row in rows
    ]


def _requeue_unembedded(memory: Memory, report: RestoreReport) -> None:
    """Re-queue a restored live row whose embedding has not succeeded.

    ``pending`` / ``processing`` / ``failed`` may have no point yet, and a run
    in flight may already have read the subject's ``details``: the row goes
    back to ``pending`` with a fresh retry budget (the embedding switch's
    re-queue; ``requeue_embedding`` is fenced to ``success``). A run in flight
    then loses its claim when it tries to stamp ``success``, and the sweep
    re-embeds from the restored row, whose payload replaces whatever the
    earlier run wrote. Inside the restore's transaction: no vector store.
    """
    if memory.deleted_at is not None or memory.context_id is None:
        return
    if memory.embedding_status == "success":
        return
    memory.embedding_status = "pending"
    memory.embedding_error = None
    memory.embedding_retry_count = 0
    report.embedding_requeued += 1


async def sync_restored_payloads(
    db: AsyncSession, syncs: Sequence[PayloadSync], report: RestoreReport
) -> None:
    """Bring the restored memories' points in line with the restored ``details``.

    Called AFTER the restore's transaction committed, in the same session (a
    fresh transaction), by the live erasure and by the re-run (#2266): no
    vector-store round trip happens while the subject's memories are locked.
    Works in groups of ``PAYLOAD_SYNC_GROUP`` rows, one transaction each (as
    ``session_rollback``'s payload step does): the group's rows are read
    ``FOR SHARE`` in id order and held until the group commits, so an edit
    that lands meanwhile waits and then syncs its own payload over this one,
    and no row waits on more than one group's round trips. Per row:

    - a tombstone (or a row gone, or without a context) is skipped: its point
      is gone or going, as ``forget`` leaves it;
    - a row whose version moved since the restore was edited, and that edit
      synced its own payload: skipped;
    - else the ``location`` payload is patched — whatever the row's
      ``embedding_status``: a row the restore re-queued may still have a
      point carrying the subject's location. The point, the keys and the
      chunk cascade follow ``MemoryService._sync_metadata_payload``
      (``metadata_patch_target``; a point another live row shares, or a
      patch with nothing left to write, is left as it is).

    Nothing here fails the caller: the request's Postgres work is committed
    already, and no later run would find the restore again. A row whose
    patch fails — for any reason, the vector store or a read in between —
    degrades to an embedding re-queue (``requeue_embedding``: the sweep's
    upsert rewrites the whole payload from the row); a row not ``success`` is
    queued already. A re-queue that fails too is counted
    (``payload_requeue_failed``) and logged, and the next row goes on. Each
    row runs in a SAVEPOINT, so a failed statement leaves the group usable.
    Logs carry ids only, never values.

    Semantics: between the restore's commit and the patch — or, when the
    patch fails, until the embedding sweep re-embeds the row — the point may
    still carry the ``location`` the erased member wrote.

    Args:
        db: The session; its restore transaction has committed.
        syncs: ``RestoreReport.to_sync``.
        report: Gets the ``payload_*`` counts.
    """
    if not syncs:
        return
    from services.session_rollback import PAYLOAD_SYNC_GROUP

    versions = {sync.memory_id: sync.version for sync in syncs}
    ids = sorted(versions)
    routes: dict[UUID, tuple[str, bool]] = {}
    for start in range(0, len(ids), PAYLOAD_SYNC_GROUP):
        group = ids[start : start + PAYLOAD_SYNC_GROUP]
        try:
            memories = {
                memory.id: memory
                for memory in (
                    await db.execute(
                        select(Memory)
                        .where(Memory.id.in_(group))
                        .order_by(Memory.id)
                        .with_for_update(read=True)
                        .execution_options(populate_existing=True)
                    )
                ).scalars()
            }
        except Exception as exc:
            await db.rollback()
            report.payload_requeue_failed += len(group)
            logger.warning(
                "erasure_details_payload_read_failed",
                memory_ids=[str(i) for i in group],
                error_class=type(exc).__name__,
            )
            continue
        for memory_id in group:
            memory = memories.get(memory_id)
            if memory is None or memory.deleted_at is not None or memory.context_id is None:
                report.payload_skipped_tombstone += 1
                continue
            if memory.version != versions[memory_id]:
                report.payload_skipped_moved += 1
                logger.info(
                    "erasure_details_payload_moved",
                    memory_id=str(memory_id),
                    restored_version=versions[memory_id],
                    version=memory.version,
                )
                continue
            await _sync_one_payload(db, memory, routes, report)
        try:
            await db.commit()
        except Exception as exc:
            await db.rollback()
            logger.warning(
                "erasure_details_payload_commit_failed",
                memory_ids=[str(i) for i in group],
                error_class=type(exc).__name__,
            )


async def _sync_one_payload(
    db: AsyncSession,
    memory: Memory,
    routes: dict[UUID, tuple[str, bool]],
    report: RestoreReport,
) -> None:
    """Patch one restored row's point, or re-queue it (``sync_restored_payloads``)."""
    from services.content_chunk_writer import chunk_cascade_needed, requeue_embedding
    from services.context_routing import resolve_collection_name
    from services.memory_service import (
        metadata_patch_target,
        metadata_payload_patch,
        restore_payload_request,
    )

    memory_id, embedded = memory.id, memory.embedding_status == "success"
    try:
        async with db.begin_nested():
            updates, delete_keys = metadata_payload_patch(
                restore_payload_request(memory, {"details"}), None, memory.details
            )
            # Issue #78: date-range filters read the payload's updated_at.
            updates["updated_at"] = to_utc_iso(memory.updated_at)
            target = await metadata_patch_target(
                db, memory.id, memory.point_id, updates, delete_keys
            )
            if target is None:
                report.payload_skipped_shared += 1
                return
            if memory.context_id not in routes:
                context_id = cast(UUID, memory.context_id)
                routes[context_id] = (
                    await resolve_collection_name(db, context_id),
                    await chunk_cascade_needed(db, context_id),
                )
            collection, cascade = routes[cast(UUID, memory.context_id)]
            await update_memory_payload_in_qdrant(
                memory_id=target.point_id,
                payload_updates=target.updates,
                collection_name=collection,
                delete_keys=target.delete_keys or None,
                cascade_chunks=cascade and target.own_point,
            )
    except Exception as exc:
        try:
            requeued = embedded and await requeue_embedding(
                db, memory_id, reason="erasure_details_payload"
            )
        except Exception as requeue_exc:
            report.payload_requeue_failed += 1
            logger.warning(
                "erasure_details_payload_requeue_failed",
                memory_id=str(memory_id),
                error_class=type(exc).__name__,
                requeue_error_class=type(requeue_exc).__name__,
            )
            return
        if requeued:
            report.payload_requeued_after_error += 1
        else:
            report.payload_skipped_requeued += 1
        # a row queued already may have no point yet: not a fault
        (logger.warning if requeued or not isinstance(exc, QdrantError) else logger.info)(
            "erasure_details_payload_failed",
            memory_id=str(memory_id),
            error_class=type(exc).__name__,
            requeued=requeued,
        )
        return
    report.payload_synced += 1


def _with_live_trigger(memory: Memory, value: Any) -> tuple[Any, bool]:
    """``value`` as a time memory can hold it (#2266).

    A ``type='time'`` row must keep its trigger window
    (``valid_trigger_window_format``). A value's trigger goes back normalized
    (``normalize_trigger``; one already normalized is unchanged): a trigger
    written while the row was not a time memory was never normalized, and a
    ``from`` / ``until`` of any other shape would fail the CHECK. Where the
    value has no usable
    trigger — it is unavailable (None), or the author wrote it before the row
    became a time memory — the live trigger's window is merged in: only its
    window keys (``trigger_window``), since the erased member may have
    written it and its ``text`` is theirs. The live one is normalized: every
    write path runs ``_apply_time_trigger``.

    Returns:
        The value to write, and whether the live trigger was merged in.
    """
    if memory.type != MEMORY_TYPE_TIME:
        return value, False
    trigger = value.get("trigger") if isinstance(value, dict) else None
    if trigger is not None:
        # normalized or not: a normalized trigger normalizes to itself, and
        # one written on a non-time row (date-only from/until, missing parts)
        # gets the window the CHECK needs, or falls through to the live one
        try:
            return {**value, "trigger": normalize_trigger(trigger)}, False
        except TriggerValidationError:
            pass
    live = memory.details.get("trigger") if isinstance(memory.details, dict) else None
    return {**(value if isinstance(value, dict) else {}), "trigger": trigger_window(live)}, True


async def restore_editor_details(
    db: AsyncSession,
    subject: str,
    *,
    actor: str,
    dry_run: bool = False,
    erased_actors: Collection[str] = frozenset(),
    rows: Sequence[DetailsEdit] | None = None,
) -> RestoreReport:
    """Put back the ``details`` ``subject`` wrote on memories others authored.

    Where the subject made the newest ``details`` edit of a memory another
    member authored, the live ``details`` goes back to the last value someone
    else wrote (``restore_targets``: the subject's trailing run of edits is
    walked past; a value an earlier erasure removed gives NULL). Memories the
    subject authored are not touched here: ``_scrub_surviving_memories``
    NULLs their ``details``.

    Each restore is an edit of its own: ``version`` + 1, ``updated_at`` now,
    and one ``updated`` revision row with ``writer='erasure'``, ``actor`` as
    its actor and ``{"erased": ["details"], "restored_from": <from_version>}``
    as its prior — "details changed; the overwritten value is unavailable;
    the edits from that version up were reverted" — so the subject's value
    is in no prior, a later erasure can tell whose value is live,
    ``expected_version`` protects the restored value from a stale edit, and a rebuilt version at or above the subject's edits reads
    ``details`` as unavailable. The memory's last writer (``agent_id`` /
    ``session_id``) becomes none. A restore that would write the live value
    again is skipped.

    Order and locks: the subject's memories are locked first, in id order
    (``lock_subject_memories``, the order every edit and session rollback
    takes), so the log read after it is exact and no edit lands in between.
    During erasure this runs BEFORE ``strip_subject_details`` and
    ``pseudonymize_revision_actor``: it finds the subject's edits by
    ``actor_user_id == subject``, and the new row's actor is the pseudonym,
    so neither statement touches it. The value it reads (the prior of the
    lowest row of the subject's run) is one the strip leaves alone. The
    locks are held until the caller commits; a subject with many edits holds
    many rows for that long, which an erasure accepts. No vector-store call
    happens here (#2266): a restored live row whose embedding has not
    succeeded is re-queued in this transaction, and every restored row is
    listed in ``report.to_sync`` for ``sync_restored_payloads``, which the
    caller runs after its commit.

    The resource indexer rewrites ``details`` without a revision; on such a
    row the restore may replace the indexer's value until its next run
    writes it again. No special case.

    Args:
        db: The session; the caller owns the transaction.
        subject: The id as ``memory_revisions.actor_user_id`` and
            ``memories.user_id`` hold it now: the raw sub during erasure, the
            stored salted pseudonym when re-run over a past erasure (#2243).
        actor: The pseudonym written as the new rows' actor — never the raw
            sub, which must not outlive the erasure.
        dry_run: Compute what would change and write nothing (no row, no
            row lock: the log is read as of the statement, which a preview
            can afford); ``to_sync`` stays empty.
        erased_actors: Pseudonyms of other erased members (``restore_targets``,
            ``erased_pseudonyms``).
        rows: The subject's ``_updated_rows``, read by the caller under the
            memory locks (``erase_subject_details``, #2268); the caller
            locked, so nothing is locked or read here. None reads them.

    Returns:
        The counts, the restored memories and the payloads to sync.
    """
    if rows is None:
        locked = 0 if dry_run else await lock_subject_memories(db, subject)
        rows = await _updated_rows(db, subject)
        logger.info("erasure_restore_locked", locked=locked)
    report = RestoreReport()
    targets = restore_targets(rows, subject, erased_actors=erased_actors)
    logger.info("erasure_restore_candidates", targets=len(targets))
    if not targets:
        return report
    memories = {
        memory.id: memory
        for memory in (
            await db.execute(
                select(Memory)
                .where(Memory.id.in_([t.memory_id for t in targets]))
                .order_by(Memory.id)
                .execution_options(populate_existing=True)
            )
        ).scalars()
    }
    for target in targets:
        memory = memories[target.memory_id]
        value, trigger_kept = _with_live_trigger(memory, target.details)
        if memory.details == value:
            continue  # the live value is already the one to put back
        if target.unavailable:
            report.unavailable += 1
        else:
            report.restored += 1
        report.memory_ids.append(memory.id)
        if dry_run:
            continue
        memory.details = value
        memory.updated_at = utcnow()
        memory.version = memory.version + 1
        record_revision(
            db,
            memory,
            kind=REVISION_KIND_UPDATED,
            actor_user_id=actor,
            at=memory.updated_at,
            prior={PRIOR_ERASED_KEY: ["details"], PRIOR_RESTORED_FROM_KEY: target.from_version},
            writer=REVISION_WRITER_ERASURE,
            provenance=WriteProvenance(None, None),
        )
        _requeue_unembedded(memory, report)
        report.to_sync.append(PayloadSync(memory.id, memory.version))
        logger.info(
            "erasure_details_restored",
            memory_id=str(memory.id),
            from_version=target.from_version,
            unavailable=target.unavailable,
            trigger_kept=trigger_kept,
        )
    if report.to_sync:
        await db.flush()
    return report


@dataclass(frozen=True)
class SubjectDetailsErasure:
    """What ``erase_subject_details`` did (or would do, on a dry run)."""

    restore: RestoreReport
    strip: StripCounts


async def erase_subject_details(
    db: AsyncSession,
    subject: str,
    *,
    actor: str,
    erased_actors: Collection[str],
    existed_at: datetime | None = None,
    dry_run: bool = False,
) -> SubjectDetailsErasure:
    """Restore, then strip, ``subject``'s ``details`` — one log read (#2268).

    The two steps of the live erasure (``_delete_postgres``) and of the
    re-run (``reprocess_erasures``), in that order: ``restore_editor_details``
    then ``strip_subject_details``. The subject's memories are locked first
    (``lock_subject_memories``; free when the caller holds them already),
    and their logs are read once under those locks: the ``erasure`` rows the
    restore adds sit on top of each log, with nothing above them, and are
    put-back rows whose prior holds no value — they change no
    ``held_details_ids`` result, so the strip works from the same read.

    Args:
        db: The session; the caller owns the transaction.
        subject: The raw sub during erasure, or the stored pseudonym on a
            re-run.
        actor: The pseudonym written as the restore rows' actor.
        erased_actors: Other erased members' pseudonyms
            (``erased_pseudonyms``, without the subject's).
        existed_at: The re-run's bound for the authored arm of the strip
            (``_authored_by``); None during erasure.
        dry_run: Count; write and lock nothing.

    Returns:
        The restore report (its ``to_sync`` for ``sync_restored_payloads``
        after the caller's commit) and the strip counts.
    """
    locked = 0 if dry_run else await lock_subject_memories(db, subject)
    rows = await _updated_rows(db, subject)
    logger.info("erasure_details_rows", locked=locked, rows=len(rows))
    restore = await restore_editor_details(
        db, subject, actor=actor, dry_run=dry_run, erased_actors=erased_actors, rows=rows
    )
    strip = await strip_subject_details(
        db, subject, existed_at=existed_at, dry_run=dry_run, rows=rows
    )
    return SubjectDetailsErasure(restore=restore, strip=strip)
