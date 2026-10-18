"""``rollback_session``: undo what one session wrote in one context (#2044).

A session's writes are its ``memory_revisions`` events (``writer='api'``)
under one session id, written by one principal — the caller sub and the
verified agent of the credential (``SessionPrincipal``). The rollback groups
those events per memory and lets the memory's own history decide:

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

Execution holds a per-(context, session id) advisory lock, locks the batch's
memory rows in id order, and runs each item (each forget chunk) in a SAVEPOINT
of its own: a failed item leaves nothing behind and the rest still runs. Every
event a call handles — reversed, skipped or failed — gets the rollback row's
id in ``undone_by``, so a call that stops at ``MAX_ROLLBACK_EVENTS_PER_CALL``
(or at ``MAX_UNFORGETS_PER_CALL`` un-forgets, each of which re-embeds inside
the call) is continued by the next call, and nothing is ever reversed twice.
A rollback whose events are all handled is final (``completed``, or
``partial`` when an item failed); the next call is refused
(``AlreadyRolledBackError``).

No MCP types here; ``mcp_server/tools/session_rollback.py`` is the tool.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
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
from services.memory_versions import record_edit, record_revision, versioned_snapshot
from services.write_provenance import (
    SessionPrincipal,
    WriteProvenance,
    caller_principal,
    current_provenance,
    principal_predicate,
)
from utils.datetime import utcnow
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

# The markers #2043's erasure leaves in a ``prior`` whose ``details`` it
# removed: ``{"erased": ["details"]}`` (the subject wrote the edit) and
# ``{"scrubbed": ["details"]}`` (the subject authored the memory). Only
# ``VERSIONED_FIELDS`` keys are values; a field named by either marker has no
# old value left.
_PRIOR_LOST_KEYS = ("erased", "scrubbed")
_TEXT_FIELDS = frozenset({"summary", "context_summary", "content"})
_FINAL_STATUSES = frozenset({SESSION_ROLLBACK_COMPLETED, SESSION_ROLLBACK_PARTIAL})
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


class AlreadyRolledBackError(Exception):
    """The session's rollback is final (``completed`` / ``partial``)."""

    def __init__(self, rollback_id: int | None, status: str) -> None:
        super().__init__(status)
        self.rollback_id = rollback_id
        self.status = status


# ---------------------------------------------------------------------------
# Addressing: which session, whose events
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionScope:
    """The events a rollback addresses.

    ``principal`` is the session's principal; None is a context owner's
    rollback, which covers every principal's events under ``session_id`` in
    the context.
    """

    context_id: UUID
    session_id: str
    principal: SessionPrincipal | None


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


def _session_predicates(scope: SessionScope) -> list[ColumnElement[bool]]:
    """Every event of the session in the context, any writer."""
    preds: list[ColumnElement[bool]] = [
        MemoryRevision.context_id == scope.context_id,
        MemoryRevision.session_id == scope.session_id,
    ]
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
    return [*_session_predicates(scope), MemoryRevision.writer == REVISION_WRITER_API]


def _foreign_predicate(scope: SessionScope) -> ColumnElement[bool]:
    """An event that is NOT one of the session's API writes (NULL-safe)."""
    parts: list[ColumnElement[bool]] = [
        MemoryRevision.session_id.is_distinct_from(scope.session_id),
        MemoryRevision.writer != REVISION_WRITER_API,
    ]
    if scope.principal is not None:
        parts.append(MemoryRevision.actor_user_id != scope.principal.actor_user_id)
        parts.append(MemoryRevision.agent_id.is_distinct_from(scope.principal.agent_id))
    return or_(*parts)


