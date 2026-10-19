"""``rollback_session``: undo what one session wrote in one context (#2044).

A session's writes are its ``memory_revisions`` events (``writer='api'``)
under one session id, written by one principal — the caller sub and the
verified agent of the credential (``SessionPrincipal``). The window mode
(#2121) addresses the caller's own API writes in ``[since, until)`` instead,
for clients that have no session id (the stateless MCP transport): the same
principal, never a context owner's override, and at most
``MAX_ROLLBACK_WINDOW`` wide. The rollback groups those events per memory and
lets the memory's own history decide:

=================  ===========================================================
session did        rollback
=================  ===========================================================
created it         forget it (``_soft_delete_chunk``, chunks of
                   ``FORGET_CHUNK``)
updated it         put back the values from before the session's first edit
                   (an edit of its own: version + 1, ``writer='rollback'``)
forgot it          un-forget the tombstone the session's forget left, rebuild
                   its point (``restored``); edits made before the forget are
                   put back too
=================  ===========================================================

A memory is skipped, never half-reverted, when its state is not the one the
session left (``SKIP_CAUSES``): someone else wrote it since the session's
first event (``modified_since``), Sleep merged or archived it
(``merged_by_sleep``), someone else forgot it (``deleted_by_other``), the
resource indexer owns it (``resource_row``), the caller may not write it
(``not_permitted``), an earlier value was erased (``prior_erased``), or there
is nothing left to undo (``already_forgotten`` / ``already_restored``).

Execution holds a per-(context, session id) advisory lock — per (context,
principal) in the window mode — locks the batch's
memory rows in id order, and runs each item (each forget chunk) in a SAVEPOINT
of its own: a failed item leaves nothing behind and the rest still runs. Every
event a call handles — reversed or skipped — gets the rollback row's id in
``undone_by``, so a call that stops at ``MAX_ROLLBACK_EVENTS_PER_CALL`` (or at
``MAX_UNFORGETS_PER_CALL`` un-forgets, each of which re-embeds inside the
call) is continued by the next call, and nothing is ever reversed twice. A
failed item's events stay unhandled: the next call retries it (``partial``).
A rollback whose events are all handled is ``completed``; the next call is
refused (``AlreadyRolledBackError``) unless the session wrote again since.
A window's record is keyed by the window itself: the same ``(since, until)``
continues one record, a different window is a record of its own that only
handles events no rollback has handled yet — overlapping windows, or a
session and a window of the same principal, never reverse an event twice
(the batch's rows are locked and ``undone_by`` is re-read under the lock).
Vector deletes, re-embeds of restored text and the access audit run after
the commit (``finish``).

No MCP types here; ``mcp_server/tools/session_rollback.py`` is the tool.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

from sqlalchemy import exists, func, or_, select, text, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ColumnElement

from auth.workspace_roles import ContextRole
from models.memory import (
    REVISION_KIND_CREATED,
    REVISION_KIND_FORGOTTEN,
    REVISION_KIND_RESTORED,
    REVISION_KIND_UPDATED,
    REVISION_WRITER_API,
    REVISION_WRITER_ROLLBACK,
    SESSION_ROLLBACK_COMPLETED,
    SESSION_ROLLBACK_IN_PROGRESS,
    SESSION_ROLLBACK_PARTIAL,
    SLEEP_TOMBSTONE_DELETED_BY,
    VERSIONED_FIELDS,
    Memory,
    MemoryRevision,
    MemorySessionRollback,
)
from services.memory_versions import (
    _comparable,  # one "is this the same value" rule with the version bump
    prior_erased,
    prior_values,
    record_edit,
    record_revision,
    versioned_snapshot,
)
from services.write_provenance import (
    SessionPrincipal,
    WriteProvenance,
    caller_principal,
    current_provenance,
    principal_predicate,
)
from utils.datetime import to_utc_iso, utcnow
from utils.logger import get_logger

logger = get_logger(__name__)

#: Events one call handles at most; a session with more is continued by the
#: next call. A memory's events are never split across calls (its earliest
#: edit decides what is put back), so one large memory may exceed it alone.
MAX_ROLLBACK_EVENTS_PER_CALL = 500
#: Un-forgets one call performs at most: each rebuilds a vector (an embedding
#: call and its chunk points) inside the call, so a session that forgot many
#: memories is spread over several calls instead of running into the tool
#: timeout and repeating the same batch forever. The rest wait for the next
#: call.
MAX_UNFORGETS_PER_CALL = 100
#: Memories one forget statement (and one SAVEPOINT) covers.
FORGET_CHUNK = 100
#: Plan items a response lists; the counts always cover every item.
MAX_LISTED_ITEMS = 100
#: The widest window the window mode (#2121) rolls back in one record; a
#: longer stretch is split into several windows by the caller.
MAX_ROLLBACK_WINDOW = timedelta(days=7)

ACTION_FORGET = "forget"
ACTION_RESTORE = "restore"
ACTION_UNFORGET = "unforget"
ACTION_SKIP = "skip"

CAUSE_MODIFIED_SINCE = "modified_since"
CAUSE_MERGED_BY_SLEEP = "merged_by_sleep"
CAUSE_DELETED_BY_OTHER = "deleted_by_other"
CAUSE_ALREADY_FORGOTTEN = "already_forgotten"
CAUSE_ALREADY_RESTORED = "already_restored"
CAUSE_RESOURCE_ROW = "resource_row"
CAUSE_PURGED = "purged"
CAUSE_PRIOR_ERASED = "prior_erased"
CAUSE_NOT_PERMITTED = "not_permitted"
SKIP_CAUSES: tuple[str, ...] = (
    CAUSE_MODIFIED_SINCE,
    CAUSE_MERGED_BY_SLEEP,
    CAUSE_DELETED_BY_OTHER,
    CAUSE_ALREADY_FORGOTTEN,
    CAUSE_ALREADY_RESTORED,
    CAUSE_RESOURCE_ROW,
    CAUSE_PURGED,
    CAUSE_PRIOR_ERASED,
    CAUSE_NOT_PERMITTED,
)

_TEXT_FIELDS = frozenset({"summary", "context_summary", "content"})
# ``partial`` is not final: its failed items are retried by the next call.
_FINAL_STATUSES = frozenset({SESSION_ROLLBACK_COMPLETED})
_COUNT_KEYS = (
    "forgotten",
    "restored",
    "updated_restored",
    "skipped",
    "failed",
    "chunks_requeued",
    "points_requeued",
)


class SessionNotFoundError(Exception):
    """No event of this session is addressable by the caller in the context.

    Raised identically for a session id never used and for another
    principal's session: there is no existence oracle.
    """


class NoWritesInWindowError(Exception):
    """The caller's principal has no API write in the window in the context (#2121).

    Says nothing about other principals: the window predicate cannot reach
    their rows.
    """


class AlreadyRolledBackError(Exception):
    """The session's rollback is ``completed`` and it wrote nothing since."""

    def __init__(self, rollback_id: int | None, status: str) -> None:
        super().__init__(status)
        self.rollback_id = rollback_id
        self.status = status


