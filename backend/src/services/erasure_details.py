"""Revision-log erasure: strip an erased subject's ``details`` (#2043, #2242).

An ``updated`` row of ``memory_revisions`` holds in ``prior`` the values its
edit OVERWROTE, so the ``details`` a subject wrote sit in the prior of the
next edit that changed ``details`` after theirs — not in the subject's own
row, whose prior is the previous writer's — and, since every write replaces
the whole dict, in the priors of every later edit that carried their keys
unchanged. On memories others authored the restore and the strip work per
top-level key (#2275): ``details_provenance`` replays each memory's log and
attributes every key's value to whoever introduced it. That replay replaces
the whole-value walk of #2244 / #2266 (and its ``undone_by`` block heuristic
for session rollbacks from before #2266: a value match instead, exact unless
the rolled-back session itself re-wrote a key to an earlier value).

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
  the subject wrote live ``details`` keys of a memory someone else authored,
  put each back to the value from before their write (per key since #2275:
  a later member's whole-dict edit that carried the key unchanged does not
  make it theirs). Like the strip, a function of one subject string;
  ``actor`` is always the pseudonym. Each restore writes a ``writer='erasure'``
  row whose prior is ``{"details": <the overwritten value minus the
  subject's keys>, "erased_details_keys": <their digests>, "restored_from":
  <version>, "details_restored_from": <key digest to version>}``
  (#2393; the per-key map takes precedence over the scalar; before v0.129.0:
  ``{"erased": ["details"], "restored_from": <version>}``).
- ``erase_subject_details(db, subject, ...)`` (#2268) — the restore, then
  the strip, from one read of the logs under the memory locks and one
  ``details_provenance.plan_details_erasure`` (#2275): what the live
  erasure and the re-run call.
- ``sync_restored_payloads(db, syncs, report)`` (#2266) — the restored
  memories' vector payloads, patched after the caller's commit: no
  vector-store call happens while the subject's memories are locked.
  ``restored_payload_syncs`` and ``record_resynced_payloads`` (#2302) — the
  same for a run interrupted after its Postgres commit: the restored set read
  back from the log, and the outcome recorded on its request row (the
  sweep's re-drive and the re-run's backstop).
- ``erased_pseudonyms(db)`` (#2243, #2268) — every member whose erasure ran
  in Postgres, the restore's ``erased_actors``.
- ``pseudonymize_audit_logs(db, user_id, emails)`` (#2287) — the subject's
  own ``audit_logs`` rows: called inside the erasure's Postgres transaction,
  and by the re-run for rows an erasure on v0.124.x or before left behind.
- ``pseudonymize_audit_mentions(db, emails, subs=...)`` (#2298) — other
  actors' rows that name the subject by an exact ``user_email`` or
  ``resource = 'user:'||email`` value; ``pseudonymize_audit_trail`` runs
  both under one id-ordered lock (the live erasure and the re-run).
- ``clear_audit_request_metadata`` / ``pseudonymize_erasure_audit_initiator``
  (#2345) — the re-run's sweep of the rows that already carry the
  pseudonym: no IP / user agent, and the ``account_erasure`` row's
  ``initiated_by`` the pseudonym where it was the subject's sub.
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

import copy
import re
from collections import defaultdict
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    ColumnElement,
    CompoundSelect,
    Select,
    Text,
    any_,
    bindparam,
    case,
    exists,
    func,
    literal,
    null,
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

from config.constants import ERASED_AUTHOR_EMBEDDING_ERROR
from db.qdrant import update_memory_payload_in_qdrant
from models.auth import APIKey, AuditLog, Context, User
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
    Memory,
    MemoryRevision,
)
from services.details_provenance import (
    DetailsEdit,
    DetailsErasurePlan,
    PriorStrip,
    details_key_digest,
    plan_details_erasure,
)
from services.memory_versions import (
    PRIOR_DETAILS_RESTORED_FROM_KEY,
    PRIOR_ERASED_DETAILS_KEYS_KEY,
    PRIOR_ERASED_KEY,
    PRIOR_RESTORED_FROM_KEY,
    PRIOR_SCRUBBED_KEY,
    record_revision,
    revision_changed_field,
)
from services.payload_sync import (
    PlannedPatch,
    RequeueEmbeddingOnFailure,
    sync_payloads_after_commit,
)
from services.write_provenance import WriteProvenance
from utils.datetime import to_naive_utc, to_utc_iso, utcnow
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
    - ``ip_address`` and ``user_agent`` are cleared (#2345): request metadata
      of the subject, like the request row's own, which is cleared too.

    The live erasure passes the subject's one email. The re-run passes every
    email it recovered for the account (one per request of it), or none:
    then ``user_email`` and email occurrences in ``resource`` stay and only
    the sub is replaced. ``orphaned_only`` (the re-run): rows whose
    ``user_id`` belongs to a user row are never touched by this statement,
    even when that user row appeared after the run read the audit subs (a
    re-registration); the mention sweep (``pseudonymize_audit_mentions``)
    still rewrites an erased address that no user row holds on them.

    Rows of every other ``user_id`` are never touched here: where another
    actor's row names the subject (an admin acting on them, or they acting
    on someone else) ``pseudonymize_audit_mentions`` rewrites the exact
    values (#2298); ``pseudonymize_audit_trail`` runs both under one lock.
    The rows are locked in id order first (``locked_ids``).

    Returns:
        The rowcount.
    """
    user_pseudonym = erasure_pseudonym(user_id)
    values: dict[str, Any] = {"user_id": user_pseudonym, "ip_address": None, "user_agent": None}
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
        return (_own_audit_rows(row, user_id, orphaned_only=orphaned_only),)

    result = await db.execute(
        update(AuditLog)
        .where(AuditLog.id.in_(locked_ids(AuditLog, own_rows)), *own_rows(AuditLog))
        .values(values)
    )
    return cast(CursorResult[Any], result).rowcount or 0


