"""Re-key the stored erasure pseudonyms after an ``AUDIT_PSEUDO_SALT`` rotation (#2598).

An erasure stores its subject as ``sha256(AUDIT_PSEUDO_SALT + value)``: the
account id, and on the audit trail the subject's email addresses. Rotating
the salt changes only the pseudonyms written afterwards, so whoever holds the
old salt can still hash a candidate account id or address and find the rows
of an account erased before the rotation. This command re-keys them: every
stored pseudonym ``p`` becomes ``sha256(<current salt> + p)``. The old salt is
not needed, and the new value cannot be derived without the current one. Runs
where the API runs (same env: DATABASE_URL, QDRANT_*), e.g. inside the API
container::

    python -m src.cli.rekey_erasure_pseudonyms                        # plan (default, read-only)
    python -m src.cli.rekey_erasure_pseudonyms --apply --yes          # re-key
    python -m src.cli.rekey_erasure_pseudonyms --stamp-current        # plan of the stamp
    python -m src.cli.rekey_erasure_pseudonyms --stamp-current --apply

Which requests: every ``erasure_requests`` row that stores its subject's
pseudonym (a finalized erasure, one that failed after its Postgres step, a
note row the sweep pseudonymized). Since v0.141.0 every writer of such a row
stamps the fingerprint of the salt it used (``pseudonym_salt`` in
``deleted_data_summary``, ``erasure_details.pseudonym_salt_fingerprint``). A
row under the current salt is skipped; a row under another salt, or with no
fingerprint (written before v0.141.0), is re-keyed and stamped — a second
apply changes nothing and never hashes a hash.

``--stamp-current`` is for a deployment that has NOT rotated the salt since
those rows were written: it writes the current fingerprint on every row that
has none and changes nothing else, so that a later rotation re-keys exactly
those rows. The order: upgrade, ``--stamp-current --apply``, rotate
``AUDIT_PSEUDO_SALT``, restart every process, re-key ``--apply``. A deployment
whose salt was rotated already runs the re-key directly; the plan says how
many rows carry no fingerprint (an unrotated row re-keyed under its own salt
loses nothing but the derivation from the account id, which a rotation drops
anyway).

What one apply rewrites, in ONE Postgres transaction, by exact value: the
pseudonyms of the requests to re-key (their ``user_id``) in every column an
erasure writes one to (``SUB_COLUMNS``: ``erasure_requests.user_id`` /
``initiated_by``, ``memories.user_id``, ``memory_revisions.actor_user_id``,
``vector_erasure_jobs.subject_pseudonym``, ``secret_access_log``, the
authorship columns of surviving rows, ...) and on ``audit_logs`` (``user_id``,
``user_email`` — the raw-sub fallback —, ``resource = 'user:'||p``, the
``user_pseudonym:<first 16>`` resource of the erasure's own rows, the
subject-identifier keys of ``user_metadata``); and the subjects' email
pseudonyms: the 64-hex ``user_email`` and ``user:<64 hex>`` values on the
subjects' own audit rows, rewritten wherever ``user_email`` or a whole
``user:`` resource carries them. On the subjects' own rows a pseudonym inside
any other ``resource`` is rewritten too (the erasure replaced every
occurrence there), and so is the leading part of one the erasure cut at the
column's length. One map of old to new values for every request, so a value
two subjects share is re-keyed once. The rows are locked in id order, the
lock order of the erasure (contexts, memories, revisions, ..., audit rows,
request rows), and one run at a time (a transaction advisory lock).

After the commit, the vector payload ``user_id`` of the re-keyed subjects'
memories (their summary and chunk points, on whichever store is active) is
set to the new value, compare-and-set on the old one. A failed patch is
logged (memory id, error class) and counted: the request keeps
``pseudonym_rekey_payloads`` in its summary and the next apply patches its
memories again — a point whose author's pseudonym under the current salt is
the memory's author still carries the old value. The plan makes no
vector-store call.

Limits (counts only, never a value):

- Rows that store the account id instead of the pseudonym (a request that
  failed after its Postgres step before v0.124.0, a note row from before
  v0.125.0, a row finalized before v0.58.1) cannot be re-keyed: their
  pseudonym cannot be derived without the old salt. Run ``reprocess_erasures
  --all --apply`` under the salt the erasures used before the rotation.
- An email pseudonym that only other actors' rows carry (no row of the
  subject's own names the address, and it is not the address the
  ``account_erasure`` row recorded) is not found and stays under the old salt.
  It cannot be counted.
- An email pseudonym of a request already under the current salt is never
  re-keyed, even where a request to re-key carries it too (counted), nor a
  value that is a live account's id (counted).
- Run it while no erasure, re-run (``reprocess_erasures``,
  ``cleanup_erased_vectors``) or sweep runs: they derive pseudonyms with the
  salt of their own process.

After the re-key, a pseudonym derived from an account id under the new salt
does not equal the stored one, by design: the code that derives one and
compares (the write guard of a stale session, the restore of a pre-erasure
snapshot, the vector-erasure drain's raw-author points) no longer recognizes
the account id of a re-keyed subject — as after any rotation. A value still
under the old salt is recognized as that subject's raw value by those paths
(its pseudonym under the current salt is the stored one).

Exit codes: 0 ok · 1 error (nothing of the transaction is written) · 2 usage
(``--yes`` without ``--apply``).
"""