# ---------------------------------------------------------------------------
# Addressing: which session, whose events
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionScope:
    """The events a rollback addresses: one session, or one window (#2121).

    Session mode (``session_id``): ``principal`` is the session's principal;
    None is a context owner's rollback, which covers every principal's
    events under ``session_id`` in the context.

    Window mode (``since`` / ``until``, naive UTC, ``[since, until)``): the
    principal's own events in the window, any session or none. The
    principal is required — there is no owner override in this mode.
    """

    context_id: UUID
    session_id: str | None
    principal: SessionPrincipal | None
    since: datetime | None = None
    until: datetime | None = None

    def __post_init__(self) -> None:
        if (self.session_id is None) == (self.since is None):
            raise ValueError("SessionScope takes exactly one of session_id or since")
        if self.since is None:
            if self.until is not None:
                raise ValueError("until needs since")
            return
        if self.until is None:
            raise ValueError("a window needs until")
        if self.until <= self.since:
            raise ValueError("until must be later than since")
        if self.until - self.since > MAX_ROLLBACK_WINDOW:
            raise ValueError("a window is at most MAX_ROLLBACK_WINDOW wide")
        if self.principal is None:
            raise ValueError("a window is always the caller's own principal")

    @property
    def window(self) -> bool:
        """True for the window mode (#2121)."""
        return self.since is not None


async def context_role(db: AsyncSession, user_id: str, context_id: UUID) -> ContextRole | None:
    """The caller's effective role on the context, or None when they have none."""
    from services.capacity_lock import CapacityPolicy
    from services.permission_service import PermissionService
    from utils.exceptions import AuthorizationError, NotFoundException

    try:
        _context, role = await PermissionService(db).check_context_access(
            user_id, context_id, ContextRole.VIEWER, capacity=CapacityPolicy.ALLOWED
        )
    except (AuthorizationError, NotFoundException):
        return None
    return role


def addressable_principal(user_id: str, role: ContextRole | None) -> SessionPrincipal | None:
    """The principal whose sessions this caller may address; None = every one.

    A context OWNER (workspace owner/admin included) may name any session in
    the context. Everyone else — and every agent credential, owner or not, so
    an agent can only ever reach its own agent's sessions — addresses only
    the sessions written with its own sub and its own credential's agent.
    """
    from auth.agent_scope import get_agent_scope

    if role == ContextRole.OWNER and get_agent_scope() is None:
        return None
    return caller_principal(user_id)


def _scope_predicates(scope: SessionScope) -> list[ColumnElement[bool]]:
    """Every event of the scope in the context, any writer.

    Window mode: the principal's events with ``since <= at < until``
    (``idx_memory_revisions_actor_at``).
    """
    preds: list[ColumnElement[bool]] = [MemoryRevision.context_id == scope.context_id]
    if scope.window:
        assert scope.principal is not None  # SessionScope.__post_init__
        preds += [
            principal_predicate(
                MemoryRevision.actor_user_id, MemoryRevision.agent_id, scope.principal
            ),
            MemoryRevision.at >= scope.since,
            MemoryRevision.at < scope.until,
        ]
        return preds
    preds.append(MemoryRevision.session_id == scope.session_id)
    if scope.principal is not None:
        preds.append(
            principal_predicate(
                MemoryRevision.actor_user_id, MemoryRevision.agent_id, scope.principal
            )
        )
    return preds


def _api_event_predicates(scope: SessionScope) -> list[ColumnElement[bool]]:
    """The session's own API writes — what a rollback reverses.

    The rollback's own events (``writer='rollback'``) are never reversed by a
    rollback; they count as another writer's events instead.
    """
    return [*_scope_predicates(scope), MemoryRevision.writer == REVISION_WRITER_API]


def _foreign_predicate(scope: SessionScope) -> ColumnElement[bool]:
    """An event that is NOT one of the scope's API writes (NULL-safe).

    Window mode: the negation of the window predicate — another actor or
    agent, another writer, or outside the window. The principal's own write
    after ``until`` on a memory therefore skips it (``modified_since``); one
    before ``since`` is older than the window's first event there and blocks
    nothing. ``actor_user_id`` and ``at`` are NOT NULL.
    """
    if scope.window:
        assert scope.principal is not None
        return or_(
            MemoryRevision.actor_user_id != scope.principal.actor_user_id,
            MemoryRevision.agent_id.is_distinct_from(scope.principal.agent_id),
            MemoryRevision.writer != REVISION_WRITER_API,
            MemoryRevision.at < scope.since,
            MemoryRevision.at >= scope.until,
        )
    parts: list[ColumnElement[bool]] = [
        MemoryRevision.session_id.is_distinct_from(scope.session_id),
        MemoryRevision.writer != REVISION_WRITER_API,
    ]
    if scope.principal is not None:
        parts.append(MemoryRevision.actor_user_id != scope.principal.actor_user_id)
        parts.append(MemoryRevision.agent_id.is_distinct_from(scope.principal.agent_id))
    return or_(*parts)


def _record_principal_predicates(
    context_id: UUID, principal: SessionPrincipal | None
) -> list[ColumnElement[bool]]:
    return [
        MemorySessionRollback.context_id == context_id,
        MemorySessionRollback.principal_user_id.is_(None)
        if principal is None
        else MemorySessionRollback.principal_user_id == principal.actor_user_id,
        MemorySessionRollback.agent_id.is_(None)
        if principal is None or principal.agent_id is None
        else MemorySessionRollback.agent_id == principal.agent_id,
    ]


def _target_predicates(scope: SessionScope) -> list[ColumnElement[bool]]:
    """The ``memory_session_rollbacks`` row of this scope (its unique key)."""
    preds = _record_principal_predicates(scope.context_id, scope.principal)
    if scope.window:
        return [
            *preds,
            MemorySessionRollback.session_id.is_(None),
            MemorySessionRollback.window_since == scope.since,
            MemorySessionRollback.window_until == scope.until,
        ]
    return [*preds, MemorySessionRollback.session_id == scope.session_id]