def _own_audit_rows(row: Any, user_id: str, *, orphaned_only: bool) -> ColumnElement[bool]:
    """The rows ``user_id`` acted in (``pseudonymize_audit_logs``'s scope)."""
    predicate: ColumnElement[bool] = row.user_id == user_id
    if orphaned_only:
        predicate = predicate & ~exists().where(User.user_id == row.user_id)
    return predicate


def _by_length(values: Iterable[str]) -> list[str]:
    """Distinct ``values``, the longest first (then alphabetical): a stable order."""
    return sorted(set(values), key=lambda v: (-len(v), v))


def audit_mention_resources(emails: Collection[str], subs: Collection[str] = ()) -> list[str]:
    """The ``resource`` values naming the subject (#2298): ``user:{email}``,
    the shape every writer of a row about a member produces, and
    ``user:{sub}`` (today only on the subject's own rows; one more value)."""
    return [f"user:{e}" for e in _by_length(emails)] + [f"user:{s}" for s in _by_length(subs)]


def audit_mention_predicate(
    row: Any, emails: Collection[str], subs: Collection[str] = ()
) -> ColumnElement[bool] | None:
    """Rows that name the subject by an exact value (#2298): ``user_email``
    is one of ``emails`` (the subject acting on another member's row) or
    ``resource`` is one of ``audit_mention_resources`` (another actor
    acting on the subject). None when there is nothing to match."""
    arms: list[ColumnElement[bool]] = []
    if emails:
        arms.append(row.user_email.in_(_by_length(emails)))
    resources = audit_mention_resources(emails, subs)
    if resources:
        arms.append(row.resource.in_(resources))
    return or_(*arms) if arms else None


async def pseudonymize_audit_mentions(
    db: AsyncSession, emails: Collection[str], *, subs: Collection[str] = ()
) -> int:
    """Pseudonymize the subject's identifiers on other actors' audit rows (#2298).

    The rows are kept (legal retention) and so is everything else on them —
    ``user_id``, ``action``, the hashes. Exact values only, never a
    substring of another actor's text:

    - ``user_email`` equal to one of ``emails`` (the subject acting as an
      admin on someone else's row: ``user_id`` is the target's there) →
      ``erasure_pseudonym(email)``; the subject was the actor, so the row's
      ``ip_address`` / ``user_agent`` are theirs and are cleared (#2345);
    - ``resource`` equal to ``user:{email}`` or ``user:{sub}`` (an admin
      acting on the subject) → ``user:`` + the pseudonym — 69 characters,
      within the column. The acting admin's ``user_id`` / ``user_email``
      and request metadata stay (accountability evidence).

    The pseudonyms are the ones ``pseudonymize_audit_logs`` writes on the
    subject's own rows, so the two still correlate. No ``user_id``
    predicate: the live erasure rewrote the own rows just before (none is
    counted twice), and the re-run may not know the sub. Idempotent: once
    rewritten, no row matches. The rows are locked in id order first.

    Returns:
        The rowcount; 0 without a statement when there is nothing to match.
    """
    if audit_mention_predicate(AuditLog, emails, subs) is None:
        return 0
    values: dict[str, Any] = {}
    ordered = _by_length(emails)
    if ordered:
        values["user_email"] = case(
            *((AuditLog.user_email == e, erasure_pseudonym(e)) for e in ordered),
            else_=AuditLog.user_email,
        )
        # every SET reads the row's old values: ``user_email`` before its rewrite
        acted = AuditLog.user_email.in_(ordered)
        for column in (AuditLog.ip_address, AuditLog.user_agent):
            values[column.key] = case((acted, null()), else_=column)
    values["resource"] = case(
        *(
            (AuditLog.resource == f"user:{v}", f"user:{erasure_pseudonym(v)}")
            for v in [*ordered, *_by_length(subs)]
        ),
        else_=AuditLog.resource,
    )

    def mentions(row: Any) -> tuple[ColumnElement[bool], ...]:
        return (cast(ColumnElement[bool], audit_mention_predicate(row, emails, subs)),)

    result = await db.execute(
        update(AuditLog)
        .where(AuditLog.id.in_(locked_ids(AuditLog, mentions)), *mentions(AuditLog))
        .values(values)
    )
    return cast(CursorResult[Any], result).rowcount or 0


async def count_audit_mentions(
    db: AsyncSession,
    emails: Collection[str],
    *,
    subs: Collection[str] = (),
    exclude_user_id: str | None = None,
) -> int:
    """What ``pseudonymize_audit_mentions`` would change (a dry run's count;
    the same predicate, no lock). ``exclude_user_id``: the rows the own-row
    UPDATE rewrites before the mentions (``pseudonymize_audit_trail``), so
    a dry run does not count them twice."""
    predicate = audit_mention_predicate(AuditLog, emails, subs)
    if predicate is None:
        return 0
    if exclude_user_id is not None:
        predicate = predicate & (AuditLog.user_id != exclude_user_id)
    return (await db.execute(select(func.count(AuditLog.id)).where(predicate))).scalar_one()