def _target_predicates(scope: SessionScope) -> list[ColumnElement[bool]]:
    """The ``memory_session_rollbacks`` row of this scope (the unique key)."""
    principal = scope.principal
    return [
        MemorySessionRollback.context_id == scope.context_id,
        MemorySessionRollback.session_id == scope.session_id,
        MemorySessionRollback.principal_user_id.is_(None)
        if principal is None
        else MemorySessionRollback.principal_user_id == principal.actor_user_id,
        MemorySessionRollback.agent_id.is_(None)
        if principal is None or principal.agent_id is None
        else MemorySessionRollback.agent_id == principal.agent_id,
    ]


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
    when such a first ``prior`` lost the field to erasure (its ``erased`` /
    ``scrubbed`` marker names it: the old value is gone, so the memory cannot
    be put back whole). Marker keys are never values.
    """
    values: dict[str, Any] = {}
    seen: set[str] = set()
    erased = False
    for event in updates:
        prior = event.prior or {}
        lost = {
            name
            for key in _PRIOR_LOST_KEYS
            if isinstance(prior.get(key), list)
            for name in prior[key]
            if isinstance(name, str)
        }
        for name in VERSIONED_FIELDS:
            if name in seen:
                continue
            if name in lost:
                erased = True
                seen.add(name)
            elif name in prior:
                values[name] = prior[name]
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


def _comparable(name: str, value: Any) -> Any:
    # A NULL tag array and an empty one say the same thing (#2043's rule).
    return list(value or []) if name == "tags" else value


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


async def _session_exists(db: AsyncSession, scope: SessionScope) -> bool:
    return bool(await db.scalar(select(exists().where(*_session_predicates(scope)))))


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
    """The next batch's plan: the newest memories first, ≤ the event cap."""
    unhandled = [*_api_event_predicates(scope), MemoryRevision.undone_by.is_(None)]
    total = await _unhandled_count(db, scope)
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
        return _Batch(items=[], memories={}, events=0, remaining=total)

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
    stmt = select(Memory).where(Memory.id.in_(chosen)).order_by(Memory.id)
    if lock:
        # Id order, like every multi-row lock here (backend rules, #1985).
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    memories = {m.id: m for m in (await db.execute(stmt)).scalars()}
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
                memory_type=memory.type,
                memory_source_type=memory.source_type,
            )
        result[memory_id] = decided[group]
    return result


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


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
    forgotten_ids: list[UUID] = field(default_factory=list)
    collection: str | None = None

    def listed_items(self) -> list[dict[str, Any]]:
        return [item.as_dict() for item in self.items[:MAX_LISTED_ITEMS]]


def _empty_summary() -> dict[str, Any]:
    return {**dict.fromkeys(_COUNT_KEYS, 0), "skipped_items": [], "errors": []}