async def unfinished_window_until(
    db: AsyncSession, context_id: UUID, principal: SessionPrincipal, since: datetime
) -> datetime | None:
    """The ``until`` of the principal's unfinished rollback of a window from ``since``.

    A follow-up call that omits ``until`` continues that record instead of
    opening a new window ending now (#2121); None when there is none (the
    caller then ends the window now). The newest such record wins.
    """
    return await db.scalar(
        select(MemorySessionRollback.window_until)
        .where(
            *_record_principal_predicates(context_id, principal),
            MemorySessionRollback.session_id.is_(None),
            MemorySessionRollback.window_since == since,
            MemorySessionRollback.status.in_(
                (SESSION_ROLLBACK_IN_PROGRESS, SESSION_ROLLBACK_PARTIAL)
            ),
        )
        .order_by(MemorySessionRollback.id.desc())
        .limit(1)
    )


# ---------------------------------------------------------------------------
# The planner (pure)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionEvent:
    """One of the session's API writes on a memory."""

    id: int
    memory_id: UUID
    kind: str
    version: int
    prior: dict[str, Any]
    actor_user_id: str


@dataclass(frozen=True)
class MemoryState:
    """The memory as it is now (read under the row lock when executing)."""

    exists: bool
    deleted: bool = False
    deleted_by: str | None = None
    version: int = 1
    resource_row: bool = False
    # Another writer's event after the session's first event on this memory.
    foreign_after: bool = False
    permitted: bool = True
    # The versioned fields as stored (``versioned_snapshot``).
    current: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PlanItem:
    """What the rollback does to one memory."""

    memory_id: UUID
    kind: str  # what the session did: created / updated / forgotten
    action: str
    cause: str | None = None
    values: dict[str, Any] = field(default_factory=dict)
    unforget: bool = False
    unforget_deleted_by: str | None = None
    event_ids: tuple[int, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        item: dict[str, Any] = {
            "memory_id": str(self.memory_id),
            "kind": self.kind,
            "action": self.action,
        }
        if self.cause:
            item["cause"] = self.cause
        if self.action == ACTION_RESTORE:
            item["fields"] = sorted(self.values)
            if self.unforget:
                item["unforget"] = True
        return item


def combined_prior(updates: list[SessionEvent]) -> tuple[dict[str, Any], bool]:
    """The values from before the earliest of ``updates`` changed each field.

    ``updates`` oldest first. A field's value is the ``prior`` of the first
    update that changed it. Returns ``(values, erased)``: ``erased`` is True
    when such a first ``prior`` lost the field to erasure (the old value is
    gone, so the memory cannot be put back whole). Marker keys are never
    values.
    """
    values: dict[str, Any] = {}
    seen: set[str] = set()
    erased = False
    for event in updates:
        # #2043's erasure markers, read only through the memory_versions
        # helpers so the marker keys are never values. ``erased`` names a
        # field the event changed whose overwritten value is gone. The
        # ``scrubbed`` marker is not read: it says the LIVE value is gone (the
        # memory's author was erased) and sits on every ``updated`` event of
        # such a memory whatever the event changed, while erasure strips a
        # changed ``details`` and marks it ``erased`` — so a session that
        # edited only the tags of an erased author's memory still gets its
        # tags back (v0.113.0 review F3).
        lost = set(prior_erased(event.prior))
        kept = prior_values(event.prior)
        for name in VERSIONED_FIELDS:
            if name in seen:
                continue
            if name in lost:
                erased = True
                seen.add(name)
            elif name in kept:
                values[name] = kept[name]
                seen.add(name)
    return values, erased


def plan_memory(events: list[SessionEvent], state: MemoryState) -> PlanItem:
    """Decide what the rollback does to one memory (see the module table).

    Args:
        events: The session's unhandled API events on the memory, oldest first.
        state: The memory now.
    """
    first, last = events[0], events[-1]
    kind = REVISION_KIND_CREATED if first.kind == REVISION_KIND_CREATED else last.kind
    ids = tuple(e.id for e in events)

    def skip(cause: str) -> PlanItem:
        return PlanItem(first.memory_id, kind, ACTION_SKIP, cause=cause, event_ids=ids)

    if not state.exists:
        return skip(CAUSE_PURGED)
    if state.resource_row:
        return skip(CAUSE_RESOURCE_ROW)
    if not state.permitted:
        return skip(CAUSE_NOT_PERMITTED)
    if state.foreign_after or state.version != last.version:
        return skip(CAUSE_MODIFIED_SINCE)
    forgotten_by_session = (
        state.deleted
        and last.kind == REVISION_KIND_FORGOTTEN
        and state.deleted_by == last.actor_user_id
    )
    if state.deleted and not forgotten_by_session:
        return skip(
            CAUSE_MERGED_BY_SLEEP
            if state.deleted_by in SLEEP_TOMBSTONE_DELETED_BY
            else CAUSE_DELETED_BY_OTHER
        )
    if first.kind == REVISION_KIND_CREATED:
        if state.deleted:
            return skip(CAUSE_ALREADY_FORGOTTEN)
        return PlanItem(first.memory_id, kind, ACTION_FORGET, event_ids=ids)
    values, erased = combined_prior([e for e in events if e.kind == REVISION_KIND_UPDATED])
    if erased:
        return skip(CAUSE_PRIOR_ERASED)
    # A field the session changed and changed back needs no write.
    values = {
        name: value
        for name, value in values.items()
        if _comparable(name, value) != _comparable(name, state.current.get(name))
    }
    if values:
        return PlanItem(
            first.memory_id,
            kind,
            ACTION_RESTORE,
            values=values,
            unforget=state.deleted,
            unforget_deleted_by=last.actor_user_id if state.deleted else None,
            event_ids=ids,
        )
    if state.deleted:
        return PlanItem(
            first.memory_id,
            kind,
            ACTION_UNFORGET,
            unforget=True,
            unforget_deleted_by=last.actor_user_id,
            event_ids=ids,
        )
    return skip(CAUSE_ALREADY_RESTORED)


def plan_counts(items: list[PlanItem]) -> dict[str, int]:
    counts = {ACTION_FORGET: 0, ACTION_RESTORE: 0, ACTION_UNFORGET: 0, ACTION_SKIP: 0}
    for item in items:
        counts[item.action] += 1
    return counts


# ---------------------------------------------------------------------------
# Reading the batch
# ---------------------------------------------------------------------------


@dataclass
class _Batch:
    items: list[PlanItem]
    memories: dict[UUID, Any]
    events: int
    remaining: int


async def _scope_exists(db: AsyncSession, scope: SessionScope) -> bool:
    """Whether the scope has an event to address.

    A session exists with any of its events; a window only with an API write
    of the principal — rollback-written events alone are nothing to undo.
    """
    preds = _api_event_predicates(scope) if scope.window else _scope_predicates(scope)
    return bool(await db.scalar(select(exists().where(*preds))))


async def _unhandled_count(db: AsyncSession, scope: SessionScope) -> int:
    return int(
        await db.scalar(
            select(func.count())
            .select_from(MemoryRevision)
            .where(*_api_event_predicates(scope), MemoryRevision.undone_by.is_(None))
        )
        or 0
    )


async def _handled_exists(db: AsyncSession, scope: SessionScope) -> bool:
    return bool(
        await db.scalar(
            select(
                exists().where(*_api_event_predicates(scope), MemoryRevision.undone_by.is_not(None))
            )
        )
    )


async def _read_batch(
    db: AsyncSession, scope: SessionScope, user_id: str, workspace_id: UUID, *, lock: bool
) -> _Batch:
    """The next batch's plan: the newest memories first, ≤ the event cap.

    Executing (``lock``), the batch's memory rows are locked before their
    events are read: a write that held a row lock when the groups were read
    (the session's own ``update_memory`` racing its rollback) has committed
    its event by the time the lock is granted, so the plan sees it as one of
    the session's events instead of skipping the memory as ``modified_since``
    and marking its earlier events handled for good (v0.113.0 review F7).
    """
    unhandled = [*_api_event_predicates(scope), MemoryRevision.undone_by.is_(None)]
    groups = (
        await db.execute(
            select(
                MemoryRevision.memory_id,
                func.max(MemoryRevision.id).label("last_id"),
                func.count().label("n"),
            )
            .where(*unhandled)
            .group_by(MemoryRevision.memory_id)
            .order_by(func.max(MemoryRevision.id).desc())
            .limit(MAX_ROLLBACK_EVENTS_PER_CALL)
        )
    ).all()
    chosen: list[UUID] = []
    taken = 0
    for group in groups:
        if chosen and taken + group.n > MAX_ROLLBACK_EVENTS_PER_CALL:
            break
        chosen.append(group.memory_id)
        taken += int(group.n)
    if not chosen:
        return _Batch(items=[], memories={}, events=0, remaining=await _unhandled_count(db, scope))

    stmt = select(Memory).where(Memory.id.in_(chosen)).order_by(Memory.id)
    if lock:
        # Id order, like every multi-row lock here (backend rules, #1985).
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    memories = {m.id: m for m in (await db.execute(stmt)).scalars()}
    # Counted after the lock, with the events below, so ``remaining`` includes
    # an event committed while the lock was awaited.
    total = await _unhandled_count(db, scope)
    rows = (
        await db.execute(
            select(MemoryRevision)
            .where(*unhandled, MemoryRevision.memory_id.in_(chosen))
            .order_by(MemoryRevision.id)
        )
    ).scalars()
    events: dict[UUID, list[SessionEvent]] = defaultdict(list)
    for r in rows:
        events[r.memory_id].append(
            SessionEvent(
                id=r.id,
                memory_id=r.memory_id,
                kind=r.kind,
                version=r.version,
                prior=dict(r.prior or {}),
                actor_user_id=r.actor_user_id,
            )
        )
    # The newest event by anyone else on each memory, compared with the
    # session's first one there.
    foreign_last = dict(
        (
            await db.execute(
                select(MemoryRevision.memory_id, func.max(MemoryRevision.id))
                .where(MemoryRevision.memory_id.in_(chosen), _foreign_predicate(scope))
                .group_by(MemoryRevision.memory_id)
            )
        ).all()
    )
    permitted = await _permitted(db, user_id, workspace_id, scope.context_id, memories)

    items: list[PlanItem] = []
    for memory_id in chosen:
        session_events = events.get(memory_id)
        if not session_events:
            continue  # handled by a concurrent call between the two reads
        memory = memories.get(memory_id)
        if memory is None:
            state = MemoryState(exists=False)
        else:
            foreign = foreign_last.get(memory_id)
            state = MemoryState(
                exists=True,
                deleted=memory.deleted_at is not None,
                deleted_by=memory.deleted_by,
                version=memory.version,
                resource_row=memory.resource_id is not None,
                foreign_after=foreign is not None and foreign > session_events[0].id,
                permitted=permitted.get(memory_id, False),
                current=versioned_snapshot(memory),
            )
        items.append(plan_memory(session_events, state))
    items = _defer_extra_unforgets(items)
    events_taken = sum(len(item.event_ids) for item in items)
    return _Batch(
        items=items,
        memories=memories,
        events=events_taken,
        remaining=max(total - events_taken, 0),
    )


def _defer_extra_unforgets(items: list[PlanItem]) -> list[PlanItem]:
    """Keep the first ``MAX_UNFORGETS_PER_CALL`` un-forgets; later ones wait.

    A deferred item's events stay unhandled, so the next call plans it again.
    """
    kept: list[PlanItem] = []
    unforgets = 0
    for item in items:
        if item.unforget:
            unforgets += 1
            if unforgets > MAX_UNFORGETS_PER_CALL:
                continue
        kept.append(item)
    return kept


async def _permitted(
    db: AsyncSession,
    user_id: str,
    workspace_id: UUID,
    context_id: UUID,
    memories: dict[UUID, Any],
) -> dict[UUID, bool]:
    """forget's per-memory write check, once per (author, type, source) group."""
    from services.permission_service import CallerId, MemoryAuthorId, PermissionService

    perm = PermissionService(db)
    decided: dict[tuple[str, str, str | None], bool] = {}
    result: dict[UUID, bool] = {}
    for memory_id, memory in memories.items():
        group = (memory.user_id, memory.type, memory.source_type)
        if group not in decided:
            decided[group] = await perm.can_access_memory(
                user_id=CallerId(user_id),
                memory_user_id=MemoryAuthorId(memory.user_id),
                workspace_id=workspace_id,
                context_id=context_id,
                access="write",
                # Deny-capture identity: the rollback rewrites or removes the
                # memory, the audit vocabulary's ``update``.
                operation="update",
                memory_type=memory.type,
                memory_source_type=memory.source_type,
            )
        result[memory_id] = decided[group]
    return result


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PayloadSync:
    """One restored memory's vector payload patch, applied after the commit.

    ``fields`` are the restored versioned fields; ``updates`` /
    ``delete_keys`` the patch built from the row at the restore, used only
    when ``finish`` cannot re-read the row.
    """

    memory_id: UUID
    collection: str
    fields: tuple[str, ...]
    updates: dict[str, Any]
    delete_keys: tuple[str, ...]
    cascade_chunks: bool


@dataclass
class RollbackResult:
    """What one ``rollback_session`` call did (or, on a dry run, would do)."""

    dry_run: bool
    items: list[PlanItem]
    events: int
    remaining: int
    rollback_id: int | None = None
    status: str | None = None  # the rollback row's status after this call
    summary: dict[str, Any] = field(default_factory=dict)  # this call
    totals: dict[str, Any] = field(default_factory=dict)  # every call so far
    errors: list[str] = field(default_factory=list)
    correlation_id: str | None = None
    # Work for after the commit (``finish``).
    reembed_ids: list[UUID] = field(default_factory=list)
    restored_ids: list[UUID] = field(default_factory=list)
    forgotten_ids: list[UUID] = field(default_factory=list)
    forgotten_points: list[str] = field(default_factory=list)
    collection: str | None = None
    payload_syncs: list[PayloadSync] = field(default_factory=list)
    # (workspace_id, requested_by, context_id) for the access-event audit.
    audit: tuple[UUID, str, UUID] | None = None

    def listed_items(self) -> list[dict[str, Any]]:
        return [item.as_dict() for item in self.items[:MAX_LISTED_ITEMS]]


def _empty_summary() -> dict[str, Any]:
    return {**dict.fromkeys(_COUNT_KEYS, 0), "skipped_items": [], "errors": []}


def _window_lock_key(context_id: UUID, principal: SessionPrincipal) -> str:
    agent = principal.agent_id or "-"
    return f"session_rollback:{context_id}:principal:{principal.actor_user_id}:{agent}"


def _lock_key(scope: SessionScope) -> str:
    if scope.window:
        assert scope.principal is not None
        return _window_lock_key(scope.context_id, scope.principal)
    return f"session_rollback:{scope.context_id}:{scope.session_id}"


async def lock_window_principal(
    db: AsyncSession, context_id: UUID, principal: SessionPrincipal
) -> None:
    """Take the window-mode lock of one principal before resolving an omitted ``until``.

    The same transaction lock ``rollback_session`` takes for a window (it
    stacks), so two concurrent first calls resolve their window one after the
    other: the second sees the first's record and continues it instead of
    opening a second record at its own now.
    """
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": _window_lock_key(context_id, principal)},
    )