from __future__ import annotations

import argparse
import asyncio
import bisect
import re
import sys
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from uuid import UUID

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import (  # noqa: E402
    BigInteger,
    ColumnElement,
    Text,
    any_,
    false,
    func,
    or_,
    select,
    text,
    type_coerce,
    union,
    update,
)
from sqlalchemy.dialects.postgresql import ARRAY  # noqa: E402
from sqlalchemy.engine import CursorResult  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402
from sqlalchemy.orm import InstrumentedAttribute, aliased  # noqa: E402

from cli._oneshot import add_log_level_argument, configure_logging, run_plan_apply  # noqa: E402
from config.settings import is_placeholder_secret  # noqa: E402
from db.qdrant import (  # noqa: E402
    erase_point_author,
    erasure_scope_points,
    list_memory_collections,
)
from models.agent import Agent, AgentContextBinding  # noqa: E402
from models.analysis import MemoryAnalysis  # noqa: E402
from models.auth import (  # noqa: E402
    AuditLog,
    Context,
    ContextMember,
    ExternalAPIKey,
    PlanChange,
    User,
    WorkspaceInvitation,
    WorkspaceMember,
)
from models.config import ConfigOverride  # noqa: E402
from models.erasure import (  # noqa: E402
    NOTE_PSEUDONYM_REKEY_PAYLOADS_KEY,
    NOTE_PSEUDONYM_SALT_KEY,
    ErasureRequest,
)
from models.file_objects import FileObject  # noqa: E402
from models.memory import (  # noqa: E402
    Memory,
    MemoryRevision,
    MemorySessionRollback,
    NeuralMemoryEdge,
)
from models.memory_access_event import MemoryAccessEvent  # noqa: E402
from models.resource import (  # noqa: E402
    Resource,
    ResourceToken,
    WorkspaceAddon,
    WorkspaceConnector,
)
from models.retrieval_feedback import RetrievalFeedback  # noqa: E402
from models.secrets import (  # noqa: E402
    RecipientPubkey,
    Secret,
    SecretAccessLog,
    SecretGrant,
    SecretVersion,
)
from models.sleep import SleepReport  # noqa: E402
from models.vector_erasure import VectorErasureJob  # noqa: E402
from models.worker_app import WorkerAppIdentity  # noqa: E402
from services.erasure_details import (  # noqa: E402
    AUDIT_SUBJECT_METADATA_KEYS,
    ERASURE_RESOURCE_PREFIX,
    ERASURE_WRITER_ACTIONS,
    USER_RESOURCE_PREFIX,
    current_pseudonym_salt,
    erasure_pseudonym,
    is_pseudonym,
    legacy_note_row,
    merged_request_summary,
    pseudonym_salt_fingerprint,
    stores_pseudonym,
    unrepaired_erasure,
)
from utils.hashing import sha256_hex  # noqa: E402
from utils.logger import get_logger  # noqa: E402

logger = get_logger(__name__)

