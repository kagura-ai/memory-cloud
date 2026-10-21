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

Scope and locks (#2242): every statement reaches the log only through the
subject's own rows (``subject_memory_ids``), never by scanning every
tenant's ``prior``. The subject's memories are locked first, in id order
(``lock_subject_memories``); then each revision statement locks its rows in
ascending ``id`` through an ordered ``FOR UPDATE`` subquery
(``.claude/rules/backend.md``), waiting on a held row rather than skipping
it.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
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

from models.memory import REVISION_KIND_UPDATED, Memory, MemoryRevision
from services.memory_versions import PRIOR_ERASED_KEY, PRIOR_SCRUBBED_KEY


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