async def _lock_scope(db: AsyncSession, scope: SessionScope) -> None:
    """Serialize the rollbacks of one scope in one context.

    Session mode: every rollback of one session id (whatever principal).
    Window mode: every window of one principal, so overlapping windows run
    one after the other. A session-mode and a window-mode rollback of the
    same principal take different locks; they meet on the memory row locks
    and the ``undone_by IS NULL`` re-read, which keeps an event from being
    reversed twice.
    """
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": _lock_key(scope)},
    )


async def _mark_handled(db: AsyncSession, event_ids: list[int], rollback_id: int) -> None:
    if not event_ids:
        return
    await db.execute(
        update(MemoryRevision)
        .where(MemoryRevision.id.in_(event_ids), MemoryRevision.undone_by.is_(None))
        .values(undone_by=rollback_id)
        .execution_options(synchronize_session=False)
    )


class _Executor:
    """Runs one batch's items, each in a SAVEPOINT (#2065's rule)."""

    def __init__(
        self,
        db: AsyncSession,
        *,
        scope: SessionScope,
        workspace_id: UUID,
        requested_by: str,
        rollback_id: int,
        memories: dict[UUID, Any],
        classify: Callable[[BaseException], str],
        new_correlation_id: Callable[[], str],
        deadline: float | None = None,
    ) -> None:
        from services.memory_service import MemoryService

        self.db = db
        self.scope = scope
        self.ws = str(workspace_id)
        self.ctx = str(scope.context_id)
        self.requested_by = requested_by
        self.rollback_id = rollback_id
        self.memories = memories
        self.classify = classify
        self.new_correlation_id = new_correlation_id
        self.provenance: WriteProvenance = current_provenance()
        self.service = MemoryService(db)
        self.summary = _empty_summary()
        self.correlation_id: str | None = None
        self.reembed_ids: list[UUID] = []
        self.restored_ids: list[UUID] = []
        self.forgotten_ids: list[UUID] = []
        self.forgotten_points: list[str] = []
        self.failed_events = 0
        self.collection: str | None = None
        self._embedding: tuple[Any, str] | None = None
        # time.monotonic() after which no further item is started.
        self.deadline = deadline
        self.deferred: list[PlanItem] = []
        self.payload_syncs: list[PayloadSync] = []
        self._item_syncs: list[PayloadSync] = []
        self._payload_target: tuple[str, bool] | None = None

    async def run(self, items: list[PlanItem]) -> None:
        skipped = [item for item in items if item.action == ACTION_SKIP]
        forgets = [item for item in items if item.action == ACTION_FORGET]
        restores = [item for item in items if item.action in (ACTION_RESTORE, ACTION_UNFORGET)]
        for item in skipped:
            self.summary["skipped"] += 1
            self.summary["skipped_items"].append(
                {"memory_id": str(item.memory_id), "kind": item.kind, "cause": item.cause}
            )
        await _mark_handled(
            self.db, [i for item in skipped for i in item.event_ids], self.rollback_id
        )
        if forgets:
            self.collection = await self._collection()
        units: list[tuple[list[PlanItem], Callable[[list[PlanItem]], Any]]] = [
            (forgets[start : start + FORGET_CHUNK], self._forget_chunk)
            for start in range(0, len(forgets), FORGET_CHUNK)
        ]
        units += [([item], self._restore) for item in restores]
        for index, (unit, work) in enumerate(units):
            if index and self._out_of_time():
                # Time-boxed (v0.113.0 review F2): the rest waits for the next
                # call, its events unhandled, so a slow embedding provider
                # cannot push the whole batch past the tool timeout — which
                # would roll everything back and replay the same batch on
                # every call. The first unit always runs: each call progresses.
                for _rest, _ in units[index:]:
                    self.deferred.extend(_rest)
                break
            await self._guarded(unit, work)

    def _out_of_time(self) -> bool:
        return self.deadline is not None and time.monotonic() >= self.deadline

    async def _guarded(self, items: list[PlanItem], work: Callable[[list[PlanItem]], Any]) -> None:
        event_ids = [i for item in items for i in item.event_ids]
        self._item_syncs = []
        try:
            async with self.db.begin_nested():
                await work(items)
                await _mark_handled(self.db, event_ids, self.rollback_id)
            self.restored_ids.extend(
                item.memory_id for item in items if item.action != ACTION_FORGET
            )
            # Only an item whose SAVEPOINT was released syncs its payload.
            self.payload_syncs.extend(self._item_syncs)
        except Exception as exc:
            # The SAVEPOINT rolled the item back whole, and its events stay
            # unhandled: the next call plans the item again from the row as it
            # is then (a transient vector-store or provider failure is retried,
            # never half-applied). The summary names it.
            self.failed_events += len(event_ids)
            self.correlation_id = self.correlation_id or self.new_correlation_id()
            cause = self.classify(exc)
            for item in items:
                logger.warning(
                    "session_rollback_item_failed",
                    memory_id=str(item.memory_id),
                    action=item.action,
                    cause=cause,
                    correlation_id=self.correlation_id,
                    exc_type=type(exc).__name__,
                    exc=str(exc),
                    exc_info=exc,
                )
                self.summary["failed"] += 1
                self.summary["errors"].append(
                    f"Memory {item.memory_id} ({item.action}) failed: {cause} "
                    f"(correlation_id {self.correlation_id})"
                )

    async def _collection(self) -> str:
        from services.context_routing import resolve_collection_name

        return await resolve_collection_name(self.db, self.scope.context_id)

    async def _embedding_target(self) -> tuple[Any, str]:
        if self._embedding is None:
            from services.embedding_service import EmbeddingService
            from services.sleep.undo import _resolve_embedding_info

            model, collection = await _resolve_embedding_info(self.db, self.scope.context_id)
            self._embedding = (EmbeddingService(self.db, model=model), collection)
        return self._embedding

    async def _forget_chunk(self, items: list[PlanItem]) -> None:
        """Forget what the session created: forget's own chunk statement.

        The rows' points are deleted after the commit (``finish``), not here:
        this call commits once, at its end, and a point deleted before a
        rollback of that transaction would leave a live row no vector search
        finds. The other way round is safe — a tombstone's point is never a
        hit (hits are hydrated from live rows) and the orphan sweep removes
        one whose delete failed.
        """
        deleted, points = await self.service._soft_delete_chunk(
            self.requested_by,
            [item.memory_id for item in items],
            self.ws,
            self.ctx,
            writer=REVISION_WRITER_ROLLBACK,
        )
        self.forgotten_ids.extend(deleted)
        self.forgotten_points.extend(points)
        self.summary["forgotten"] += len(deleted)

    async def _restore(self, items: list[PlanItem]) -> None:
        """Un-forget and/or put back the pre-session values of one memory."""
        from db.point_writer_lock import hold_point_writer_lock
        from services.sleep.merge_retention import restore_forget_tombstone_stmt

        (item,) = items
        memory = self.memories[item.memory_id]
        if item.unforget:
            # #1798: the point is rebuilt before this UPDATE commits.
            await hold_point_writer_lock(self.db)
            result = await self.db.execute(
                restore_forget_tombstone_stmt(
                    memory.id,
                    context_id=self.scope.context_id,
                    deleted_by=cast(str, item.unforget_deleted_by),
                    agent_id=self.provenance.agent_id,
                    session_id=self.provenance.session_id,
                )
            )
            if cast(CursorResult[Any], result).rowcount != 1:
                # The row was read under its lock, so this is not expected.
                raise RuntimeError("tombstone changed under the row lock")
            await self.db.refresh(memory)
            record_revision(
                self.db,
                memory,
                kind=REVISION_KIND_RESTORED,
                actor_user_id=self.requested_by,
                writer=REVISION_WRITER_ROLLBACK,
                provenance=self.provenance,
            )
        text_changed = False
        changed: set[str] = set()
        if item.values:
            before = versioned_snapshot(memory)
            for name, value in item.values.items():
                setattr(memory, name, value)
            memory.updated_at = utcnow()  # a restore of values is an edit
            changed = {name for name in item.values if before[name] != getattr(memory, name)}
            text_changed = bool(changed & _TEXT_FIELDS)
            record_edit(
                self.db,
                memory,
                before,
                actor_user_id=self.requested_by,
                writer=REVISION_WRITER_ROLLBACK,
                provenance=self.provenance,
            )
        if item.unforget:
            from services.sleep.undo import re_embed_memory_to_qdrant

            await self.db.flush()  # the point is built from the restored row
            embedding_svc, collection = await self._embedding_target()
            requeued = await re_embed_memory_to_qdrant(
                memory,
                memory.user_id,
                embedding_svc,
                self.ws,
                self.ctx,
                collection,
                db=self.db,
            )
            if requeued == "chunks":
                self.summary["chunks_requeued"] += 1
            elif requeued == "point":
                self.summary["points_requeued"] += 1
            self.summary["restored"] += 1
            if item.values:
                self.summary["updated_restored"] += 1
            return
        if text_changed:
            # update_memory's pattern: re-embed after the commit.
            memory.embedding_status = "pending"
            self.reembed_ids.append(memory.id)
        elif changed:
            await self._queue_payload_sync(memory, changed)
        self.summary["updated_restored"] += 1

    async def _queue_payload_sync(self, memory: Any, changed: set[str]) -> None:
        """Queue the restored fields' payload patch for ``finish`` (after the commit).

        Not patched here: this call commits once, at its end, and a payload
        patched before a rollback of that transaction (a tool timeout, a
        failed commit) would leave the live row with the session's tags /
        importance / type in Postgres and the pre-session ones in the vector
        payload that ``filters`` read — with nothing to repair it (v0.113.0
        review F1). The collection and the chunk cascade are read now, while
        the session is in its transaction.
        """
        from services.memory_service import metadata_payload_patch

        updates, delete_keys = metadata_payload_patch(
            _payload_request(memory, changed), None, memory.details
        )
        if not (updates or delete_keys):
            return
        if self._payload_target is None:
            from services.content_chunk_writer import chunk_cascade_needed

            self._payload_target = (
                await self._collection(),
                await chunk_cascade_needed(self.db, self.scope.context_id),
            )
        collection, cascade = self._payload_target
        self._item_syncs.append(
            PayloadSync(
                memory_id=memory.id,
                collection=collection,
                fields=tuple(sorted(changed)),
                # Issue #78: date-range filters read the payload's updated_at.
                updates={**updates, "updated_at": to_utc_iso(memory.updated_at)},
                delete_keys=tuple(delete_keys),
                cascade_chunks=cascade,
            )
        )