# Every column an erasure writes the subject's sub pseudonym to, apart from
# ``audit_logs`` (rewritten per row, below) and ``erasure_requests`` (last:
# the erasure locks its request row last). In the erasure's lock order:
# contexts before memories, memories before their revisions
# (``AccountErasureService._delete_postgres``: ``_pseudonymize_field`` and its
# authorship sweep, ``_scrub_surviving_memories``,
# ``pseudonymize_revision_actor``, ``_erase_session_provenance``,
# ``_erase_memory_access_events``, ``_erase_secret_access_log``,
# ``capture_vector_erasure``). ``tests/cli/test_rekey_erasure_pseudonyms.py``
# checks it against the erasure's source.
SUB_COLUMNS: tuple[InstrumentedAttribute[Any], ...] = (
    Context.created_by,
    Memory.user_id,
    MemoryRevision.actor_user_id,
    MemorySessionRollback.principal_user_id,
    MemorySessionRollback.requested_by,
    MemoryAccessEvent.user_id,
    NeuralMemoryEdge.user_id,
    MemoryAnalysis.triggered_by,
    RetrievalFeedback.user_id,
    SleepReport.user_id,
    PlanChange.changed_by,
    Agent.owner_user_id,
    AgentContextBinding.created_by,
    WorkerAppIdentity.created_by,
    WorkerAppIdentity.updated_by,
    ConfigOverride.updated_by,
    WorkspaceConnector.created_by,
    Resource.created_by,
    ResourceToken.created_by,
    WorkspaceAddon.created_by,
    FileObject.created_by,
    Secret.created_by,
    SecretVersion.created_by,
    RecipientPubkey.created_by,
    RecipientPubkey.attested_by,
    RecipientPubkey.identity_id,
    SecretGrant.granted_by,
    ExternalAPIKey.updated_by,
    ContextMember.invited_by,
    WorkspaceMember.invited_by,
    WorkspaceInvitation.accepted_by,
    SecretAccessLog.actor_user_id,
    SecretAccessLog.recipient_identity,
    VectorErasureJob.subject_pseudonym,
)
REQUEST_COLUMNS: tuple[InstrumentedAttribute[Any], ...] = (
    ErasureRequest.user_id,
    ErasureRequest.initiated_by,
)
# ``user_metadata`` keys that hold a subject's sub (``pseudonymize_audit_trail``),
# and the ``account_erasure`` row's initiator (``pseudonymize_erasure_audit_initiator``)
METADATA_KEYS = (*AUDIT_SUBJECT_METADATA_KEYS, "initiated_by")
AUDIT_COLUMNS = ("user_id", "user_email", "resource", "user_metadata")
# One re-key at a time: a second apply waits, then finds every row stamped.
_REKEY_LOCK = text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))").bindparams(
    key="erasure_pseudonym_rekey"
)
_BATCH = 500
_PSEUDONYM_LENGTH = 64
# The shortest leading part of a cut pseudonym that is rewritten (#2598): a
# shorter one is left (``_cut_value``).
_CUT_MIN = 8
_HEX_RUN = re.compile(r"[0-9a-f]{64,}")
_TRAILING_HEX = re.compile(r"[0-9a-f]+$")

UNMARKED_HINT = (
    "carry no salt fingerprint (written before v0.141.0): if AUDIT_PSEUDO_SALT was not "
    "rotated since, run --stamp-current --apply instead of re-keying them"
)
ACCOUNT_ID_ROWS = (
    "store the account id, not a pseudonym: not re-keyable (run reprocess_erasures "
    "--all --apply under the salt they were erased with, before the rotation)"
)
PLACEHOLDER_SALT = (
    "AUDIT_PSEUDO_SALT is a shipped placeholder: a re-key under it protects nothing; "
    "set a private value, restart, and run again"
)
KNOWN_LIMIT = (
    "known limit: an email pseudonym that only other actors' rows carry is not found "
    "and stays under its salt (it cannot be counted)"
)


@dataclass
class PayloadCounts:
    """The vector payload patch after the commit (an apply only)."""

    memories: int = 0
    points: int = 0
    failed: int = 0
    # requests whose memories all carry the re-keyed author now / still not
    requests_done: int = 0
    requests_pending: int = 0


@dataclass
class RekeyResult:
    """One pass over the erasure requests that store a pseudonym. Counts only."""

    dry_run: bool
    stamp_current: bool = False
    # request rows by the salt they are stamped with
    current: int = 0
    other_salt: int = 0
    unmarked: int = 0
    # request rows that store the account id (not re-keyable)
    account_id_rows: int = 0
    # request rows that get (got) the current fingerprint
    stamped: int = 0
    # ``table.column`` -> rows rewritten (would be)
    columns: dict[str, int] = field(default_factory=dict)
    # values left as they are: an email pseudonym of a request under the
    # current salt (or that request's pseudonym), a live account's id
    kept_current: int = 0
    kept_live: int = 0
    # memories whose points the payload step patches (this run's and earlier
    # failed ones), and requests whose earlier payload step is pending
    payload_memories: int = 0
    payload_pending: int = 0
    placeholder_salt: bool = False
    payload: PayloadCounts | None = None

    @property
    def to_rekey(self) -> int:
        return self.other_salt + self.unmarked

    @property
    def rows(self) -> int:
        return sum(self.columns.values())

    @property
    def changes(self) -> int:
        if self.placeholder_salt:
            return 0
        return self.stamped + self.rows + self.payload_pending


@dataclass(frozen=True)
class _Request:
    id: UUID
    user_id: str
    # None: no fingerprint (written before v0.141.0)
    fingerprint: str | None
    payloads_pending: bool


def _texts(values: Collection[str]) -> Any:
    """``values`` as one ``text[]`` bind (``= ANY(...)``): no parameter limit."""
    return type_coerce(sorted(values), ARRAY(Text))


def _label(column: InstrumentedAttribute[Any]) -> str:
    return f"{column.class_.__tablename__}.{column.key}"