async def _lock_session(db: AsyncSession, scope: SessionScope) -> None:
    """Serialize every rollback of one session id in one context (whatever principal)."""
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"session_rollback:{scope.context_id}:{scope.session_id}"},
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
        self.forgotten_ids: list[UUID] = []
        self.collection: str | None = None
        self._embedding: tuple[Any, str] | None = None

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
        for start in range(0, len(forgets), FORGET_CHUNK):
            await self._guarded(forgets[start : start + FORGET_CHUNK], self._forget_chunk)
        for item in restores:
            await self._guarded([item], self._restore)

    async def _guarded(self, items: list[PlanItem], work: Callable[[list[PlanItem]], Any]) -> None:
        event_ids = [i for item in items for i in item.event_ids]
        try:
            async with self.db.begin_nested():
                await work(items)
                await _mark_handled(self.db, event_ids, self.rollback_id)
        except Exception as exc:
            # The SAVEPOINT rolled the item back; the events are still marked
            # handled (outside it) so a continuation never retries — and maybe
            # half-applies — an item that failed once. The summary names it.
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
            await _mark_handled(self.db, event_ids, self.rollback_id)

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
        """Forget what the session created: forget's own chunk statement."""
        from db.qdrant import delete_points_from_qdrant

        deleted, points = await self.service._soft_delete_chunk(
            self.requested_by,
            [item.memory_id for item in items],
            self.ws,
            self.ctx,
            writer=REVISION_WRITER_ROLLBACK,
        )
        # Points before the commit, as forget_bulk: a failed delete raises
        # inside the SAVEPOINT and the rows stay live with their points.
        await delete_points_from_qdrant(points, cast(str, self.collection))
        self.forgotten_ids.extend(deleted)
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
            await self.service._update_sync_qdrant_payload(
                memory, _payload_request(memory, changed), None, memory.details
            )
        self.summary["updated_restored"] += 1


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
) -> RollbackResult:
    """Plan (``dry_run``) or execute the next batch of one session's rollback.

    The caller has authorized the context (editor) and built ``scope`` with
    ``addressable_principal``. Nothing is committed here: the caller commits,
    then runs ``finish(result)``.

    Raises:
        SessionNotFoundError: no event of the scope exists in the context.
        AlreadyRolledBackError: the scope's rollback is final, or every one of
            its events was handled by another rollback (an owner's).
    """
    if not dry_run:
        await _lock_session(db, scope)
    if not await _session_exists(db, scope):
        raise SessionNotFoundError
    record_stmt = select(MemorySessionRollback).where(*_target_predicates(scope))
    if not dry_run:
        record_stmt = record_stmt.with_for_update()
    record = (await db.execute(record_stmt)).scalar_one_or_none()
    if record is not None and record.status in _FINAL_STATUSES:
        raise AlreadyRolledBackError(record.id, record.status)

    batch = await _read_batch(db, scope, requested_by, workspace_id, lock=not dry_run)
    if not batch.items and record is None:
        if await _handled_exists(db, scope):
            # Every event was handled by another rollback of the same events
            # (a context owner's, or the principal's own).
            raise AlreadyRolledBackError(None, SESSION_ROLLBACK_COMPLETED)
        # Only rollback-written events under this session: nothing to undo,
        # and no record, so a later write in the session stays undoable.
        return RollbackResult(dry_run=dry_run, items=[], events=0, remaining=0)
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
            requested_by=requested_by,
            status=SESSION_ROLLBACK_IN_PROGRESS,
            summary={},
        )
        db.add(record)
        await db.flush()

    executor = _Executor(
        db,
        scope=scope,
        workspace_id=workspace_id,
        requested_by=requested_by,
        rollback_id=record.id,
        memories=batch.memories,
        classify=classify,
        new_correlation_id=new_correlation_id,
    )
    await executor.run(batch.items)

    totals = _add_totals(record.summary or {}, executor.summary)
    record.summary = totals
    if batch.remaining > 0:
        record.status = SESSION_ROLLBACK_IN_PROGRESS
    else:
        record.status = SESSION_ROLLBACK_PARTIAL if totals["failed"] else SESSION_ROLLBACK_COMPLETED
        record.completed_at = utcnow()
    await db.flush()
    logger.info(
        "session_rolled_back",
        rollback_id=record.id,
        context_id=str(scope.context_id),
        owner_scope=scope.principal is None,
        status=record.status,
        events=batch.events,
        remaining=batch.remaining,
        **{key: executor.summary[key] for key in _COUNT_KEYS},
    )
    return RollbackResult(
        dry_run=False,
        items=batch.items,
        events=batch.events,
        remaining=batch.remaining,
        rollback_id=record.id,
        status=record.status,
        summary=executor.summary,
        totals=totals,
        errors=list(executor.summary["errors"]),
        correlation_id=executor.correlation_id,
        reembed_ids=executor.reembed_ids,
        forgotten_ids=executor.forgotten_ids,
        collection=executor.collection,
    )


async def finish(result: RollbackResult) -> None:
    """After the commit: re-embed restored text, drop forgotten chunk points.

    Both fail soft, as on the write paths they mirror: a re-embed task that
    fails leaves the row ``pending`` for the embedding sweep; chunk points of
    a tombstone are removed by the orphan sweep after its grace period.
    """
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
    "MAX_UNFORGETS_PER_CALL",
    "SKIP_CAUSES",
    "AlreadyRolledBackError",
    "MemoryState",
    "PlanItem",
    "RollbackResult",
    "SessionEvent",
    "SessionNotFoundError",
    "SessionScope",
    "addressable_principal",
    "combined_prior",
    "context_role",
    "finish",
    "plan_counts",
    "plan_memory",
    "rollback_session",
]