def _sync_patch(sync: PayloadSync, row: Any | None) -> tuple[dict[str, Any], list[str]]:
    """``sync``'s patch, rebuilt from ``row`` (the row now) when there is one."""
    if row is None:
        return sync.updates, list(sync.delete_keys)
    from services.memory_service import metadata_payload_patch

    updates, delete_keys = metadata_payload_patch(
        _payload_request(row, set(sync.fields)), None, row.details
    )
    return {**updates, "updated_at": to_utc_iso(row.updated_at)}, delete_keys


def _payload_request(memory: Any, changed: set[str]) -> Any:
    """The fields ``_update_sync_qdrant_payload`` reads, for a restore.

    A field restored to NULL is still sent (``[]`` / ``{}``), so the payload
    loses the value the session wrote.
    """
    return SimpleNamespace(
        tags=(memory.tags or []) if "tags" in changed else None,
        importance=memory.importance if "importance" in changed else None,
        type=memory.type if "type" in changed else None,
        details=(memory.details if memory.details is not None else {})
        if "details" in changed
        else None,
    )


def _add_totals(totals: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any]:
    merged = {key: int(totals.get(key, 0)) + int(summary.get(key, 0)) for key in _COUNT_KEYS}
    merged["calls"] = int(totals.get("calls", 0)) + 1
    return merged