async def _scope(db: AsyncSession) -> tuple[list[_Request], int]:
    """The request rows that store a pseudonym, and how many store the account id."""
    rows = (
        await db.execute(
            select(
                ErasureRequest.id,
                ErasureRequest.user_id,
                ErasureRequest.deleted_data_summary,
                stores_pseudonym().label("stores"),
            )
            .where(stores_pseudonym() | unrepaired_erasure() | legacy_note_row())
            .order_by(ErasureRequest.id)
        )
    ).all()
    requests: list[_Request] = []
    account_ids = 0
    for request_id, user_id, summary, stores in rows:
        if not stores or not is_pseudonym(user_id):
            account_ids += 1
            continue
        note = summary if isinstance(summary, dict) else {}
        fingerprint = note.get(NOTE_PSEUDONYM_SALT_KEY)
        requests.append(
            _Request(
                request_id,
                user_id,
                fingerprint if isinstance(fingerprint, str) else None,
                bool(note.get(NOTE_PSEUDONYM_REKEY_PAYLOADS_KEY)),
            )
        )
    return requests, account_ids


async def _email_pseudonyms(
    db: AsyncSession, subs: Collection[str], current: Collection[str]
) -> tuple[set[str], int]:
    """The email pseudonyms of the subjects to re-key, and how many found
    values are left as a request under the current salt's.

    Found on the subjects' own audit rows (``user_id`` = their pseudonym): a
    64-hex ``user_email`` (the subject acting, and the ``account_erasure``
    row's) and a ``user:<64 hex>`` resource. A value of a request under the
    current salt is left out: its pseudonym, and the email pseudonym its
    erasure rows recorded — an erasure under the current salt rewrote that
    value on the subjects' rows too (they acted on each other). Never a sub
    pseudonym of the subjects themselves (re-keyed as such)."""
    if not subs:
        return set(), 0
    found: set[str] = set()
    rows = await db.stream(
        select(AuditLog.user_email, AuditLog.resource).where(
            AuditLog.user_id == any_(_texts(subs))
        ),
        execution_options={"yield_per": _BATCH},
    )
    async for email, resource in rows:
        if isinstance(email, str) and is_pseudonym(email):
            found.add(email)
        if isinstance(resource, str) and resource.startswith(USER_RESOURCE_PREFIX):
            named = resource[len(USER_RESOURCE_PREFIX) :]
            if is_pseudonym(named):
                found.add(named)
    found -= set(subs)
    left = set(current)
    if current:
        left.update(
            email
            for email in (
                await db.execute(
                    select(AuditLog.user_email).where(
                        AuditLog.user_id == any_(_texts(current)),
                        AuditLog.action.in_(ERASURE_WRITER_ACTIONS),
                    )
                )
            ).scalars()
            if email
        )
    kept = found & left
    return found - kept, len(kept)


async def _live_ids(db: AsyncSession, values: Collection[str]) -> set[str]:
    """Values a live account uses as its id: never rewritten."""
    if not values:
        return set()
    return set(
        (
            await db.execute(select(User.user_id).where(User.user_id == any_(_texts(values))))
        ).scalars()
    )


async def _memory_counts(db: AsyncSession, authors: Collection[str]) -> int:
    if not authors:
        return 0
    return (
        await db.execute(
            select(func.count(Memory.id)).where(Memory.user_id == any_(_texts(authors)))
        )
    ).scalar_one()


async def _authors_with_memories(db: AsyncSession, authors: Collection[str]) -> set[str]:
    if not authors:
        return set()
    return set(
        (
            await db.execute(
                select(Memory.user_id).where(Memory.user_id == any_(_texts(authors))).distinct()
            )
        ).scalars()
    )


async def _lock_memories(db: AsyncSession, subs: Collection[str]) -> None:
    """The subjects' memories — authored, or with a revision of theirs — in id
    order, before any memory or revision row is rewritten
    (``lock_subject_memories``, for every subject at once)."""
    values = _texts(subs)
    scope = union(
        select(Memory.id).where(Memory.user_id == any_(values)),
        select(MemoryRevision.memory_id).where(MemoryRevision.actor_user_id == any_(values)),
    )
    memory = aliased(Memory)
    await db.execute(
        select(memory.id).where(memory.id.in_(scope)).order_by(memory.id).with_for_update()
    )


async def _rewrite_column(
    db: AsyncSession,
    column: InstrumentedAttribute[Any],
    mapping: Mapping[str, str],
    *,
    dry_run: bool,
) -> int:
    """Rewrite ``column`` by exact value (``mapping``), its rows locked in id
    order first. Returns the rowcount (the dry run's: how many match)."""
    if not mapping:
        return 0
    model: Any = column.class_
    olds = _texts(mapping)
    if dry_run:
        return (
            await db.execute(select(func.count()).select_from(model).where(column == any_(olds)))
        ).scalar_one()
    keys = sorted(mapping)
    pairs = select(
        type_coerce(func.unnest(type_coerce(keys, ARRAY(Text))), Text).label("old"),
        type_coerce(func.unnest(type_coerce([mapping[k] for k in keys], ARRAY(Text))), Text).label(
            "new"
        ),
    ).subquery("rekeyed")
    row = aliased(model)
    locked = (
        select(row.id)
        .where(getattr(row, column.key) == any_(olds))
        .order_by(row.id)
        .with_for_update()
    )
    result = await db.execute(
        update(model)
        .where(model.id.in_(locked), column == pairs.c.old)
        .values({column: pairs.c.new})
        .execution_options(synchronize_session=False)
    )
    return cast(CursorResult[Any], result).rowcount or 0