async def pseudonymize_audit_trail(
    db: AsyncSession,
    user_id: str | None,
    emails: Collection[str],
    *,
    orphaned_only: bool = False,
    pseudonym: str | None = None,
    mention_emails: Collection[str] | None = None,
    mention_subs: Collection[str] | None = None,
) -> tuple[int, int]:
    """The subject's own audit rows and the rows that mention them (#2287, #2298).

    The one entry point of the live erasure and the re-run. One lock phase
    first, in id order, over the union of every row the statements below
    rewrite (``user_id = :sub`` when given, the mention predicate, and the
    rows of ``pseudonym`` when given — the re-run's #2345 sweep that follows
    in its transaction): two erasures of members who acted on each other
    (one's own row is the other's mention) then queue instead of
    deadlocking. Then ``pseudonymize_audit_logs`` (when ``user_id`` is
    given) and ``pseudonymize_audit_mentions`` with ``emails`` and
    ``subs=[user_id]`` — or ``mention_emails`` / ``mention_subs`` when given
    (the re-run: the emails a user row holds again are left out of the
    mentions, not of the subject's own rows; the sweep runs once per run).

    Returns:
        ``(own rows, mentions)`` rowcounts.
    """
    if mention_subs is not None:
        subs: Collection[str] = tuple(mention_subs)
    else:
        subs = (user_id,) if user_id is not None else ()
    mentioned = tuple(emails) if mention_emails is None else tuple(mention_emails)

    def union(row: Any) -> tuple[ColumnElement[bool], ...]:
        arms: list[ColumnElement[bool]] = []
        if user_id is not None:
            arms.append(_own_audit_rows(row, user_id, orphaned_only=orphaned_only))
        mention = audit_mention_predicate(row, mentioned, subs)
        if mention is not None:
            arms.append(mention)
        if pseudonym is not None:
            arms.append(row.user_id == pseudonym)
        return (or_(*arms),)

    if user_id is None and pseudonym is None and not mentioned and not subs:
        return 0, 0
    await db.execute(locked_ids(AuditLog, union))
    own = (
        await pseudonymize_audit_logs(db, user_id, emails, orphaned_only=orphaned_only)
        if user_id is not None
        else 0
    )
    return own, await pseudonymize_audit_mentions(db, mentioned, subs=subs)


# The erasure's own audit row (``AccountErasureService._write_audit_log``).
ERASURE_AUDIT_ACTION = "account_erasure"


def _request_metadata_rows(row: Any, pseudonym: str) -> tuple[ColumnElement[bool], ...]:
    return (
        row.user_id == pseudonym,
        or_(row.ip_address.is_not(None), row.user_agent.is_not(None)),
    )


async def clear_audit_request_metadata(
    db: AsyncSession, pseudonym: str, *, dry_run: bool = False
) -> set[int]:
    """Clear ``ip_address`` / ``user_agent`` on the audit rows that carry the
    erased subject's ``pseudonym`` (#2345): rows an erasure pseudonymized
    before the live path cleared them, and the ``account_erasure`` row that
    copied the request's. By the pseudonym, never the raw sub; the rows are
    locked in id order. Idempotent.

    Returns:
        The ids of the rows changed (``dry_run``: that would change; no lock).
    """
    if dry_run:
        row = aliased(AuditLog)
        return set(
            (
                await db.execute(select(row.id).where(*_request_metadata_rows(row, pseudonym)))
            ).scalars()
        )
    result = await db.execute(
        update(AuditLog)
        .where(
            AuditLog.id.in_(
                locked_ids(AuditLog, lambda row: _request_metadata_rows(row, pseudonym))
            ),
            *_request_metadata_rows(AuditLog, pseudonym),
        )
        .values(ip_address=None, user_agent=None)
        .returning(AuditLog.id)
    )
    return set(result.scalars())


async def pseudonymize_erasure_audit_initiator(
    db: AsyncSession, pseudonym: str, *, dry_run: bool = False
) -> set[int]:
    """The ``account_erasure`` rows of ``pseudonym`` whose
    ``user_metadata.initiated_by`` is the subject's raw sub get the
    pseudonym there (#2345: the rule of ``pseudonymized_request_values``,
    decided by comparison — the value's pseudonym is the subject's — not by
    ``is_self_service``). An admin's id, a value already pseudonymized and
    a missing key are left. One or two rows per erasure, locked in id
    order; the column is ``json``, so the whole value is written.

    Returns:
        The ids of the rows changed (``dry_run``: that would change; no lock).
    """
    stmt = (
        select(AuditLog.id, AuditLog.user_metadata)
        .where(AuditLog.action == ERASURE_AUDIT_ACTION, AuditLog.user_id == pseudonym)
        .order_by(AuditLog.id)
    )
    if not dry_run:
        stmt = stmt.with_for_update()
    changed: set[int] = set()
    for row_id, metadata in (await db.execute(stmt)).all():
        initiated_by = metadata.get("initiated_by") if isinstance(metadata, dict) else None
        if (
            not isinstance(initiated_by, str)
            or initiated_by == pseudonym
            or erasure_pseudonym(initiated_by) != pseudonym
        ):
            continue
        changed.add(row_id)
        if not dry_run:
            await db.execute(
                update(AuditLog)
                .where(AuditLog.id == row_id)
                .values(user_metadata={**metadata, "initiated_by": pseudonym})
            )
    return changed


