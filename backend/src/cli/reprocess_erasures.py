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

1. where the erased member wrote live ``details`` keys of a memory someone
   else authored, each goes back to its value from before their write (a new
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

Since v0.129.0 (#2275) 1 and 2 work per top-level ``details`` key: the keys
whose value the erased member introduced — also where a later member's
whole-dict edit carried them unchanged — go back (or are removed), keys a
later member rewrote stay, and only the member's keys leave each prior.
For an erasure that ran before v0.129.0 the state the member wrote was
stripped whole, so a key they wrote and a later member carried cannot be
told from the later member's own: it is kept and counted, never reverted
(``details keys ambiguous ... kept``, printed by the plan as by the apply;
the plan is enough to read the count).

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
id (before the revision log of v0.113.0 anyway). Such a note row written
before v0.125.0 is still pseudonymized (#2302): it gets the pseudonym derived
from the account id, no request metadata and the ``pseudonymized`` flag, as
the sweep writes it since. A request the sweep found interrupted after its
Postgres step (``after_postgres_commit: interrupted: sweep``, #2286) ran and is
processed like any other; when a request marked ``after_postgres_commit:``
has no ``details_payload_synced`` in its summary (the sweep's re-drive never
recorded, or the run failed inside its payload step, #2302), the memories its
restore put back (its ``erasure`` revision rows) have their vector payloads
patched once and the counts are recorded. A request that failed
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
and ``user_email`` is rewritten where it is one of the account's addresses
(below; left as it is when none is recovered). An account id that belongs to a user again (re-registered) is
never matched: its rows are kept (``user_id`` stays) and the request is
listed — but a row of theirs that carries the erased address in
``user_email`` (not in use) is matched by the mention sweep below: that
address becomes its pseudonym and the row's IP / user agent are cleared. Running it again changes
nothing.

Other actors' rows that name the subject (#2298) — an admin acting on them
(``resource = 'user:'||email``), or the subject acting as an admin on
someone else (``user_email = email``) — get the same pseudonyms, by exact
value only, once per pseudonym per run, in the request's transaction.

Which address, and when (#2359, ``erasure_details.email_holdings``): the
account's addresses are rebuilt from what the database already holds. Each
of its requests is one tenure of the account (an account erased, re-created
under the same id and erased again has two), from its first audit row after
the previous tenure to the request's end (``memory_scrub_at``, else
``completed_at``, else ``started_at`` plus an hour). Within a tenure the
email-sync audit rows (``oauth_user_email_synced``: the HMACs of the old and
the new address, kept by erasure) give each address's holding; the plaintext
is recovered from the table's ``user_email`` values and ``user:`` resources
by HMAC (``AUDIT_HMAC_KEY``), the last address by the request's
``user_email_hash``. A row naming an address is rewritten only when it was
written inside the account's holding and outside every interval another
account is known to have held the address (its sync rows, a user row that
holds it now — from its creation —, another erasure request carrying the
address — to its end); an instant both share goes to the other account.

Limits — fail closed, counts only (never an address, a hash or an id):

- ``audit mentions <request> N row(s) kept``: rows naming an address inside
  the account's holding and inside another account's; left raw.
- ``audit addresses <request> N ... not recovered``: holdings whose start,
  end or plaintext could not be told — an earlier address whose HMAC
  matches no value in the table (no row names it in plaintext any more, or
  ``AUDIT_HMAC_KEY`` changed since the sync), a break in the sync chain, a
  tenure with no audit row of the account; rows naming them are left raw.
  The count rises on a second run after an apply: the first one
  pseudonymized the rows the plaintext was recovered from. A request's own
  address that no row names in plaintext is not counted (its hash is
  unsalted: nothing is left raw), nor an address changed with no email-sync
  row to record it (unknown, as before).
- Rows an erasure or a re-run before v0.134.0 rewrote by address without a
  time bound — possibly another holder's — carry the pseudonym and cannot
  be told apart any more: they stay as they are.

The dry run writes and locks nothing and makes no vector-store call; it
prints request ids and counts, never values or account ids; its counts are
the apply's.

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
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import exists, func, select, union, update
from sqlalchemy.engine import CursorResult  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from cli._oneshot import add_log_level_argument, configure_logging, run_plan_apply  # noqa: E402
from models.auth import AuditLog, User  # noqa: E402
from models.erasure import (  # noqa: E402
    FAILURE_AFTER_POSTGRES_COMMIT,
    NOTE_PSEUDONYMIZED_KEY,
    STATUS_COMPLETE,
    ErasureRequest,
)
from models.memory import Memory, MemoryRevision  # noqa: E402
from services.erasure_details import (  # noqa: E402
    AUDIT_SUBJECT_METADATA_KEYS,
    ERASURE_AUDIT_ACTION,
    REPROCESS_AUDIT_ACTION,
    EmailHoldings,
    PayloadSync,
    RestoreReport,
    StripCounts,
    SubjectDetailsErasure,
    Tenure,
    audit_actor_predicate,
    clear_audit_request_metadata,
    count_audit_mentions,
    count_audit_mentions_kept,
    email_holdings,
    email_sync_hashes,
    erase_subject_details,
    erased_pseudonyms,
    erasure_end,
    erasure_pseudonym,
    finalized_erasure,
    is_pseudonym,
    legacy_note_row,
    memory_scrub_at,
    merged_request_summary,
    note_row_pseudonymized,
    payload_counts,
    payloads_resynced,
    postgres_section,
    pseudonymize_audit_trail,
    pseudonymize_erasure_audit_initiator,
    pseudonymized_request_values,
    record_resynced_payloads,
    recover_addresses,
    restored_payload_syncs,
    sync_restored_payloads,
    unrepaired_erasure,
)
from utils.datetime import to_utc_iso, utcnow  # noqa: E402
from utils.logger import get_logger  # noqa: E402

logger = get_logger(__name__)

AUDIT_ACTION = REPROCESS_AUDIT_ACTION
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
# #2302: never prints the account id it replaces
NOTE_REPAIRED = (
    "marked complete without running before v0.125.0: the account id is replaced by its pseudonym"
)
# #2287: never prints the account id or the email
AUDIT_KEPT = "rows kept: the account id is in use again"
# #2298, #2359: never prints the email
MENTIONS_KEPT = "row(s) kept: another account held the address at the time"
# #2359: never prints an address or a hash
NOT_RECOVERED = (
    "address holding(s) not recovered, their rows left raw (no plaintext or HMAC "
    "match, or an unknown start or end; see the module docstring)"
)
# the ``resource`` prefix of a row about a member (``user:{email}``)
USER_RESOURCE = "user:"


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
    # #2275: the erased member's live details keys put back or removed (on
    # ``details_restored`` + ``details_restored_unavailable`` memories), and
    # the live keys kept because a state an erasure before v0.129.0 stripped
    # whole may hold their value (never a change: nothing is written)
    details_keys_reverted: int = 0
    details_keys_ambiguous: int = 0
    # #2266: ``sync_restored_payloads``'s outcome after the commit (the
    # ``details_payload_*`` keys of the erasure summary); empty on a dry run
    payload: dict[str, int] = field(default_factory=dict)
    # #2268: the request row stored the account id and gets the pseudonym
    repaired: bool = False
    # #2287: the subject's audit rows that still carried the account id
    audit_logs_pseudonymized: int = 0
    # #2298: other actors' rows that named the subject's email or sub
    audit_mentions_pseudonymized: int = 0
    # #2359: rows naming an address of the subject's while another account
    # held it too (left raw), and holdings that could not be recovered
    # (never a change: nothing is written)
    audit_mentions_kept: int = 0
    audit_addresses_not_recovered: int = 0
    # #2345: rows of the pseudonym whose IP / user agent were cleared, plus
    # the ``account_erasure`` rows whose ``initiated_by`` was pseudonymized
    audit_metadata_cleared: int = 0
    # #2302: a note row from before v0.125.0 stored the account id and gets
    # the pseudonym
    note_repaired: bool = False
    # #2302: memories whose payloads are patched again: a run that failed or
    # was interrupted after its Postgres commit without recording its payload
    # step, or a legacy note row's (None: not such a request)
    payload_resync: int | None = None

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
            + int(self.note_repaired)
            + int(self.payload_resync is not None)
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
    # #2298, #2359: requests with rows naming an address another account
    # held at the time (kept), and with holdings not recovered; how many
    mentions_kept: list[tuple[UUID, int]] = field(default_factory=list)
    not_recovered: list[tuple[UUID, int]] = field(default_factory=list)
    # #2275: requests with live details keys kept as ambiguous, and how many
    ambiguous: list[tuple[UUID, int]] = field(default_factory=list)

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
    """Known audit subject-identifier candidates, keyed by salted pseudonym."""

    # no user row has them: an erased subject's residue is found here
    orphans: dict[str, str]
    # a user row has them: never matched, even when an erasure's pseudonym is
    # theirs; by pseudonym, the raw sub (#2359: the account's email history
    # up to its erasure is still read by it, never its rows rewritten)
    in_use: Mapping[str, str]


async def _audit_subs(db: AsyncSession, pseudonyms: Collection[str] | None = None) -> AuditSubs:
    """Recover raw subjects only from recognized audit identifier locations.

    A historical erasure may already have scrubbed every ``user_id`` while
    leaving only metadata or a raw-sub email fallback. Candidate strings are
    matched by their salted pseudonym to the request by the caller, never
    inferred from arbitrary text. Current users are excluded, and the write
    rechecks that exclusion. Known erased pseudonyms are not hashed again;
    a raw IdP subject may itself be 64 hex, so shape alone cannot exclude it.
    """
    if pseudonyms is not None and not pseudonyms:
        return AuditSubs({}, {})
    email = AuditLog.user_email
    named = func.substr(AuditLog.resource, len(USER_RESOURCE) + 1)
    candidates = union(
        select(AuditLog.user_id.label("sub")),
        select(email.label("sub")),
        select(func.left(email, func.length(email) - 4).label("sub")).where(
            email.endswith("@api", autoescape=True)
        ),
        select(named.label("sub")).where(
            AuditLog.resource.startswith(USER_RESOURCE, autoescape=True)
        ),
    ).subquery()
    rows = await db.stream(
        select(candidates.c.sub, exists().where(User.user_id == candidates.c.sub).label("in_use")),
        execution_options={"yield_per": 500},
    )
    orphans: dict[str, str] = {}
    in_use: dict[str, str] = {}
    async for sub, live in rows:
        if not sub or (pseudonyms is not None and sub in pseudonyms):
            continue
        pseudonym = erasure_pseudonym(sub)
        if pseudonyms is not None and pseudonym not in pseudonyms:
            continue
        if live:
            in_use[pseudonym] = sub
        else:
            orphans[pseudonym] = sub
    # Whole JSON only: even extracting an unrelated key rejects escaped U+0000.
    rows = await db.stream(
        select(AuditLog.user_metadata).where(AuditLog.user_metadata.is_not(None)),
        execution_options={"yield_per": 500},
    )
    async for (metadata,) in rows:
        if not isinstance(metadata, dict):
            continue
        for key in AUDIT_SUBJECT_METADATA_KEYS:
            sub = metadata.get(key)
            if (
                not isinstance(sub, str)
                or not sub
                or (pseudonyms is not None and sub in pseudonyms)
            ):
                continue
            # Legacy JSON accepts NUL and lone surrogates; a UTF-8 VARCHAR
            # auth sub cannot contain them. Unrelated residue must not stop the sweep.
            if "\x00" in sub:
                continue
            try:
                sub.encode("utf-8")
            except UnicodeEncodeError:
                continue
            pseudonym = erasure_pseudonym(sub)
            if pseudonyms is None or pseudonym in pseudonyms:
                orphans[pseudonym] = sub
    # Recheck all matched candidates, including those discovered only in metadata.
    subjects = list(orphans.values())
    for offset in range(0, len(subjects), 500):
        live = set(
            (
                await db.execute(
                    select(User.user_id).where(User.user_id.in_(subjects[offset : offset + 500]))
                )
            ).scalars()
        )
        for sub in live:
            pseudonym = erasure_pseudonym(sub)
            in_use[pseudonym] = sub
            orphans.pop(pseudonym, None)
    return AuditSubs(orphans, in_use)


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
    The same bound ends the subject's last address holding (#2359,
    ``erasure_end``).
    """
    return erasure_end(summary, completed_at, started_at)


def _tenure_end(
    summary: dict[str, Any] | None, completed_at: datetime | None, started_at: datetime | None
) -> datetime | None:
    """``erasure_end`` for another request of the account (#2359): a summary
    whose ``memory_scrub_at`` is no ISO time gives an unknown end — that
    tenure's holdings are counted as not recovered — instead of stopping
    the run before the request it belongs to."""
    try:
        return erasure_end(summary, completed_at, started_at)
    except ValueError:
        return None


def _wrote_pseudonym(summary: dict[str, Any] | None) -> bool:
    """The erasure's summary says it wrote the pseudonym on memory or revision
    rows (#2268 review). When it wrote none, no row can carry it and the
    repair goes ahead: the account id must not stay on the row."""
    postgres = postgres_section(summary)
    if postgres is None:
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


async def _repair_note_row(db: AsyncSession, request_id: UUID, account_id: str) -> bool:
    """Pseudonymize a note row the sweep wrote before v0.125.0 (#2302): the
    request was marked ``complete`` because the user row was already gone,
    and the row kept the account id. It gets what the sweep writes since
    #2286 — the pseudonym, no request metadata (``pseudonymized_request_values``,
    ``initiated_by`` decided in SQL: an admin's id stays) and
    ``pseudonymized: true`` in its summary. ``erased_pseudonyms`` derived the
    same pseudonym from the account id before, so no later walk changes.
    Scoped to the one row by its id, stored value and shape
    (``legacy_note_row``); status and ``completed_at`` stay.

    Returns:
        Whether the row was repaired (False when another run got there first).
    """
    result = await db.execute(
        update(ErasureRequest)
        .where(
            ErasureRequest.id == request_id,
            ErasureRequest.user_id == account_id,
            legacy_note_row(),
        )
        .values(
            **pseudonymized_request_values(account_id, None),
            deleted_data_summary=merged_request_summary({NOTE_PSEUDONYMIZED_KEY: True}),
        )
        .execution_options(synchronize_session=False)
    )
    return bool(cast(CursorResult[Any], result).rowcount)


def _merge_syncs(live: list[PayloadSync], derived: list[PayloadSync]) -> list[PayloadSync]:
    """``live`` (this run's restore) and ``derived`` (an interrupted run's,
    from the log), one per memory: a live entry wins, it carries the newer
    version."""
    merged = {sync.memory_id: sync for sync in derived}
    merged.update((sync.memory_id, sync) for sync in live)
    return [merged[memory_id] for memory_id in sorted(merged)]


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
    audit_only: bool = False,
    holdings: EmailHoldings | None = None,
    note_account_id: str | None = None,
    resync_postgres: dict[str, Any] | None = None,
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
    in the same transaction, ``user_email`` by every address of
    ``holdings``. ``audit_only`` (#2286, a note row the sweep
    pseudonymized): that audit scrub alone — the request never reached the
    Postgres step as far as its row says, so there is nothing to restore or
    strip by its pseudonym. ``holdings`` (#2359, ``email_holdings`` of the
    pseudonym, read by the caller; None once this pseudonym's sweep ran this
    run): other actors' rows naming one of the account's addresses while it
    held them, or its account id, are pseudonymized in the same transaction,
    under one lock with the subject's own rows; a row inside an interval
    another account held the address is left and counted. The same first
    time (#2345), the rows that carry ``pseudonym`` lose their request
    metadata (``clear_audit_request_metadata``,
    ``pseudonymize_erasure_audit_initiator``) — every caller passes
    ``holdings`` for a request that stores (or, repaired, gets) the
    pseudonym, and only then.
    ``note_account_id`` (#2302, a note row from before v0.125.0): the account
    id the row stores; the row gets the pseudonym in the same transaction
    (``_repair_note_row``), and the memories an erasure restored under that
    pseudonym (a run interrupted after its Postgres commit on v0.124.x ends
    up as such a row; a request that never ran has none) have their payloads
    patched after the commit, unbounded — once, as the repair is.
    ``resync_postgres`` (#2302, a run that failed or was interrupted after its
    Postgres commit without recording its payload step): the row's
    ``postgres`` summary; the memories that run restored
    (``restored_payload_syncs``, bounded by its ``memory_scrub_at``) are
    patched with this run's, and the outcome is recorded on the row
    (``record_resynced_payloads``), so a second run does not patch them again.
    """
    try:
        repaired = account_id is not None
        if repaired and not dry_run:
            repaired = await _repair(db, request_id, cast(str, account_id))
        note_repaired = note_account_id is not None
        if note_repaired and not dry_run:
            note_repaired = await _repair_note_row(db, request_id, cast(str, note_account_id))
        # #2302: a plain read of the log, before any memory lock
        derived: list[PayloadSync] = []
        if resync_postgres is not None or note_account_id is not None:
            since = (
                memory_scrub_at({"postgres": resync_postgres}, strict=False)
                if resync_postgres is not None
                else None
            )
            derived = await restored_payload_syncs(db, pseudonym, since=since)
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
        audited, mentions, kept = 0, 0, 0
        sweep = holdings is not None
        # the subject's own rows (by the account id) take every address of
        # theirs, without a window (the account id proves the row theirs),
        # so no own row is left for the mention UPDATE (the dry run's count
        # leaves them all out)
        addresses = holdings.addresses if holdings is not None else ()
        subs = [audit_account_id] if audit_account_id is not None and sweep else []
        if sweep:
            # read before any write: the plan's count is the apply's
            kept = await count_audit_mentions_kept(db, addresses, exclude_user_id=audit_account_id)
        if dry_run:
            if audit_account_id is not None:
                audited = await _count_audit_rows(db, audit_account_id)
            if sweep:
                # the apply rewrites the own rows first: none is counted twice
                mentions = await count_audit_mentions(
                    db,
                    addresses,
                    subs=subs,
                    exclude_user_id=audit_account_id,
                    orphaned_only=True,
                )
        else:
            # one lock phase over every audit row this request rewrites
            audited, mentions = await pseudonymize_audit_trail(
                db,
                audit_account_id,
                addresses,
                orphaned_only=True,
                pseudonym=pseudonym if sweep else None,
                mention_addresses=addresses,
                mention_subs=subs,
            )
        metadata_cleared = 0
        if sweep:
            # rows, not edits: one row can lose its IP and get the initiator
            cleared = await clear_audit_request_metadata(db, pseudonym, dry_run=dry_run)
            if dry_run and cleared:
                # The mention UPDATE clears its actor's IP/UA before this sweep.
                # Do not count those rows twice when they already carry the pseudonym.
                acted = audit_actor_predicate(AuditLog, addresses, subs, orphaned_only=True)
                if acted is not None:
                    cleared -= set(
                        (
                            await db.execute(
                                select(AuditLog.id).where(AuditLog.id.in_(cleared), acted)
                            )
                        ).scalars()
                    )
            initiators = await pseudonymize_erasure_audit_initiator(db, pseudonym, dry_run=dry_run)
            metadata_cleared = len(cleared | initiators)
        restore, strip = details.restore, details.strip
        change = RequestChange(
            request_id=request_id,
            completed_at=completed_at,
            details_restored=restore.restored,
            details_restored_unavailable=restore.unavailable,
            details_stripped=strip.details_stripped,
            scrub_marked=strip.scrub_marked,
            embedding_requeued=restore.embedding_requeued,
            details_keys_reverted=restore.keys_reverted,
            details_keys_ambiguous=restore.keys_ambiguous,
            repaired=repaired,
            audit_logs_pseudonymized=audited,
            audit_mentions_pseudonymized=mentions,
            audit_mentions_kept=kept,
            audit_addresses_not_recovered=holdings.not_recovered if holdings is not None else 0,
            audit_metadata_cleared=metadata_cleared,
            note_repaired=note_repaired,
            # a re-drive after the Postgres commit is a change even with
            # nothing to patch (its counts are recorded); a note row's only
            # when it has memories to patch (its repair is the change)
            payload_resync=(
                len(derived)
                if resync_postgres is not None or (note_account_id is not None and derived)
                else None
            ),
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
                    "details_keys_reverted": change.details_keys_reverted,
                    "details_keys_ambiguous": change.details_keys_ambiguous,
                    "request_repaired": change.repaired,
                    "audit_logs_pseudonymized": change.audit_logs_pseudonymized,
                    "audit_mentions_pseudonymized": change.audit_mentions_pseudonymized,
                    "audit_metadata_cleared": change.audit_metadata_cleared,
                    # #2359: counts of what was left raw, never values
                    "audit_mentions_kept": change.audit_mentions_kept,
                    "audit_addresses_not_recovered": change.audit_addresses_not_recovered,
                    "tool": TOOL,
                    # #2302: only on the requests they apply to
                    **({"request_note_pseudonymized": True} if change.note_repaired else {}),
                    **(
                        {"payloads_resynced": change.payload_resync}
                        if change.payload_resync is not None
                        else {}
                    ),
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
        details_keys_reverted=change.details_keys_reverted,
        details_keys_ambiguous=change.details_keys_ambiguous,
        request_repaired=change.repaired,
        audit_logs_pseudonymized=change.audit_logs_pseudonymized,
        audit_mentions_pseudonymized=change.audit_mentions_pseudonymized,
        audit_metadata_cleared=change.audit_metadata_cleared,
        request_note_pseudonymized=change.note_repaired,
        payloads_resynced=change.payload_resync,
    )
    try:
        await sync_restored_payloads(db, _merge_syncs(restore.to_sync, derived), restore)
        if resync_postgres is not None:
            if await record_resynced_payloads(db, request_id, resync_postgres, restore):
                await db.commit()
            else:
                # the row moved since it was read: nothing recorded
                await db.rollback()
                logger.info("erasure_reprocessed_request_moved", request_id=str(request_id))
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
    naming the subject (#2298), once per pseudonym, by each address only
    while the subject held it (#2359, ``email_holdings``: one tenure per
    request of the pseudonym, the addresses recovered once per run).

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
            ErasureRequest.failure_reason,
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
    # account's other requests carry its other emails (#2359: its other
    # tenures), and once its audit rows carry the pseudonym no later run can
    # reach them again
    tenures: dict[str, list[Tenure]] = defaultdict(list)
    for user_id, email_hash, derive, summary, completed_at, started_at in (
        await db.execute(
            select(
                ErasureRequest.user_id,
                ErasureRequest.user_email_hash,
                (unrepaired_erasure() | legacy_note_row()).label("derive"),
                ErasureRequest.deleted_data_summary,
                ErasureRequest.completed_at,
                ErasureRequest.started_at,
            ).where(stmt.whereclause)
        )
    ).all():
        # #2302: a legacy note row is keyed by its derived pseudonym too
        tenures[erasure_pseudonym(user_id) if derive else user_id].append(
            Tenure(end=_tenure_end(summary, completed_at, started_at), email_hash=email_hash)
        )
    if request_id is not None:
        stmt = stmt.where(ErasureRequest.id == request_id)
    requests = (await db.execute(stmt)).all()
    erased = await erased_pseudonyms(db)
    audit_subs = await _audit_subs(db, erased)
    # #2359: the plaintext of every address the run may need, in one pass
    subject_ids = set(tenures) | {
        sub
        for pseudonym, sub in [*audit_subs.orphans.items(), *audit_subs.in_use.items()]
        if pseudonym in tenures
    }
    recovery = await recover_addresses(
        db,
        hmacs=await email_sync_hashes(db, subject_ids),
        sha256s={t.email_hash for ts in tenures.values() for t in ts if t.email_hash},
    )
    await db.rollback()
    if request_id is not None and not requests:
        raise LookupError(
            f"{request_id} is not an erasure request that ran in Postgres "
            "(complete, or failed after its Postgres step)"
        )

    result = ReprocessResult(dry_run=dry_run, scanned=len(requests))
    # the one request that pseudonymizes an account id's rows takes every
    # address of the account (``email_holdings``, by the subject's pseudonym)
    audited: set[str] = set()
    # #2298: the pseudonyms whose mention sweep ran (once per run). #2359: an
    # address two erased accounts held needs no per-address dedupe any more —
    # each one's windows leave out the other's holding (``excluded``)
    swept: set[str] = set()

    async def holdings_of(pseudonym: str) -> EmailHoldings | None:
        """The pseudonym's holdings the first time it is processed."""
        if pseudonym in swept:
            return None
        swept.add(pseudonym)
        # the raw sub, also one in use again: its sync rows and first audit
        # row up to the erasure are this account's (tenures are bounded by
        # time; the re-created account's later rows are another holder's)
        ids = [pseudonym]
        sub = audit_subs.orphans.get(pseudonym) or audit_subs.in_use.get(pseudonym)
        if sub is not None:
            ids.append(sub)
        return await email_holdings(
            db, ids, now=utcnow(), tenures=tenures[pseudonym], recovery=recovery
        )

    def record(change: RequestChange) -> None:
        if change.audit_mentions_kept:
            result.mentions_kept.append((change.request_id, change.audit_mentions_kept))
        if change.audit_addresses_not_recovered:
            result.not_recovered.append((change.request_id, change.audit_addresses_not_recovered))
        if change.details_keys_ambiguous:
            result.ambiguous.append((change.request_id, change.details_keys_ambiguous))
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
                holdings=await holdings_of(pseudonym),
                # #2302: the repair writes the marker in this transaction;
                # the payload step's backstop runs with it, like the others'
                resync_postgres=(
                    None
                    if payloads_resynced(request.deleted_data_summary)
                    else dict(postgres_section(request.deleted_data_summary) or {})
                ),
            )
        else:
            # Both checks before the user_id is used as a subject: only a row
            # an erasure run finalized stores the pseudonym (``finalized_erasure``).
            if not request.finalized:
                # #2286: a note row the sweep pseudonymized stores a real
                # pseudonym, so the orphan lookup is sound for it: audit rows
                # a run interrupted on v0.124.x (before its audit-log step)
                # left raw are found and scrubbed, and the rows naming the
                # subject (#2298) — nothing else is done. #2302: a legacy note
                # row (before v0.125.0) stores the raw sub: it gets the
                # pseudonym derived from it (what ``erased_pseudonyms``
                # derives), and the same scrub by that pseudonym, in one
                # transaction. The sub is never looked up as a subject.
                legacy = not request.note_pseudonymized
                pseudonym = erasure_pseudonym(request.user_id) if legacy else request.user_id
                change = await _reprocess_one(
                    db,
                    request.id,
                    pseudonym,
                    request.completed_at,
                    existed_at,
                    dry_run,
                    erased,
                    audit_account_id=audit_account_id(request.id, pseudonym),
                    audit_only=True,
                    holdings=await holdings_of(pseudonym),
                    note_account_id=request.user_id if legacy else None,
                )
                record(change)
                result.skipped.append((request.id, SKIPPED_NOT_RUN))
                continue
            if not is_pseudonym(request.user_id):
                result.skipped.append((request.id, SKIPPED_NOT_PSEUDONYMIZED))
                continue
            # #2302: the backstop of the payload step of a run that failed or
            # was interrupted after its Postgres commit without recording it
            # (the sweep's re-drive, or a failure inside the step itself)
            summary = request.deleted_data_summary or {}
            resync = (
                request.failure_reason is not None
                and request.failure_reason.startswith(FAILURE_AFTER_POSTGRES_COMMIT)
                and not payloads_resynced(summary)
            )
            change = await _reprocess_one(
                db,
                request.id,
                request.user_id,
                request.completed_at,
                existed_at,
                dry_run,
                erased,
                audit_account_id=audit_account_id(request.id, request.user_id),
                holdings=await holdings_of(request.user_id),
                resync_postgres=dict(summary["postgres"]) if resync else None,
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
        if c.details_keys_reverted:
            print(f"would revert details keys  {c.request_id}  {_keys_reverted(c)}")
    _print_ambiguous(result)
    for c in result.changed:
        if c.repaired:
            # The plan always runs as a dry run (``_oneshot.run_plan_apply``).
            print(f"would repair  {c.request_id}  {REPAIRED}")
    for c in result.changed:
        if c.note_repaired:
            print(f"would pseudonymize note row  {c.request_id}  {NOTE_REPAIRED}")
    for c in result.changed:
        if c.payload_resync is not None:
            print(f"would resync payloads  {c.request_id}  {c.payload_resync} memory(ies)")
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
    _print_left_raw(result)
    for request_id, reason in result.skipped:
        print(f"skipped  {request_id}  {reason}")
    verb = "would change" if result.dry_run else "changed"
    print(
        f"{result.scanned} completed erasure(s): {verb} {len(result.changed)}, "
        f"skipped {len(result.skipped)}"
    )


def _print_applied(result: ReprocessResult) -> None:
    for c in result.changed:
        if c.details_keys_reverted:
            print(f"reverted details keys  {c.request_id}  {_keys_reverted(c)}")
    _print_ambiguous(result)
    _print_left_raw(result)
    for c in result.changed:
        if c.repaired:
            print(f"repaired  {c.request_id}  {REPAIRED}")
    for c in result.changed:
        if c.note_repaired:
            print(f"pseudonymized note row  {c.request_id}  {NOTE_REPAIRED}")
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
        if c.details_restored or c.details_restored_unavailable or c.payload_resync is not None:
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


def _keys_reverted(change: RequestChange) -> str:
    memories = change.details_restored + change.details_restored_unavailable
    return f"{change.details_keys_reverted} key(s) on {memories} memory(ies)"


def _print_ambiguous(result: ReprocessResult) -> None:
    """#2275: counts only — never a key name, a value or a pseudonym."""
    for request_id, keys in result.ambiguous:
        print(
            f"details keys ambiguous  {request_id}  {keys} key(s) kept "
            "(stripped history, see runbook)"
        )


def _print_left_raw(result: ReprocessResult) -> None:
    """#2359: counts only — never an address, a hash or an account id."""
    for request_id, rows in result.mentions_kept:
        print(f"audit mentions  {request_id}  {rows} {MENTIONS_KEPT}")
    for request_id, holdings in result.not_recovered:
        print(f"audit addresses  {request_id}  {holdings} {NOT_RECOVERED}")


def _audit_rows(change: RequestChange) -> str:
    return f"{change.audit_logs_pseudonymized} row(s)"


def _summary(result: ReprocessResult) -> str:
    restored = sum(c.details_restored for c in result.changed)
    unavailable = sum(c.details_restored_unavailable for c in result.changed)
    stripped = sum(c.details_stripped for c in result.changed)
    marked = sum(c.scrub_marked for c in result.changed)
    repaired = sum(c.repaired for c in result.changed)
    audited = sum(c.audit_logs_pseudonymized for c in result.changed)
    mentions = sum(c.audit_mentions_pseudonymized for c in result.changed)
    cleared = sum(c.audit_metadata_cleared for c in result.changed)
    notes = sum(c.note_repaired for c in result.changed)
    resynced = sum(c.payload_resync is not None for c in result.changed)
    keys = sum(c.details_keys_reverted for c in result.changed)
    ambiguous = sum(n for _, n in result.ambiguous)
    return (
        (
            f"{len(result.changed)} erasure request(s): {restored} memory details restored, "
            f"{unavailable} cleared (unavailable), {stripped} revision prior(s) stripped, "
            f"{marked} marked scrubbed"
        )
        + (f", {keys} details key(s) reverted" if keys else "")
        + (f", {ambiguous} ambiguous key(s) kept" if ambiguous else "")
        + (f", {repaired} request row(s) repaired" if repaired else "")
        + (f", {audited} audit row(s) pseudonymized" if audited else "")
        + (f", {mentions} audit mention(s) pseudonymized" if mentions else "")
        + (f", {cleared} audit row(s) request metadata cleared" if cleared else "")
        + (f", {notes} note row(s) pseudonymized" if notes else "")
        + (f", {resynced} request(s) payloads resynced" if resynced else "")
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
        help=(
            "every completed erasure request (note rows from before v0.125.0 are "
            "pseudonymized), and every request that failed after its Postgres step"
        ),
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