def _cut_value(run: str, keys: Sequence[str]) -> str | None:
    """The one stored value ``run`` is the leading part of, else None."""
    i = bisect.bisect_left(keys, run)
    matches = []
    while i < len(keys) and keys[i].startswith(run) and len(matches) < 2:
        matches.append(keys[i])
        i += 1
    return matches[0] if len(matches) == 1 else None


def rekeyed_resource(
    resource: str | None,
    user_id: str | None,
    subs: Mapping[str, str],
    values: Mapping[str, str],
    keys: Sequence[str],
    length: int,
) -> str | None:
    """``resource`` of one audit row with the stored pseudonyms re-keyed.

    The places the erasure put them (``pseudonymize_audit_logs``,
    ``pseudonymize_audit_mentions``, ``_write_audit_log``): a whole ``user:``
    value on any row; on the subject's own rows (``user_id`` in ``subs``) the
    ``user_pseudonym:<first 16>`` resource, and any occurrence in any other
    resource, including the leading part of one the erasure cut at the
    column's ``length`` (at least ``_CUT_MIN`` digits, of exactly one stored
    value). Another actor's row is never matched by substring.

    Args:
        subs: The sub pseudonyms to re-key, old -> new.
        values: Every value to re-key (``subs`` and the email pseudonyms).
        keys: ``values``' keys, sorted (the cut lookup).
    """
    if not resource:
        return resource
    if resource.startswith(USER_RESOURCE_PREFIX):
        named = resource[len(USER_RESOURCE_PREFIX) :]
        return f"{USER_RESOURCE_PREFIX}{values[named]}" if named in values else resource
    if user_id is None or user_id not in subs:
        return resource
    if resource == f"{ERASURE_RESOURCE_PREFIX}{user_id[:16]}":
        return f"{ERASURE_RESOURCE_PREFIX}{subs[user_id][:16]}"
    parts: list[str] = []
    done = 0
    for run in _HEX_RUN.finditer(resource):
        pos = run.start()
        while pos + _PSEUDONYM_LENGTH <= run.end():
            window = resource[pos : pos + _PSEUDONYM_LENGTH]
            if window in values:
                parts += [resource[done:pos], values[window]]
                pos += _PSEUDONYM_LENGTH
                done = pos
            else:
                pos += 1
    if len(resource) >= length and (tail := _TRAILING_HEX.search(resource, done)) is not None:
        run = tail.group()
        if _CUT_MIN <= len(run) < _PSEUDONYM_LENGTH and (value := _cut_value(run, keys)):
            parts += [resource[done : tail.start()], values[value][: len(run)]]
            done = len(resource)
    parts.append(resource[done:])
    return "".join(parts)[:length]


def _audit_change(
    row: Any, subs: Mapping[str, str], values: Mapping[str, str], keys: Sequence[str], length: int
) -> dict[str, Any]:
    """The columns of one audit row that change, with their new values."""
    change: dict[str, Any] = {}
    if row.user_id in subs:
        change["user_id"] = subs[row.user_id]
    if row.user_email in values:
        change["user_email"] = values[row.user_email]
    resource = rekeyed_resource(row.resource, row.user_id, subs, values, keys, length)
    if resource != row.resource:
        change["resource"] = resource
    metadata = row.user_metadata
    if isinstance(metadata, dict):
        replaced = {
            key: subs[value]
            for key in METADATA_KEYS
            if isinstance(value := metadata.get(key), str) and value in subs
        }
        if replaced:
            # the column is ``json``: the whole value, the other keys as read
            change["user_metadata"] = {**metadata, **replaced}
    return change


async def _metadata_ids(db: AsyncSession, subs: Mapping[str, str]) -> set[int]:
    """Rows whose ``user_metadata`` names a subject. Whole JSON, matched in
    Python (#2348: SQL extraction rejects an escaped U+0000 of legacy JSON)."""
    if not subs:
        return set()
    rows = await db.stream(
        select(AuditLog.id, AuditLog.user_metadata).where(AuditLog.user_metadata.is_not(None)),
        execution_options={"yield_per": _BATCH},
    )
    matched: set[int] = set()
    async for row_id, metadata in rows:
        if isinstance(metadata, dict) and any(
            isinstance(value := metadata.get(key), str) and value in subs for key in METADATA_KEYS
        ):
            matched.add(row_id)
    return matched