def ran_in_postgres() -> ColumnElement[bool]:
    """The request's Postgres step committed: only a run writes
    ``deleted_data_summary.postgres`` (since #360) — after that commit
    before v0.125.0, inside its transaction since (#2286)."""
    return func.coalesce(ErasureRequest.deleted_data_summary.has_key("postgres"), False)


def _never_ran() -> ColumnElement[bool]:
    """A ``complete`` request without ``postgres`` in its summary: the
    sweep's ``user_row_already_absent`` rows, its only writer (#2286; the
    table and the ``postgres`` summary both came with #360)."""
    return (ErasureRequest.status == STATUS_COMPLETE) & ~ran_in_postgres()


def legacy_note_row() -> ColumnElement[bool]:
    """A note row (``_never_ran``) the sweep wrote before v0.125.0: it stores
    the raw sub, not the pseudonym (#2302: ``reprocess_erasures`` repairs it)."""
    return _never_ran() & ~note_row_pseudonymized()


def merged_request_summary(keys: dict[str, Any]) -> ColumnElement[Any]:
    """``deleted_data_summary || keys`` in SQL, keeping what the row holds.

    A summary still stored as the JSON value ``null`` (written before #2026
    made the column store SQL NULL) is treated like SQL NULL: ``'null' ||
    {...}`` is a JSON *array*, which would hide ``postgres`` from
    ``has_key`` (``ran_in_postgres``).
    """
    stored = func.nullif(ErasureRequest.deleted_data_summary, literal("null").cast(JSONB))
    return func.coalesce(stored, literal({}, JSONB)).op("||")(literal(keys, JSONB))


async def record_resynced_payloads(
    db: AsyncSession, request_id: UUID, postgres: Mapping[str, Any], report: RestoreReport
) -> bool:
    """Record the payload re-drive of a run interrupted after its Postgres
    commit (#2302) on its request row, as the live run's step 3b would:
    ``payload_counts(report)`` merged into the ``postgres`` section
    (``postgres`` = what the row held), zeros included — the presence of
    ``details_payload_synced`` is what tells ``reprocess_erasures`` the
    re-drive ran. Guarded by ``status = failed`` and the
    ``after_postgres_commit:`` marker (the sweep's ``interrupted`` close, or a
    failure inside the run's own payload step — review of #2302): a row the
    run's own ``_finalize`` completed meanwhile is left alone.
    Does not commit.

    Returns:
        Whether the row was written.
    """
    result = await db.execute(
        update(ErasureRequest)
        .where(
            ErasureRequest.id == request_id,
            ErasureRequest.status == STATUS_FAILED,
            _failed_after_commit_marked(),
        )
        .values(
            deleted_data_summary=merged_request_summary(
                {"postgres": {**postgres, **payload_counts(report)}}
            )
        )
        .execution_options(synchronize_session=False)
    )
    return bool(cast(CursorResult[Any], result).rowcount)


