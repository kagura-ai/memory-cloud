"""Revision-log erasure: strip an erased subject's ``details`` (#2043, #2242).

An ``updated`` row of ``memory_revisions`` holds in ``prior`` the values its
edit OVERWROTE, so the ``details`` a subject wrote sit in the prior of the
next edit that changed ``details`` after theirs — not in the subject's own
row, whose prior is the previous writer's.

Module functions, no service instance, so that account erasure
(``AccountErasureService._erase_memory_revisions``), restoring an erased
editor's live details on another author's memory (#2244) and re-processing
past erasures by pseudonym (#2243) share one definition:

- ``strip_subject_details(db, subject)`` — a function of ONE subject string:
  the raw sub during erasure, the stored salted pseudonym afterwards
  (``memories.user_id`` and ``memory_revisions.actor_user_id`` both carry
  it once erasure ran). Idempotent: a re-run matches no row.
- ``pseudonymize_revision_actor(db, user_id, pseudonym)`` — takes the raw sub
  only; a re-run by pseudonym never calls it (it would hash the hash).
- ``restore_editor_details(db, subject, *, actor, dry_run)`` (#2244) — where
  the subject wrote the live ``details`` of a memory someone else authored,
  put back the last value someone else wrote. Like the strip, a function of
  one subject string; ``actor`` is always the pseudonym.

Scope and locks (#2242): every statement reaches the log only through the
subject's own rows (``subject_memory_ids``), never by scanning every
tenant's ``prior``. The subject's memories are locked first, in id order
(``lock_subject_memories``); then each revision statement locks its rows in
ascending ``id`` through an ordered ``FOR UPDATE`` subquery
(``.claude/rules/backend.md``), waiting on a held row rather than skipping
it.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, cast
from uuid import UUID

from sqlalchemy import (
    ColumnElement,
    CompoundSelect,
    Select,
    or_,
    select,
    type_coerce,
    union,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.orm.util import AliasedClass

from db.qdrant import update_memory_payload_in_qdrant
from models.memory import (
    REVISION_KIND_UPDATED,
    REVISION_WRITER_ERASURE,
    Memory,
    MemoryRevision,
)
from services.memory_versions import PRIOR_ERASED_KEY, PRIOR_SCRUBBED_KEY, record_revision
from services.write_provenance import WriteProvenance
from utils.datetime import to_utc_iso, utcnow
from utils.logger import get_logger

logger = get_logger(__name__)


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
    """A revision that changed ``details``.

    Its prior still holds the overwritten ``details``, or an earlier erasure
    stripped it (the prior lists ``details`` under ``PRIOR_ERASED_KEY``): it
    changed details all the same (#2235).
    """
    return or_(row.prior.has_key("details"), row.prior[PRIOR_ERASED_KEY].has_key("details"))


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


def _authored_by(row: AliasedClass[MemoryRevision], subject: str) -> ColumnElement[bool]:
    return row.memory_id.in_(select(Memory.id).where(Memory.user_id == subject))


def strip_predicate(
    row: AliasedClass[MemoryRevision], subject: str
) -> tuple[ColumnElement[bool], ...]:
    """Revision rows whose ``prior.details`` is a value ``subject`` wrote, or
    any ``prior.details`` on a memory ``subject`` authored.

    ``row`` is the row under test. ``subject_memory_ids`` is a pre-filter
    that drives the plan from the subject's own rows; the result set is the
    ``authored | holds_subjects_details`` one, which it contains by
    construction (``holds_subjects_details`` needs a revision by ``subject``
    on the same memory). The pre-filter never replaces it: on a memory the
    subject only edited, other members' values stay. ``prior ? 'details'``
    makes a re-run match nothing.
    """
    theirs = aliased(MemoryRevision)
    between = aliased(MemoryRevision)
    # The subject's details edit, and no other details edit between it and
    # this row: this row's prior is the value the subject wrote. Both
    # subqueries correlate explicitly (#2235): auto-correlation only reaches
    # the immediately enclosing SELECT, so the inner one would otherwise get
    # its own uncorrelated FROM entry and look at every memory's edits.
    holds_subjects_details = (
        select(theirs.id)
        .where(
            theirs.memory_id == row.memory_id,
            theirs.actor_user_id == subject,
            # an ``erasure`` row (#2244) carries the pseudonym as its actor but
            # put back another member's value: not the subject's details
            theirs.writer != REVISION_WRITER_ERASURE,
            revision_edited_details(theirs),
            theirs.version < row.version,
            ~select(between.id)
            .where(
                between.memory_id == row.memory_id,
                revision_edited_details(between),
                between.version > theirs.version,
                between.version < row.version,
            )
            .correlate(row, theirs)
            .exists(),
        )
        .correlate(row)
        .exists()
    )
    return (
        row.memory_id.in_(subject_memory_ids(subject)),
        row.prior.has_key("details"),
        _authored_by(row, subject) | holds_subjects_details,
    )


def scrub_mark_predicate(
    row: AliasedClass[MemoryRevision], subject: str
) -> tuple[ColumnElement[bool], ...]:
    """Every ``updated`` row of a memory ``subject`` authored that does not
    carry the scrubbed marker yet.

    The marker test leaves an erasure's result set as it was (no row of a
    memory still authored under the raw sub carries it) and makes a re-run
    by pseudonym (#2243) touch, lock and count no row twice. It is a
    containment test, not ``NOT (prior->'scrubbed' ? 'details')``: that one
    is NULL on a prior without the key and would exclude every unmarked row.
    """
    return (
        _authored_by(row, subject),
        row.kind == REVISION_KIND_UPDATED,
        ~row.prior.contains({PRIOR_SCRUBBED_KEY: ["details"]}),
    )


def locked_revision_ids(
    predicate_on: Callable[[AliasedClass[MemoryRevision]], Sequence[ColumnElement[bool]]],
) -> Select[int]:
    """Ids of the revision rows ``predicate_on`` matches, locked in id order.

    Postgres does not pull up a sub-select that carries a locking clause, so
    the rows are locked on the sorted output, ascending ``id``, before the
    UPDATE rewrites them — two statements over overlapping rows queue instead
    of deadlocking. ``FOR UPDATE`` waits: a skipped row would silently keep
    personal data, so never ``SKIP LOCKED`` / ``NOWAIT`` here. Row locks last
    until the transaction commits, so a later statement re-locking a row is
    free.
    """
    row = aliased(MemoryRevision)
    return select(row.id).where(*predicate_on(row)).order_by(row.id).with_for_update()


async def lock_subject_memories(db: AsyncSession, subject: str) -> None:
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

    Args:
        db: The session; the caller owns the transaction.
        subject: The raw sub, or the stored salted pseudonym (#2243).
    """
    memory = aliased(Memory)
    await db.execute(
        select(memory.id)
        .where(memory.id.in_(subject_memory_ids(subject)))
        .order_by(memory.id)
        .with_for_update()
    )


async def strip_subject_details(db: AsyncSession, subject: str) -> StripCounts:
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

    It first locks the subject's memories (``lock_subject_memories``), so
    no edit adds a revision to their logs until the caller commits. During
    erasure this runs BEFORE the subject's events are pseudonymized (1 finds
    their edits by ``actor_user_id``) and before the memory scrub (1 and 2
    find the authored memories by ``memories.user_id``). Each statement's
    subqueries read the rows as they were when it started.

    Args:
        db: The session; the caller owns the transaction.
        subject: The raw sub during erasure, or the stored salted pseudonym
            when re-run over a past erasure (#2243).

    Returns:
        The rowcounts of the two UPDATEs; both 0 on a re-run.
    """
    await lock_subject_memories(db, subject)
    stripped = cast(
        CursorResult[Any],
        await db.execute(
            update(MemoryRevision)
            .where(
                MemoryRevision.id.in_(
                    locked_revision_ids(lambda row: strip_predicate(row, subject))
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
                    locked_revision_ids(lambda row: scrub_mark_predicate(row, subject))
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
    """One ``updated`` revision that changed ``details`` (``revision_edited_details``).

    Attributes:
        memory_id / version / id: The row's position in its memory's log.
        actor_user_id: Who wrote the edit (raw sub, or a pseudonym once erased).
        writer: One of ``REVISION_WRITERS``.
        details: ``prior.details`` — the value the edit overwrote (None when
            it was NULL, or when an erasure stripped it).
        erased: An earlier erasure stripped ``prior.details`` (it lists
            ``details`` under ``PRIOR_ERASED_KEY``): the value is unavailable.
    """

    memory_id: UUID
    version: int
    id: int
    actor_user_id: str
    writer: str
    details: Any
    erased: bool


@dataclass(frozen=True)
class RestoreTarget:
    """What ``restore_editor_details`` puts back on one memory.

    Attributes:
        memory_id: The memory.
        details: The last ``details`` someone other than the subject wrote, or
            None when ``unavailable``.
        unavailable: That value was itself removed by an earlier erasure.
        from_version: The version of the subject's lowest edit in their
            trailing run of details edits (the row whose prior is ``details``).
    """

    memory_id: UUID
    details: Any
    unavailable: bool
    from_version: int


@dataclass
class RestoreReport:
    """What ``restore_editor_details`` changed (or would change, on a dry run).

    Attributes:
        restored: Memories whose ``details`` went back to another member's
            value (NULL included, when that value was NULL).
        unavailable: Memories whose ``details`` became NULL because the value
            to go back to was removed by an earlier erasure.
        payload_synced: Vector points whose payload was patched.
        payload_skipped: Restored memories whose point was left as it is: a
            tombstone, or a point another live row shares. Always 0 on a dry
            run.
        embedding_requeued: Restored memories whose embedding had not
            succeeded, re-queued so the next embed writes the payload from the
            restored row. Always 0 on a dry run.
        memory_ids: The restored memories, in id order.
    """

    restored: int = 0
    unavailable: int = 0
    payload_synced: int = 0
    payload_skipped: int = 0
    embedding_requeued: int = 0
    memory_ids: list[UUID] = field(default_factory=list)


def _written_by(edit: DetailsEdit, subject: str) -> bool:
    """An edit the subject wrote. An ``erasure`` row never is: its actor is
    the pseudonym, the same string a re-run by pseudonym (#2243) passes as the
    subject, and it put back someone else's value."""
    return edit.actor_user_id == subject and edit.writer != REVISION_WRITER_ERASURE


def restore_targets(rows: Iterable[DetailsEdit], subject: str) -> list[RestoreTarget]:
    """Where ``subject`` wrote a memory's live ``details``: the value to put back.

    Pure. ``rows`` are the details edits of the candidate memories, in any
    order. Per memory, newest first by ``(version, id)``: the newest details
    edit must be the subject's (``_written_by``), else the live value is not
    theirs and the memory is left alone. The walk goes down the subject's
    trailing run of details edits (non-details edits in between are not in
    ``rows``); the prior of the run's lowest row is the last value someone
    else wrote — or the creation value, when every details edit is the
    subject's. A rollback the subject requested counts as their write, the
    rule ``strip_subject_details`` follows.

    Whole-``details`` revert equals the per-key one: ``prior`` holds the
    whole previous value, and every edit above that lowest row is the
    subject's, so a key differs between the value put back and the live one
    only where the subject wrote it. Keys the subject never touched are
    identical in both.

    When that prior was stripped by an earlier erasure (its writer was
    erased first), the value is unavailable: the target is ``None`` with
    ``unavailable=True``. The walk does not go further down: the value below
    was overwritten by the erased member's edit and was not what the memory
    held before the subject's edits.

    Returns:
        One target per memory to restore, in memory id order.
    """
    by_memory: dict[UUID, list[DetailsEdit]] = defaultdict(list)
    for row in rows:
        by_memory[row.memory_id].append(row)
    targets: list[RestoreTarget] = []
    for memory_id in sorted(by_memory):
        edits = sorted(by_memory[memory_id], key=lambda e: (e.version, e.id), reverse=True)
        if not _written_by(edits[0], subject):
            continue
        lowest = edits[0]
        for edit in edits[1:]:
            if not _written_by(edit, subject):
                break
            lowest = edit
        targets.append(
            RestoreTarget(
                memory_id=memory_id,
                details=None if lowest.erased else lowest.details,
                unavailable=lowest.erased,
                from_version=lowest.version,
            )
        )
    return targets


async def _details_edits(db: AsyncSession, subject: str) -> list[DetailsEdit]:
    """The details edits of the memories ``subject`` edited and did not author.

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
            )
            .join(Memory, Memory.id == MemoryRevision.memory_id)
            .where(
                MemoryRevision.memory_id.in_(edited),
                Memory.user_id != subject,
                Memory.context_id.is_not(None),
                MemoryRevision.kind == REVISION_KIND_UPDATED,
                revision_edited_details(MemoryRevision),
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
        )
        for row in rows
    ]


async def _sync_details_payload(
    db: AsyncSession, memories: Sequence[Memory], report: RestoreReport
) -> None:
    """Bring the restored memories' points in line with the restored ``details``.

    A row whose embedding succeeded gets its ``location`` payload patched
    inside the caller's transaction, before its commit, and a vector-store
    error propagates (``update_memory``'s convention): a completed erasure
    must not leave the subject's coordinates on another author's point, and
    an erasure that fails here rolls Postgres back and is re-run whole (the
    patch converges). The point, the keys and the chunk cascade follow
    ``MemoryService._sync_metadata_payload`` (``metadata_patch_target``).

    A row whose embedding has not succeeded (``pending`` / ``processing`` /
    ``failed``) may have no point yet, and a run in flight may already have
    read the subject's ``details``: it is re-queued instead (``pending``, a
    fresh retry budget — the embedding switch's re-queue). A run in flight
    then loses its claim when it tries to stamp ``success``, and the sweep
    re-embeds from the restored row, whose payload replaces whatever the
    earlier run wrote. A tombstone is skipped (its point is gone or going,
    as ``forget`` leaves it), as is a point another live row shares.
    """
    from services.content_chunk_writer import chunk_cascade_needed
    from services.context_routing import resolve_collection_name
    from services.memory_service import (
        metadata_patch_target,
        metadata_payload_patch,
        restore_payload_request,
    )

    routes: dict[UUID, tuple[str, bool]] = {}
    for memory in memories:
        if memory.deleted_at is not None or memory.context_id is None:
            report.payload_skipped += 1
            continue
        if memory.embedding_status != "success":
            memory.embedding_status = "pending"
            memory.embedding_error = None
            memory.embedding_retry_count = 0
            report.embedding_requeued += 1
            continue
        updates, delete_keys = metadata_payload_patch(
            restore_payload_request(memory, {"details"}), None, memory.details
        )
        # Issue #78: date-range filters read the payload's updated_at.
        updates["updated_at"] = to_utc_iso(memory.updated_at)
        target = await metadata_patch_target(db, memory.id, memory.point_id, updates, delete_keys)
        if target is None:
            report.payload_skipped += 1
            continue
        if memory.context_id not in routes:
            routes[memory.context_id] = (
                await resolve_collection_name(db, memory.context_id),
                await chunk_cascade_needed(db, memory.context_id),
            )
        collection, cascade = routes[memory.context_id]
        await update_memory_payload_in_qdrant(
            memory_id=target.point_id,
            payload_updates=target.updates,
            collection_name=collection,
            delete_keys=target.delete_keys or None,
            cascade_chunks=cascade and target.own_point,
        )
        report.payload_synced += 1
    await db.flush()


async def restore_editor_details(
    db: AsyncSession, subject: str, *, actor: str, dry_run: bool = False
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
    its actor and ``{"erased": ["details"]}`` as its prior — "details
    changed; the overwritten value is unavailable" — so the subject's value
    is in no prior, ``expected_version`` protects the restored value from a
    stale edit, and a rebuilt version at or above the subject's edits reads
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
    many rows for that long, which an erasure accepts.

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
            vector-store call).

    Returns:
        The counts and the restored memories.

    Raises:
        QdrantError: A payload patch of a live, embedded row failed; the
            caller rolls back.
    """
    await lock_subject_memories(db, subject)
    report = RestoreReport()
    targets = restore_targets(await _details_edits(db, subject), subject)
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
    restored: list[Memory] = []
    for target in targets:
        memory = memories[target.memory_id]
        if memory.details == target.details:
            continue  # the live value is already the one to put back
        if target.unavailable:
            report.unavailable += 1
        else:
            report.restored += 1
        report.memory_ids.append(memory.id)
        if dry_run:
            continue
        memory.details = target.details
        memory.updated_at = utcnow()
        memory.version = memory.version + 1
        record_revision(
            db,
            memory,
            kind=REVISION_KIND_UPDATED,
            actor_user_id=actor,
            at=memory.updated_at,
            prior={PRIOR_ERASED_KEY: ["details"]},
            writer=REVISION_WRITER_ERASURE,
            provenance=WriteProvenance(None, None),
        )
        restored.append(memory)
        logger.info(
            "erasure_details_restored",
            memory_id=str(memory.id),
            from_version=target.from_version,
            unavailable=target.unavailable,
        )
    if not restored:
        return report
    await db.flush()
    await _sync_details_payload(db, restored, report)
    return report