async def _rewrite_audit_logs(
    db: AsyncSession, subs: Mapping[str, str], emails: Mapping[str, str], *, dry_run: bool
) -> dict[str, int]:
    """Re-key the audit rows that carry a value: locked in id order (the apply),
    then read and rewritten in batches, only the columns that change.

    Returns:
        Rows changed per column (``AUDIT_COLUMNS``).
    """
    values = {**emails, **subs}
    counts: dict[str, int] = dict.fromkeys(AUDIT_COLUMNS, 0)
    if not values:
        return counts
    metadata_ids = await _metadata_ids(db, subs)
    resources = [f"{USER_RESOURCE_PREFIX}{value}" for value in values]

    def candidates(row: Any) -> ColumnElement[bool]:
        return or_(
            row.user_id == any_(_texts(subs)) if subs else false(),
            row.user_email == any_(_texts(values)),
            row.resource == any_(_texts(resources)),
            row.id == any_(type_coerce(sorted(metadata_ids), ARRAY(BigInteger)))
            if metadata_ids
            else false(),
        )

    row = aliased(AuditLog)
    stmt = select(row.id).where(candidates(row)).order_by(row.id)
    if not dry_run:
        stmt = stmt.with_for_update()
    ids = list((await db.execute(stmt)).scalars())
    keys = sorted(values)
    length = cast(int, AuditLog.resource.type.length)
    for offset in range(0, len(ids), _BATCH):
        rows = await db.execute(
            select(
                AuditLog.id,
                AuditLog.user_id,
                AuditLog.user_email,
                AuditLog.resource,
                AuditLog.user_metadata,
            )
            .where(AuditLog.id.in_(ids[offset : offset + _BATCH]))
            .order_by(AuditLog.id)
        )
        batch: list[dict[str, Any]] = []
        for audit in rows:
            change = _audit_change(audit, subs, values, keys, length)
            if not change:
                continue
            for column in change:
                counts[column] += 1
            batch.append({"id": audit.id, **change})
        if batch and not dry_run:
            # ORM bulk UPDATE by primary key: only the keys each dict names
            await db.execute(update(AuditLog), batch)
    return counts


async def _stamp(
    db: AsyncSession, ids: Collection[UUID], note: dict[str, Any], *, dry_run: bool
) -> int:
    """Merge ``note`` into the summary of the request rows ``ids``."""
    if not ids:
        return 0
    if dry_run:
        return len(ids)
    result = await db.execute(
        update(ErasureRequest)
        .where(ErasureRequest.id.in_(sorted(ids)))
        .values(deleted_data_summary=merged_request_summary(note))
        .execution_options(synchronize_session=False)
    )
    return cast(CursorResult[Any], result).rowcount or 0


async def _lock_requests(db: AsyncSession, ids: Collection[UUID], subs: Collection[str]) -> None:
    """The request rows to stamp and every row carrying a sub to re-key, in id order."""
    arms: list[ColumnElement[bool]] = [ErasureRequest.id.in_(sorted(ids))]
    if subs:
        arms += [
            ErasureRequest.user_id == any_(_texts(subs)),
            ErasureRequest.initiated_by == any_(_texts(subs)),
        ]
    await db.execute(
        select(ErasureRequest.id).where(or_(*arms)).order_by(ErasureRequest.id).with_for_update()
    )


async def _rekey(db: AsyncSession, *, dry_run: bool, stamp_current: bool) -> RekeyResult:
    """The Postgres part, in the caller's transaction (see the module docstring)."""
    salt = current_pseudonym_salt()
    fingerprint = pseudonym_salt_fingerprint(salt)
    result = RekeyResult(dry_run=dry_run, stamp_current=stamp_current)
    if not dry_run:
        await db.execute(_REKEY_LOCK)
    requests, result.account_id_rows = await _scope(db)
    current = [r for r in requests if r.fingerprint == fingerprint]
    to_rekey = [r for r in requests if r.fingerprint != fingerprint]
    result.current = len(current)
    result.unmarked = sum(r.fingerprint is None for r in to_rekey)
    result.other_salt = len(to_rekey) - result.unmarked
    salt_note: dict[str, Any] = {NOTE_PSEUDONYM_SALT_KEY: fingerprint}
    if stamp_current:
        unmarked = [r.id for r in to_rekey if r.fingerprint is None]
        if not dry_run and unmarked:
            await _lock_requests(db, unmarked, ())
        result.stamped = await _stamp(db, unmarked, salt_note, dry_run=dry_run)
        return result
    if is_placeholder_secret(salt):
        result.placeholder_salt = True
        return result
    pending = {r.user_id for r in current if r.payloads_pending}
    result.payload_pending = sum(r.payloads_pending for r in current)

    current_subs = {r.user_id for r in current}
    # a pseudonym a request under the current salt stores is that salt's
    # (the same account erased twice): its other requests are only stamped
    subs = {r.user_id for r in to_rekey} - current_subs
    emails, result.kept_current = await _email_pseudonyms(db, subs, current_subs)
    result.kept_current += len({r.user_id for r in to_rekey} & current_subs)
    live = await _live_ids(db, subs | emails)
    result.kept_live = len(live)
    subs -= live
    emails -= live
    sub_map = {p: erasure_pseudonym(p) for p in sorted(subs)}
    email_map = {e: erasure_pseudonym(e) for e in sorted(emails)}
    with_memories = await _authors_with_memories(db, sub_map)
    result.payload_memories = await _memory_counts(db, set(sub_map) | pending)

    for column in SUB_COLUMNS:
        if column is Memory.user_id and sub_map and not dry_run:
            await _lock_memories(db, sub_map)
        if count := await _rewrite_column(db, column, sub_map, dry_run=dry_run):
            result.columns[_label(column)] = count
    audit = await _rewrite_audit_logs(db, sub_map, email_map, dry_run=dry_run)
    result.columns.update({f"audit_logs.{k}": v for k, v in audit.items() if v})
    ids = [r.id for r in to_rekey]
    if not dry_run and ids:
        await _lock_requests(db, ids, sub_map)
    for column in REQUEST_COLUMNS:
        if count := await _rewrite_column(db, column, sub_map, dry_run=dry_run):
            result.columns[_label(column)] = count
    # the payload step's mark on the requests whose subject has memories
    patched = [r.id for r in to_rekey if r.user_id in with_memories]
    result.stamped = await _stamp(
        db, patched, {**salt_note, NOTE_PSEUDONYM_REKEY_PAYLOADS_KEY: True}, dry_run=dry_run
    ) + await _stamp(
        db, [r.id for r in to_rekey if r.id not in patched], salt_note, dry_run=dry_run
    )
    return result