async def rollback_session(
    db: AsyncSession,
    *,
    scope: SessionScope,
    workspace_id: UUID,
    requested_by: str,
    dry_run: bool,
    classify: Callable[[BaseException], str],
    new_correlation_id: Callable[[], str],
    time_budget: float | None = None,
) -> RollbackResult:
    """Plan (``dry_run``) or execute the next batch of one session's rollback.

    The caller has authorized the context (editor) and built ``scope`` with
    ``addressable_principal`` (session mode) or ``caller_principal`` (window
    mode). Nothing is committed here: the caller commits,
    then runs ``finish(result)``. With ``time_budget`` (seconds, from the
    start of this call) no further item is started once it is spent; the rest
    is left for the next call (``remaining``).

    Raises:
        SessionNotFoundError: no event of the session exists in the context.
        NoWritesInWindowError: the principal has no API write in the window.
        AlreadyRolledBackError: the scope's rollback is ``completed`` and the
            session wrote nothing since, or every one of its events was
            handled by another rollback (a context owner's, a session-mode
            one of a window's events, another window's).
    """
    deadline = time.monotonic() + time_budget if time_budget is not None else None
    if not dry_run:
        await _lock_scope(db, scope)
    exists = await _scope_exists(db, scope)
    record_stmt = select(MemorySessionRollback).where(*_target_predicates(scope))
    if not dry_run:
        record_stmt = record_stmt.with_for_update()
    record = (await db.execute(record_stmt)).scalar_one_or_none()
    if not exists and not (scope.window and record is not None):
        raise NoWritesInWindowError if scope.window else SessionNotFoundError
    # A window record whose events are all gone (purged memories cascade
    # their revisions away) is closed by the empty batch below instead of
    # answering no_writes_in_window forever: an omitted until would keep
    # continuing it and never reach the caller's newer writes.

    batch = await _read_batch(db, scope, requested_by, workspace_id, lock=not dry_run)
    if not batch.items:
        if record is not None and record.status in _FINAL_STATUSES:
            raise AlreadyRolledBackError(record.id, record.status)
        if record is None:
            if await _handled_exists(db, scope):
                # Every event was handled by another rollback of the same
                # events (a context owner's, the principal's own session-mode
                # one, or another window's).
                raise AlreadyRolledBackError(None, SESSION_ROLLBACK_COMPLETED)
            # Only rollback-written events under this session: nothing to
            # undo, and no record, so a later write in the session stays
            # undoable.
            return RollbackResult(dry_run=dry_run, items=[], events=0, remaining=0)
        # An unfinished record whose remaining events another rollback (a
        # context owner's) handled: the call below closes it.
    if dry_run:
        return RollbackResult(
            dry_run=True,
            items=batch.items,
            events=batch.events,
            remaining=batch.remaining,
            rollback_id=record.id if record is not None else None,
            status=record.status if record is not None else None,
        )

    if record is None:
        principal = scope.principal
        record = MemorySessionRollback(
            workspace_id=workspace_id,
            context_id=scope.context_id,
            session_id=scope.session_id,
            principal_user_id=principal.actor_user_id if principal is not None else None,
            agent_id=principal.agent_id if principal is not None else None,
            window_since=scope.since,
            window_until=scope.until,
            requested_by=requested_by,
            status=SESSION_ROLLBACK_IN_PROGRESS,
            summary={},
        )
        db.add(record)
        await db.flush()
    elif record.status in _FINAL_STATUSES:
        # The session wrote again after its rollback finished (the MCP
        # session id lasts a whole connection), or a write that was in flight
        # when a window's rollback finished landed inside the window: those
        # events are new, never handled, and are rolled back under the same
        # record.
        record.status = SESSION_ROLLBACK_IN_PROGRESS
        record.completed_at = None

    executor = _Executor(
        db,
        scope=scope,
        workspace_id=workspace_id,
        requested_by=requested_by,
        rollback_id=record.id,
        memories=batch.memories,
        classify=classify,
        new_correlation_id=new_correlation_id,
        deadline=deadline,
    )
    await executor.run(batch.items)

    totals = _add_totals(record.summary or {}, executor.summary)
    record.summary = totals
    # A failed item's events stay unhandled, and so do a deferred item's:
    # both are part of what remains.
    deferred_events = sum(len(item.event_ids) for item in executor.deferred)
    remaining = batch.remaining + executor.failed_events + deferred_events
    if remaining == 0:
        record.status = SESSION_ROLLBACK_COMPLETED
        record.completed_at = utcnow()
    else:
        record.status = (
            SESSION_ROLLBACK_PARTIAL if executor.failed_events else SESSION_ROLLBACK_IN_PROGRESS
        )
    await db.flush()
    logger.info(
        "session_rolled_back",
        rollback_id=record.id,
        context_id=str(scope.context_id),
        owner_scope=scope.principal is None,
        window=scope.window,
        status=record.status,
        events=batch.events,
        remaining=remaining,
        deferred=len(executor.deferred),
        **{key: executor.summary[key] for key in _COUNT_KEYS},
    )
    deferred_ids = {item.memory_id for item in executor.deferred}
    return RollbackResult(
        dry_run=False,
        items=[item for item in batch.items if item.memory_id not in deferred_ids],
        events=batch.events - executor.failed_events - deferred_events,
        remaining=remaining,
        rollback_id=record.id,
        status=record.status,
        summary=executor.summary,
        totals=totals,
        errors=list(executor.summary["errors"]),
        correlation_id=executor.correlation_id,
        reembed_ids=executor.reembed_ids,
        restored_ids=executor.restored_ids,
        forgotten_ids=executor.forgotten_ids,
        forgotten_points=executor.forgotten_points,
        collection=executor.collection,
        payload_syncs=executor.payload_syncs,
        audit=(workspace_id, requested_by, scope.context_id),
    )