def postgres_section(summary: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """``summary.postgres`` when it is an object (the Postgres step's
    counts), else None."""
    postgres = (summary or {}).get("postgres")
    return postgres if isinstance(postgres, dict) else None


def payloads_resynced(summary: Mapping[str, Any] | None) -> bool:
    """The payload re-drive of an interrupted run recorded its outcome
    (``record_resynced_payloads``; the live run's step 3b writes the same
    key)."""
    return "details_payload_synced" in (postgres_section(summary) or {})


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
    return ran_in_postgres() & (
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
        & ran_in_postgres()
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
    rows = await db.execute(
        select(
            ErasureRequest.user_id,
            (unrepaired_erasure() | legacy_note_row()).label("derive"),
        ).where(finalized_erasure() | unrepaired_erasure() | _never_ran())
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
        details_stripped: Revision rows whose ``prior.details`` lost the
            subject's keys (memories others authored, #2275) or was replaced
            by the erased marker (memories the subject authored).
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
    *,
    existed_at: datetime | None = None,
) -> tuple[ColumnElement[bool], ...]:
    """Revision rows of a memory ``subject`` authored that still hold a
    ``prior.details`` (the authored arm: stripped whole).

    The keys the subject wrote on memories others authored leave their
    priors per key (``plan_details_erasure``, #2275), not through this
    predicate. ``subject_memory_ids`` is a pre-filter that drives the plan
    from the subject's own rows. ``prior ? 'details'`` makes a re-run match
    nothing. ``existed_at`` bounds it (``_authored_by``).
    """
    return (
        row.memory_id.in_(subject_memory_ids(subject)),
        row.prior.has_key("details"),
        _authored_by(row, subject, existed_at),
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
    model: type[Memory] | type[Context] | type[MemoryRevision] | type[AuditLog] | type[APIKey],
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
    (#2263), for ``audit_logs`` (#2287) and for connector ``api_keys`` (#2305).
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
    plan: DetailsErasurePlan | None = None,
) -> StripCounts:
    """Strip ``subject``'s ``details`` out of the revision log.

    Three UPDATEs, in this order:

    1. On memories others authored (#2275): the subject's keys leave every
       prior that holds them (``plan.strips``: the states in which the
       replay attributes a key's value to the subject — written by them, or
       carried unchanged by a later whole-dict edit, or put back over theirs
       by a session rollback). The rest of each dict stays — a rebuilt
       version shows the other members' keys — and the removed keys'
       digests join ``PRIOR_ERASED_DETAILS_KEYS_KEY`` (no key name; read
       only by a later erasure's replay). One lock phase over all those
       rows, in id order, then one ``jsonb - text[]`` UPDATE per distinct
       key set (``_strip_editor_keys``).
    2. On memories the subject authored: ``prior.details`` is removed from
       every revision and replaced by ``{"erased": ["details"]}``
       (``PRIOR_ERASED_KEY``) so a rebuilt earlier version reports its
       details as unavailable instead of passing another value off as it.
       The overwritten text stays, as the memory text itself does.
    3. every ``updated`` row of a memory the subject authored gets
       ``{"scrubbed": ["details"]}`` (``PRIOR_SCRUBBED_KEY``): the live
       ``details`` that a rebuild starts from is NULLed by the memory scrub,
       so no version below those events can read it either.

    Without ``plan`` it first locks the subject's memories
    (``lock_subject_memories``), so no edit adds a revision to their logs
    until the caller commits, and reads their logs under those locks. During
    erasure this runs BEFORE the subject's events are pseudonymized (1 finds
    their edits by ``actor_user_id``) and before the memory scrub (2 and 3
    find the authored memories by ``memories.user_id``).

    Args:
        db: The session; the caller owns the transaction.
        subject: The raw sub during erasure, or the stored salted pseudonym
            when re-run over a past erasure (#2243).
        existed_at: On a re-run by pseudonym, the erasure's
            ``memory_scrub_at`` (or, before #2268, its completion): 2 and 3
            then reach only rows written by then (``_authored_by``), so what
            surviving members wrote on the scrubbed memories since stays as
            it is. None during erasure (every row predates it). 1 needs no
            bound: a key attributed to the subject is theirs whenever it was
            written.
        dry_run: Count the rows the UPDATEs would change; write and lock
            nothing.
        plan: The subject's ``plan_details_erasure``, built by the caller
            from the logs read under the memory locks
            (``erase_subject_details``); nothing is locked or read for 1
            then. None builds it here.

    Returns:
        The rowcounts (or the counts they would have); both 0 on a re-run.
    """
    if plan is None:
        if not dry_run:
            await lock_subject_memories(db, subject)
        plan = await plan_subject_details(db, subject)
    if dry_run:
        return StripCounts(
            details_stripped=len(plan.strips)
            + await _count(db, lambda row: strip_predicate(row, subject, existed_at=existed_at)),
            scrub_marked=await _count(
                db, lambda row: scrub_mark_predicate(row, subject, existed_at=existed_at)
            ),
        )
    editor_stripped = await _strip_editor_keys(db, plan.strips)
    stripped = cast(
        CursorResult[Any],
        await db.execute(
            update(MemoryRevision)
            .where(
                MemoryRevision.id.in_(
                    locked_revision_ids(
                        lambda row: strip_predicate(row, subject, existed_at=existed_at)
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
    return StripCounts(
        details_stripped=editor_stripped + (stripped.rowcount or 0),
        scrub_marked=marked.rowcount or 0,
    )


async def _strip_editor_keys(db: AsyncSession, strips: Sequence[PriorStrip]) -> int:
    """Remove each ``PriorStrip``'s keys from its row's ``prior.details`` (#2275).

    The rows are locked first, all of them in one id-ordered ``FOR UPDATE``
    (the order every revision statement takes, #2242); every one belongs
    to a memory the caller holds. Then one UPDATE per distinct key set:
    ``jsonb_set(prior, '{details}', prior->'details' - keys)``, and the keys'
    digests appended to ``PRIOR_ERASED_DETAILS_KEYS_KEY`` (a later erasure's
    walk reads them as an erased member's values, not as removals). The guards
    keep a row whose ``details`` is gone or not an object (NULL: ``-`` on a
    scalar raises) as it is.

    Returns:
        How many rows were rewritten.
    """
    if not strips:
        return 0
    await db.execute(
        select(MemoryRevision.id)
        .where(
            MemoryRevision.id
            == any_(
                bindparam(
                    "strip_ids", [s.row_id for s in strips], type_=ARRAY(BigInteger), unique=True
                )
            )
        )
        .order_by(MemoryRevision.id)
        .with_for_update()
    )
    groups: dict[tuple[str, ...], list[int]] = defaultdict(list)
    for strip in strips:
        groups[strip.keys].append(strip.row_id)
    total = 0
    details = MemoryRevision.prior["details"]
    for keys in sorted(groups):
        result = cast(
            CursorResult[Any],
            await db.execute(
                update(MemoryRevision)
                .where(
                    MemoryRevision.id
                    == any_(bindparam("ids", groups[keys], type_=ARRAY(BigInteger), unique=True)),
                    MemoryRevision.prior.has_key("details"),
                    func.jsonb_typeof(details) == "object",
                )
                .values(
                    prior=func.jsonb_set(
                        func.jsonb_set(
                            MemoryRevision.prior,
                            bindparam("path", ["details"], type_=ARRAY(Text), unique=True),
                            details.op("-", return_type=JSONB)(
                                bindparam("keys", list(keys), type_=ARRAY(Text), unique=True)
                            ),
                            type_=JSONB,
                        ),
                        bindparam(
                            "marker_path",
                            [PRIOR_ERASED_DETAILS_KEYS_KEY],
                            type_=ARRAY(Text),
                            unique=True,
                        ),
                        func.coalesce(
                            MemoryRevision.prior[PRIOR_ERASED_DETAILS_KEYS_KEY],
                            type_coerce([], JSONB),
                        ).op("||", return_type=JSONB)(
                            type_coerce(sorted(details_key_digest(k) for k in keys), JSONB)
                        ),
                        type_=JSONB,
                    )
                )
                .execution_options(synchronize_session=False)
            ),
        )
        total += result.rowcount or 0
    return total


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
# #2244, #2275: the live details keys an erased member wrote on another
# author's memory — per key, replayed from the log (``details_provenance``)
# ---------------------------------------------------------------------------


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
        restored: Memories whose ``details`` lost the subject's keys: each
            went back to the value from before their write, or was removed
            when there was none (#2275; NULL when nothing is left over a
            NULL value). Includes a restore whose value equals the live one
            (#2393): it still writes its version and row.
        unavailable: Memories where the value to go back to for at least one
            key was removed by an earlier erasure: the key was removed. Not
            counted in ``restored``.
        keys_reverted: The subject's live keys put back or removed, over all
            those memories.
        keys_ambiguous: Live keys kept because a state an erasure before
            v0.129.0 stripped whole may hold the subject's value
            (``plan_details_erasure``); never reverted.
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
    keys_reverted: int = 0
    keys_ambiguous: int = 0
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


def memory_scrub_at(summary: Mapping[str, Any] | None, *, strict: bool = True) -> datetime | None:
    """``summary.postgres.memory_scrub_at`` (#2268) as naive UTC, or None.

    When the erasure locked the subject's memories, as ``_execute`` stores it
    in the request's summary (since v0.124.0). None when the summary has no
    ``postgres`` section or no such key.

    Raises:
        ValueError: The value is not an ISO time and ``strict`` (the re-run's
            strip bound: a wrong bound would strip rows it must not). With
            ``strict=False`` it gives None (the payload re-drive, which then
            reads without a bound).
    """
    scrubbed = (postgres_section(summary) or {}).get("memory_scrub_at")
    if not isinstance(scrubbed, str):
        return None
    try:
        return to_naive_utc(datetime.fromisoformat(scrubbed))
    except ValueError:
        if strict:
            raise
        return None


async def restored_payload_syncs(
    db: AsyncSession, pseudonym: str, *, since: datetime | None
) -> list[PayloadSync]:
    """The memories an erasure run restored under ``pseudonym`` (#2302).

    Read back from the restore's own revision rows (``restore_editor_details``
    writes one per restored memory: ``kind='updated'``, ``writer='erasure'``,
    ``actor_user_id`` = the pseudonym, ``at`` = after ``memory_scrub_at``),
    for a run whose in-memory ``RestoreReport`` was lost with its process
    (interrupted after the Postgres commit, #2286): what
    ``sync_restored_payloads`` needs to patch their vector payloads. One
    entry per memory, at its newest such revision's version — a row edited
    since has moved past it and is skipped by the patch, as in the live run.
    A later restore under the same pseudonym (the re-run, or an erasure of
    the same sub registered again) is included too: the patch is idempotent.

    ``since`` is the run's ``memory_scrub_at``. None (a summary without a
    usable one, or a note row of a run interrupted on v0.124.x) reads every
    such row of the pseudonym: an earlier restore under it is patched too,
    which the idempotent, version-checked patch makes harmless — leaving a
    restored point with the erased member's ``location`` is not. Reads
    ``idx_memory_revisions_actor``: the pseudonym's rows only.
    """
    bound = [] if since is None else [MemoryRevision.at >= since]
    rows = await db.execute(
        select(MemoryRevision.memory_id, func.max(MemoryRevision.version))
        .where(
            MemoryRevision.actor_user_id == pseudonym,
            MemoryRevision.writer == REVISION_WRITER_ERASURE,
            MemoryRevision.kind == REVISION_KIND_UPDATED,
            *bound,
        )
        .group_by(MemoryRevision.memory_id)
        .order_by(MemoryRevision.memory_id)
    )
    return [PayloadSync(memory_id, version) for memory_id, version in rows.all()]


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


async def _updated_rows(db: AsyncSession, subject: str) -> list[DetailsEdit]:
    """The ``updated`` rows of the memories ``subject`` edited and did not author.

    The input of ``plan_details_erasure`` (#2275). Every ``updated`` row,
    ``details_changed`` set on those that changed ``details``; the replay
    reads those only.

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
                MemoryRevision.prior[PRIOR_ERASED_DETAILS_KEYS_KEY].label("erased_keys"),
                MemoryRevision.prior[PRIOR_RESTORED_FROM_KEY].as_integer().label("restored_from"),
                MemoryRevision.prior[PRIOR_DETAILS_RESTORED_FROM_KEY].label("restored_from_by_key"),
                MemoryRevision.prior.has_key(PRIOR_DETAILS_RESTORED_FROM_KEY).label(
                    "has_restore_map"
                ),
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
            erased_keys=frozenset(row.erased_keys if isinstance(row.erased_keys, list) else ()),
            restored_from=row.restored_from,
            restored_from_by_key=(
                (row.restored_from_by_key if isinstance(row.restored_from_by_key, dict) else {})
                if row.has_restore_map
                else None
            ),
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
    if memory.embedding_error == ERASED_AUTHOR_EMBEDDING_ERROR:
        # #2393: the author was erased; the row stays out of the queue until
        # the admin retry or a model switch (no point to rebuild for it).
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
    Runs the shared post-commit payload step (``payload_sync``, #2283, as
    ``session_rollback`` does) in groups of ``PAYLOAD_SYNC_GROUP`` rows, one
    transaction each: the group's rows are read
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
    queued already (``RequeueEmbeddingOnFailure``). A re-queue that fails too
    is counted (``payload_requeue_failed``) and logged, and the next row goes
    on. Each row runs in a SAVEPOINT, so a failed statement leaves the group
    usable.
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
    versions = {sync.memory_id: sync.version for sync in syncs}
    routes: dict[UUID, tuple[str, bool]] = {}
    policy = RequeueEmbeddingOnFailure("erasure_details_payload", reason="erasure_details_payload")

    def plan(memory_id: UUID, rows: Mapping[UUID, Any] | None) -> PlannedPatch[Memory] | None:
        memory = rows.get(memory_id) if rows is not None else None
        if memory is None or memory.deleted_at is not None or memory.context_id is None:
            report.payload_skipped_tombstone += 1
            return None
        if memory.version != versions[memory_id]:
            report.payload_skipped_moved += 1
            logger.info(
                "erasure_details_payload_moved",
                memory_id=str(memory_id),
                restored_version=versions[memory_id],
                version=memory.version,
            )
            return None
        return PlannedPatch(memory.id, memory, embedded=memory.embedding_status == "success")

    async def apply(patch: PlannedPatch[Memory]) -> None:
        await _sync_one_payload(db, patch.data, routes, report)

    try:
        await sync_payloads_after_commit(
            db, sorted(versions), memory_id=lambda i: i, plan=plan, apply=apply, on_failure=policy
        )
    finally:
        report.payload_requeued_after_error += policy.requeued_after_error
        report.payload_skipped_requeued += policy.skipped_requeued
        report.payload_requeue_failed += policy.requeue_failed


async def _sync_one_payload(
    db: AsyncSession,
    memory: Memory,
    routes: dict[UUID, tuple[str, bool]],
    report: RestoreReport,
) -> None:
    """Patch one restored row's point (``sync_restored_payloads``).

    Raises on any failure: ``RequeueEmbeddingOnFailure`` re-queues the row.
    """
    from services.content_chunk_writer import chunk_cascade_needed
    from services.context_routing import resolve_collection_name
    from services.memory_service import (
        metadata_patch_target,
        metadata_payload_patch,
        restore_payload_request,
    )

    async with db.begin_nested():
        updates, delete_keys = metadata_payload_patch(
            restore_payload_request(memory, {"details"}), None, memory.details
        )
        # Issue #78: date-range filters read the payload's updated_at.
        updates["updated_at"] = to_utc_iso(memory.updated_at)
        target = await metadata_patch_target(db, memory.id, memory.point_id, updates, delete_keys)
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


async def plan_subject_details(
    db: AsyncSession,
    subject: str,
    *,
    erased_actors: Collection[str] = frozenset(),
    rows: Sequence[DetailsEdit] | None = None,
) -> DetailsErasurePlan:
    """``plan_details_erasure`` over the logs of the memories ``subject``
    edited and did not author (#2275).

    Reads ``_updated_rows`` (unless given) and the live ``details`` and
    author of each of those memories — under the caller's memory locks
    during erasure, as of the statement on a dry run.
    """
    if rows is None:
        rows = await _updated_rows(db, subject)
    ids = sorted({row.memory_id for row in rows})
    if not ids:
        return DetailsErasurePlan()
    live = {
        memory_id: (details, author)
        for memory_id, details, author in (
            await db.execute(
                select(Memory.id, Memory.details, Memory.user_id)
                .where(Memory.id.in_(ids))
                .order_by(Memory.id)
            )
        ).all()
    }
    return plan_details_erasure(rows, live, subject, erased_actors=erased_actors)


async def restore_editor_details(
    db: AsyncSession,
    subject: str,
    *,
    actor: str,
    dry_run: bool = False,
    erased_actors: Collection[str] = frozenset(),
    plan: DetailsErasurePlan | None = None,
) -> RestoreReport:
    """Put back the ``details`` keys ``subject`` wrote on memories others authored.

    Per key (#2275, ``plan_details_erasure``): every live key whose value
    the subject introduced — wrote, or left live under a later member's
    whole-dict edit that carried it unchanged — goes back to the value it
    had before their write (past every value an erased member wrote), or is
    removed when it had none. Keys a later member rewrote stay theirs; keys
    the subject removed stay removed. A key whose value to go back to an
    earlier erasure stripped is removed (``unavailable``). Memories the
    subject authored are not touched here: ``_scrub_surviving_memories``
    NULLs their ``details``.

    Each restore is an edit of its own: ``version`` + 1, ``updated_at`` now,
    and one ``updated`` revision row with ``writer='erasure'``, ``actor`` as
    its actor and ``{"details": <the overwritten value minus the subject's
    keys>, "erased_details_keys": <their digests>, "restored_from":
    <from_version>, "details_restored_from": <key digest to version>}`` as
    its prior (#2393) — stripped like
    every other prior, so the subject's value is in none, a later erasure
    can tell whose keys are live, ``expected_version`` protects the restored
    value from a stale edit, and a rebuilt version shows the other members'
    keys. The memory's last writer (``agent_id`` / ``session_id``) becomes
    none. A restore to a value equal to the live one still writes its
    version and row (#2393): the row records where each key's value came
    from, which a later erasure reads.

    Order and locks: without ``plan``, the subject's memories are locked
    first, in id order (``lock_subject_memories``, the order every edit and
    session rollback takes), so the log read after it is exact and no edit
    lands in between. During erasure this runs BEFORE
    ``strip_subject_details`` and ``pseudonymize_revision_actor``: it finds
    the subject's edits by ``actor_user_id == subject``, and the new row's
    actor is the pseudonym. The locks are held until the caller commits. No
    vector-store call happens here (#2266): a restored live row whose
    embedding has not succeeded is re-queued in this transaction, and every
    restored row is listed in ``report.to_sync`` for
    ``sync_restored_payloads``, which the caller runs after its commit.

    The resource indexer (and a Sleep merge) rewrites ``details`` without a
    revision; the replay sees that change in the next edit's prior and
    attributes it to the edit before it — when that edit is the subject's,
    the restore puts the indexer's keys back to their older values and the
    strip removes the indexer's values from the priors, until the indexer's
    next run writes them again. No special case (a known residual).

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
        erased_actors: Pseudonyms of other erased members
            (``erased_pseudonyms``): their values are walked past.
        plan: The subject's plan, built by the caller under the memory locks
            (``erase_subject_details``); nothing is locked or read for it
            then. None builds it here.

    Returns:
        The counts, the restored memories and the payloads to sync.
    """
    if plan is None:
        locked = 0 if dry_run else await lock_subject_memories(db, subject)
        plan = await plan_subject_details(db, subject, erased_actors=erased_actors)
        logger.info("erasure_restore_locked", locked=locked)
    report = RestoreReport(keys_ambiguous=plan.keys_ambiguous)
    logger.info("erasure_restore_candidates", targets=len(plan.reverts))
    if not plan.reverts:
        return report
    memories = {
        memory.id: memory
        for memory in (
            await db.execute(
                select(Memory)
                .where(Memory.id.in_([r.memory_id for r in plan.reverts]))
                .order_by(Memory.id)
                .execution_options(populate_existing=True)
            )
        ).scalars()
    }
    for revert in plan.reverts:
        memory = memories.get(revert.memory_id)
        if memory is None:
            continue  # gone since the plan read (never under the locks)
        value, trigger_kept = _with_live_trigger(memory, copy.deepcopy(revert.result))
        # A value equal to the live one is still written (#2393): the plan
        # lists only memories holding a live key of the subject's, so that
        # value is still attributed to the subject's edit. Without this row a
        # later erasure of the member who wrote it first finds none of its own
        # keys live, and the result depends on the erasure order. The one
        # exception is a revert of the trigger alone whose live window went
        # back in: nothing of anyone else's is put back, and the row would
        # attribute that window to the subject again, so every re-run would
        # write another.
        if (
            memory.details == value
            and trigger_kept
            and all(r.key == "trigger" for r in revert.reverts)
        ):
            continue
        if revert.unavailable:
            report.unavailable += 1
        else:
            report.restored += 1
        report.keys_reverted += len(revert.reverts)
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
            prior={
                "details": copy.deepcopy(revert.prior),
                PRIOR_ERASED_DETAILS_KEYS_KEY: list(revert.erased_keys),
                PRIOR_RESTORED_FROM_KEY: revert.from_version,
                PRIOR_DETAILS_RESTORED_FROM_KEY: revert.restored_from_by_key,
            },
            writer=REVISION_WRITER_ERASURE,
            provenance=WriteProvenance(None, None),
        )
        _requeue_unembedded(memory, report)
        report.to_sync.append(PayloadSync(memory.id, memory.version))
        logger.info(
            "erasure_details_restored",
            memory_id=str(memory.id),
            keys=len(revert.reverts),
            from_version=revert.from_version,
            unavailable=revert.unavailable,
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
    their logs and live ``details`` are read once under those locks, and one
    ``plan_details_erasure`` (#2275) drives both steps: the ``erasure`` rows
    the restore adds sit on top of each log and hold no key of the
    subject's, so the strip works from the same plan.

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
    plan = await plan_subject_details(db, subject, erased_actors=erased_actors, rows=rows)
    logger.info(
        "erasure_details_rows",
        locked=locked,
        rows=len(rows),
        memories=len(plan.memory_ids),
        reverts=len(plan.reverts),
        strips=len(plan.strips),
        ambiguous=plan.keys_ambiguous,
    )
    restore = await restore_editor_details(
        db, subject, actor=actor, dry_run=dry_run, erased_actors=erased_actors, plan=plan
    )
    strip = await strip_subject_details(
        db, subject, existed_at=existed_at, dry_run=dry_run, plan=plan
    )
    return SubjectDetailsErasure(restore=restore, strip=strip)