async def _patch_payloads(db: AsyncSession) -> PayloadCounts:
    """After the commit: the vector payload ``user_id`` of the memories of every
    request marked ``pseudonym_rekey_payloads`` under the current salt.

    A point of such a memory (summary and chunk points) whose author, re-keyed
    under the current salt, is the memory's author carries the old value: it
    is set to the memory's author, compare-and-set on the old one
    (``erase_point_author``). The test needs no old value, so an earlier
    apply's failures are patched the same way. A request's mark is removed
    once every one of its memories was patched without error; a failure is
    logged with the memory id and the error class and leaves the mark. Never
    raises for the vector store.
    """
    counts = PayloadCounts()
    salt = current_pseudonym_salt()
    fingerprint = pseudonym_salt_fingerprint(salt)
    marked = (
        await db.execute(
            select(ErasureRequest.id, ErasureRequest.user_id)
            .where(
                ErasureRequest.deleted_data_summary.has_key(NOTE_PSEUDONYM_REKEY_PAYLOADS_KEY),
                ErasureRequest.deleted_data_summary[NOTE_PSEUDONYM_SALT_KEY].astext == fingerprint,
            )
            .order_by(ErasureRequest.id)
        )
    ).all()
    await db.rollback()
    by_author: dict[str, list[UUID]] = {}
    for request_id, author in marked:
        by_author.setdefault(author, []).append(request_id)
    if not by_author:
        return counts
    try:
        collections = await list_memory_collections()
    except Exception as exc:
        logger.warning("erasure_rekey_payload_collections_failed", error_class=type(exc).__name__)
        counts.requests_pending = len(marked)
        return counts
    for author, request_ids in sorted(by_author.items()):
        memories = (
            await db.execute(
                select(Memory.id, Memory.summary_embedding_id)
                .where(Memory.user_id == author)
                .order_by(Memory.id)
            )
        ).all()
        await db.rollback()
        failed = 0
        for memory_id, summary_id in memories:
            try:
                for collection in collections:
                    scope = await erasure_scope_points(
                        collection, memory_id, summary_id or memory_id
                    )
                    for point_id, point_author in scope:
                        if point_author != author and sha256_hex(point_author, salt=salt) == author:
                            await erase_point_author(
                                collection, point_id, point_author, replacement=author
                            )
                            counts.points += 1
            except Exception as exc:
                failed += 1
                logger.warning(
                    "erasure_rekey_payload_failed",
                    memory_id=str(memory_id),
                    error_class=type(exc).__name__,
                )
        counts.memories += len(memories)
        counts.failed += failed
        if failed:
            counts.requests_pending += len(request_ids)
            continue
        await db.execute(
            update(ErasureRequest)
            .where(
                ErasureRequest.id.in_(request_ids),
                ErasureRequest.deleted_data_summary.has_key(NOTE_PSEUDONYM_REKEY_PAYLOADS_KEY),
            )
            .values(
                deleted_data_summary=ErasureRequest.deleted_data_summary.delete_path(
                    [NOTE_PSEUDONYM_REKEY_PAYLOADS_KEY]
                )
            )
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        counts.requests_done += len(request_ids)
    return counts


async def rekey_erasure_pseudonyms(
    db: AsyncSession, *, dry_run: bool = True, stamp_current: bool = False
) -> RekeyResult:
    """Re-key the stored erasure pseudonyms to the current salt (#2598).

    Args:
        db: The session. The Postgres part is one transaction, committed (or,
            on a dry run, rolled back) before the payload step.
        dry_run: Count what would change; write and lock nothing, call no
            vector store.
        stamp_current: Only stamp the current salt's fingerprint on the rows
            that have none; rewrite nothing.

    Returns:
        The counts (never a value).
    """
    try:
        result = await _rekey(db, dry_run=dry_run, stamp_current=stamp_current)
        if dry_run:
            await db.rollback()
        else:
            await db.commit()
    except BaseException:
        await db.rollback()
        raise
    if not dry_run:
        logger.info(
            "erasure_pseudonyms_rekeyed",
            stamp_current=stamp_current,
            requests=result.stamped,
            rows=result.rows,
            **{column.replace(".", "_"): count for column, count in result.columns.items()},
        )
        if not stamp_current and not result.placeholder_salt:
            result.payload = await _patch_payloads(db)
            logger.info("erasure_pseudonyms_rekey_payloads", **vars(result.payload))
    return result


def _print_plan(result: RekeyResult) -> None:
    """Counts per column and per request kind; never a value, id or address."""
    total = result.current + result.to_rekey
    print(f"erasure requests that store a pseudonym: {total}")
    print(f"  under the current salt: {result.current}")
    print(f"  under another salt: {result.other_salt}")
    print(f"  no salt fingerprint: {result.unmarked}")
    if result.account_id_rows:
        print(f"requests  {result.account_id_rows}  {ACCOUNT_ID_ROWS}")
    if result.stamp_current:
        verb = "would stamp" if result.dry_run else "stamped"
        print(
            f"{verb} {result.stamped} request(s) with the current salt's fingerprint "
            "(nothing is re-keyed)"
        )
        return
    if result.placeholder_salt:
        print(PLACEHOLDER_SALT)
        return
    if result.unmarked:
        print(f"requests  {result.unmarked}  {UNMARKED_HINT}")
    if result.columns:
        print(f"{'column':48}  rows")
    for column, rows in result.columns.items():
        print(f"{column:48}  {rows}")
    if result.kept_current:
        print(f"kept  {result.kept_current} value(s) of a request already under the current salt")
    if result.kept_live:
        print(f"kept  {result.kept_live} value(s) a live account uses as its id")
    print(
        f"vector payloads: {result.payload_memories} memory(ies) whose points are patched "
        "after the commit"
    )
    if result.payload_pending:
        print(f"payload patch pending from an earlier apply: {result.payload_pending} request(s)")
    print(KNOWN_LIMIT)
    verb = "would re-key" if result.dry_run else "re-keyed"
    print(f"{verb} {result.stamped} request(s), {result.rows} row(s)")


def _print_applied(result: RekeyResult) -> None:
    payload = result.payload
    if payload is None:
        return
    line = (
        f"payloads  patched {payload.points} point(s) on {payload.memories} memory(ies), "
        f"failed {payload.failed}"
    )
    if payload.requests_pending:
        line += (
            f"; {payload.requests_pending} request(s) keep the mark and are patched again by "
            "the next apply (see the erasure_rekey_payload_failed warnings for the memory ids)"
        )
    print(line)


def _summary(result: RekeyResult) -> str:
    if result.stamp_current:
        return (
            f"{result.stamped} erasure request(s) with the current salt's fingerprint "
            "(nothing re-keyed)"
        )
    text_ = (
        f"{result.stamped} erasure request(s) to the current salt "
        f"({result.rows} row(s) in {len(result.columns)} column(s))"
    )
    if result.payload_pending:
        text_ += f", and the pending payload patch of {result.payload_pending} request(s)"
    return text_


async def _main(args: argparse.Namespace) -> int:
    configure_logging(args.log_level)

    async def run(db: AsyncSession, dry_run: bool) -> RekeyResult:
        return await rekey_erasure_pseudonyms(db, dry_run=dry_run, stamp_current=args.stamp_current)

    return await run_plan_apply(
        run=run,
        print_plan=_print_plan,
        changes=lambda result: result.changes,
        noun="erasure request",
        verb="update",
        summary=_summary,
        print_applied=_print_applied,
        apply=args.apply,
        assume_yes=args.yes,
    )


def _parse(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--plan",
        "--dry-run",
        action="store_true",
        help="dry run: print the counts, write nothing (default)",
    )
    mode.add_argument("--apply", action="store_true", help="write the changes")
    parser.add_argument("--yes", action="store_true", help="no confirmation prompt")
    parser.add_argument(
        "--stamp-current",
        action="store_true",
        help=(
            "only stamp the current salt's fingerprint on the requests that have none "
            "(the salt was not rotated since they were written); re-key nothing"
        ),
    )
    add_log_level_argument(parser)
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(_parse())))