async def finish(result: RollbackResult, db: AsyncSession | None = None) -> None:
    """After the commit: vector deletes, payload patches, re-embeds and the audit.

    The vector store follows Postgres, never the other way round: the
    forgotten rows' points and content chunks are deleted only now that the
    tombstones are committed, restored tags / importance / type reach the
    payload now that they are, and restored texts are re-embedded now that
    the restore is. Every step fails soft, as on the write paths it mirrors —
    a tombstone's leftover point is no hit and the orphan sweep removes it; a
    re-embed that fails leaves the row ``pending`` for the embedding sweep; a
    payload patch that fails is logged (the payload keeps the session's
    values until the memory's next edit or re-embed).

    With ``db`` (the call's session, committed) the payload patches are
    built from the rows as they are now, read ``FOR SHARE``: an edit that
    committed after the rollback is not overwritten by the restored values,
    and one still in flight (it holds the row lock while it patches the
    payload) finishes first. Without it the patches queued at the restore are
    applied as they are.
    """
    if result.forgotten_points and result.collection:
        from db.qdrant import delete_points_from_qdrant

        try:
            await delete_points_from_qdrant(result.forgotten_points, result.collection)
        except Exception as exc:
            logger.warning(
                "session_rollback_point_delete_failed",
                collection=result.collection,
                points=len(result.forgotten_points),
                error_class=type(exc).__name__,
            )
    await _apply_payload_syncs(result.payload_syncs, db)
    if result.reembed_ids:
        # One task working through the ids, not one task each: a rollback can
        # restore hundreds of texts, and each embedding run holds a database
        # connection while it calls the provider.
        task = asyncio.create_task(_reembed_in_turn(list(result.reembed_ids)))
        task.add_done_callback(_log_reembed_task)
    if result.forgotten_ids and result.collection:
        from db.qdrant import delete_chunk_points

        try:
            await delete_chunk_points(result.forgotten_ids, result.collection)
        except Exception as exc:
            logger.warning(
                "content_chunks_delete_failed",
                collection=result.collection,
                memories=len(result.forgotten_ids),
                error_class=type(exc).__name__,
            )
    await _emit_access_events(result)


#: Rows one payload-sync transaction holds ``FOR SHARE`` while it patches them.
PAYLOAD_SYNC_GROUP = 50


async def _share_lock_rows(db: AsyncSession, ids: list[UUID]) -> dict[UUID, Any]:
    """The live rows among ``ids``, locked ``FOR SHARE`` in id order."""
    rows = await db.execute(
        select(
            Memory.id,
            Memory.tags,
            Memory.importance,
            Memory.type,
            Memory.details,
            Memory.updated_at,
        )
        .where(Memory.id.in_(ids), Memory.deleted_at.is_(None))
        .order_by(Memory.id)
        .with_for_update(read=True)
    )
    return {row.id: row for row in rows}


async def _apply_payload_syncs(syncs: list[PayloadSync], db: AsyncSession | None) -> None:
    if not syncs:
        return
    from db.qdrant import update_memory_payload_in_qdrant

    failed = 0
    for start in range(0, len(syncs), PAYLOAD_SYNC_GROUP):
        group = syncs[start : start + PAYLOAD_SYNC_GROUP]
        rows: dict[UUID, Any] | None = None
        if db is not None:
            try:
                rows = await _share_lock_rows(db, [sync.memory_id for sync in group])
            except Exception as exc:
                logger.warning(
                    "session_rollback_payload_reread_failed", error_class=type(exc).__name__
                )
                await _end_transaction(db, commit=False)
        for sync in group:
            if rows is not None and sync.memory_id not in rows:
                continue  # forgotten or purged since: its payload is not read
            updates, delete_keys = _sync_patch(
                sync, rows.get(sync.memory_id) if rows is not None else None
            )
            try:
                await update_memory_payload_in_qdrant(
                    memory_id=sync.memory_id,
                    payload_updates=updates,
                    collection_name=sync.collection,
                    delete_keys=delete_keys or None,
                    cascade_chunks=sync.cascade_chunks,
                )
            except Exception as exc:
                failed += 1
                logger.warning(
                    "session_rollback_payload_sync_failed",
                    memory_id=str(sync.memory_id),
                    collection=sync.collection,
                    error_class=type(exc).__name__,
                )
        if rows is not None and db is not None:
            await _end_transaction(db, commit=True)  # releases the share locks
    if failed:
        logger.warning("session_rollback_payload_syncs_failed", failed=failed, of=len(syncs))


async def _end_transaction(db: AsyncSession, *, commit: bool) -> None:
    try:
        await (db.commit() if commit else db.rollback())
    except Exception as exc:
        logger.warning("session_rollback_payload_tx_end_failed", error_class=type(exc).__name__)


async def _emit_access_events(result: RollbackResult) -> None:
    """The agent access audit of the rollback's writes (#1286; no-op for users).

    One ``forget`` row for the memories it forgot and one ``update`` row for
    those it restored, each listing the ids like ``forget_bulk``'s chunk rows.
    """
    if result.audit is None:
        return
    from services.memory_access_event_writer import (
        MAX_METADATA_MEMORY_IDS,
        emit_memory_access_event,
    )

    workspace_id, user_id, context_id = result.audit
    for operation, ids in (("forget", result.forgotten_ids), ("update", result.restored_ids)):
        if not ids:
            continue
        await emit_memory_access_event(
            operation=operation,
            outcome="success",
            workspace_id=workspace_id,
            user_id=user_id,
            context_id=context_id,
            memory_id=ids[0] if len(ids) == 1 else None,
            result_count=len(ids),
            extra_metadata=(
                {
                    "memory_ids": [str(mid) for mid in ids[:MAX_METADATA_MEMORY_IDS]],
                    "rollback_id": result.rollback_id,
                }
                if len(ids) > 1
                else {"rollback_id": result.rollback_id}
            ),
        )


async def _reembed_in_turn(memory_ids: list[UUID]) -> None:
    """Embed the restored texts one after another; a failure skips to the next.

    A row whose run fails stays ``pending`` for the embedding sweep.
    """
    from services.memory_service import process_pending_embedding

    for memory_id in memory_ids:
        try:
            await process_pending_embedding(memory_id)
        except Exception as exc:
            logger.warning(
                "session_rollback_reembed_failed",
                memory_id=str(memory_id),
                error_class=type(exc).__name__,
            )


def _log_reembed_task(task: asyncio.Task[None]) -> None:
    if not task.cancelled() and task.exception() is not None:
        logger.warning(
            "session_rollback_reembed_task_failed",
            error_class=type(task.exception()).__name__,
        )


__all__ = [
    "MAX_ROLLBACK_EVENTS_PER_CALL",
    "MAX_ROLLBACK_WINDOW",
    "MAX_UNFORGETS_PER_CALL",
    "PAYLOAD_SYNC_GROUP",
    "SKIP_CAUSES",
    "AlreadyRolledBackError",
    "MemoryState",
    "NoWritesInWindowError",
    "PayloadSync",
    "PlanItem",
    "RollbackResult",
    "SessionEvent",
    "SessionNotFoundError",
    "SessionScope",
    "addressable_principal",
    "combined_prior",
    "context_role",
    "finish",
    "lock_window_principal",
    "plan_counts",
    "plan_memory",
    "rollback_session",
    "unfinished_window_until",
]
