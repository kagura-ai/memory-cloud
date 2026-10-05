"""Move a workspace's contexts (memories and edges included) from one user to another (#1783).

Identities are keyed by ``user_id`` and never linked by email (#481): a CLI
admin (``local:<login>``) and an OAuth sign-in (the IdP ``sub``) are two users
even when they are one person. Contexts created through the CLI admin's API
key then read as another creator in the browser — the "Created by me" filter
is empty and the private ones are hidden — because ``created_by`` is compared
with the session's ``user_id``. This one-shot command hands a workspace's
contexts to the identity that should hold them. Runs where the API runs (same
env: DATABASE_URL, the vector store), e.g. inside the API container::

    python -m src.cli.transfer_context_creator --from local:admin --to <sub> --workspace <uuid>
    python -m src.cli.transfer_context_creator --from local:admin --to <sub> --workspace <uuid> --apply --yes
    python -m src.cli.transfer_context_creator --from local:admin --to <sub> --workspace <uuid> --apply --yes --repair-payloads

What moves, per live context in the workspace whose ``created_by`` is ``--from``:

* ``contexts.created_by``;
* every memory in it authored by ``--from`` (tombstones included, so a
  restore stays consistent) — ``memories.user_id`` and, for live memories,
  the ``user_id`` field of the vector-store point. A private context shows
  its owner only the memories whose ``user_id`` matches, so without this
  step the new owner would see the context and none of its content;
* every non-Hebbian edge ``--from`` holds in it (``origin != 'hebbian'``:
  declared links, ``supersedes`` / ``contradicts``, sleep-discovered ones)
  — ``neural_memory_edges.user_id`` (#1872). Those edges keep acting after
  the hand-over (supersede shadowing in recall is not user-scoped) while
  listing, updating and deleting them is keyed by the caller, so left
  behind they could only be removed with SQL. ``origin`` is ``NOT NULL``
  with a ``'hebbian'`` server default, so a row from before the column
  existed is Hebbian and stays.

``unique_edge`` is ``(user_id, src_id, dst_id)``, so an edge cannot simply
move onto a pair ``--to`` already holds a row for. The rule follows the edge
upsert's own precedence (``NeuralEdgeRepository.create_or_update_edge``):

* ``--to``'s row is outranked — it is Hebbian (a co-activation weight, which
  any declared or semantic write overwrites), or it is semantic and
  ``--from``'s edge is declared (a user assertion beats a machine guess,
  #1406): it is deleted and ``--from``'s edge takes its place, otherwise a
  declared ``supersedes`` would be lost to a retrieval counter or to a
  sleep-discovered link;
* otherwise (``--to``'s row is declared, or both are semantic) the new owner's
  row is kept as it is and ``--from``'s row is dropped. Skipping it instead
  would leave a row that still shadows recall and that nobody can manage, and
  the command would never reach "nothing left to move".

``--to`` must be the workspace owner or an ``admin`` member — anyone else
could end up owning a private context they cannot list. One ``audit_logs``
row is written per transferred context, with the memory and edge counts.
Running again after ``--apply`` changes 0 rows. Vector-store updates run after the database commit and are
reported if any fail (exit 1): the memory list is already right, recall may
miss those memories until the payload is repaired — re-run with
``--repair-payloads``, which converges: in every context an earlier run
moved to ``--to`` (found by its audit row) it moves any memory still
authored by ``--from``, any non-Hebbian edge ``--from`` still holds (also
the ones a transfer made before #1872 left behind) and re-points the vector
point of every live memory ``--to`` owns (idempotent). Without ``--apply``
the sweep is planned and printed, not written. A context ``--to`` owned all
along is never touched — ``--from`` may legitimately have authored memories
in a shared one.

A memory whose embedding has not succeeded has no vector point yet. Qdrant
answers a payload update for an unknown point id with an error (the LanceDB
store returns silently), so only a failed update of a memory with
``embedding_status == 'success'`` counts as a payload failure; the others
are listed as "skipped, not embedded yet" and do not affect the exit code —
the later embed writes the payload from the row, which already carries the
new ``user_id``.

The command is not fenced against concurrent writes: a ``remember`` by the
``--from`` identity that was authorized before the flip, or an embedding
worker that loaded the old ``user_id``, can land after it. Run it while the
``--from`` identity's clients (the API key, MCP) are idle, then run it once
more with ``--repair-payloads`` to sweep anything that slipped in. Do that
final sweep before the retired account is deleted or erased. Once only the
``users`` row is gone the sweep still runs (``--repair-payloads`` does not
need the ``--from`` user row, only its ``user_id``; its scope stays the audit
rows matching from / to / workspace, so a mistyped ``--from`` finds nothing)
while a plain transfer does not. After an account erasure it finds nothing:
the erasure replaces the ``user_id`` on the memories and edges that outlive
the account with a pseudonym, so no row matches ``--from`` any more.

What this does NOT do: move API keys (mint a new key for ``--to`` if MCP
clients should keep seeing the private contexts), touch other ``created_by``
columns (resources, agents, files, secrets), per-user retrieval history
(Hebbian edge weights, feedback, sleep reports — boosting starts over), or
merge the two user rows.

Exit codes: 0 ok · 1 error (including vector-store update failures).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import (  # noqa: E402
    ColumnElement,
    and_,
    delete,
    exists,
    func,
    not_,
    or_,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402
from sqlalchemy.orm import aliased  # noqa: E402

from auth.workspace_roles import WorkspaceRole  # noqa: E402
from cli._oneshot import add_log_level_argument, configure_logging, run_plan_apply  # noqa: E402
from db.qdrant import update_memory_payload_in_qdrant  # noqa: E402
from models.auth import AuditLog, Context, User, Workspace, WorkspaceMember  # noqa: E402
from models.memory import (  # noqa: E402
    EDGE_ORIGIN_DECLARED,
    EDGE_ORIGIN_HEBBIAN,
    EDGE_ORIGIN_SEMANTIC,
    Memory,
    NeuralMemoryEdge,
)
from services.context_routing import resolve_collection_name  # noqa: E402

AUDIT_ACTION = "context_creator_transferred"
# The "who" of the audit row: there is no session behind a CLI run.
AUDIT_ACTOR_ID = "cli:transfer_context_creator"
AUDIT_ACTOR_EMAIL = "cli@local"
_PAYLOAD_BATCH = 32
# The only embedding_status with a vector point behind it.
_EMBEDDED = "success"


@dataclass(frozen=True)
class EdgeCounts:
    """What happens to ``--from``'s non-Hebbian edges in a set of contexts.

    ``moved`` change hands; ``dropped`` are deleted because ``--to`` already
    holds an edge of equal or higher rank on the pair; ``replaced`` are
    outranked rows of ``--to`` deleted to make room for a moved edge (see the
    module docstring).
    """

    moved: int = 0
    dropped: int = 0
    replaced: int = 0

    @property
    def total(self) -> int:
        """Rows of ``--from`` the run touches (a replaced row belongs to a moved one)."""
        return self.moved + self.dropped


@dataclass(frozen=True)
class PlanLine:
    """One context in scope and how many rows by ``--from`` move with it."""

    context_id: UUID
    name: str
    is_private: bool
    memory_count: int  # every row, tombstones included — what the UPDATE touches
    edges: EdgeCounts = EdgeCounts()


@dataclass
class TransferResult:
    """Outcome of one pass over the contexts in scope."""

    from_user_id: str
    to_user_id: str
    workspace_id: UUID
    dry_run: bool
    lines: list[PlanLine] = field(default_factory=list)
    # --repair-payloads, over the contexts an earlier run moved to ``to``:
    # memories still authored by ``from`` that move, non-Hebbian edges ``from``
    # still holds, and live memories by either identity whose vector payload
    # is re-pointed. All zero without the flag.
    repair_moved: int = 0
    repair_edges: EdgeCounts = EdgeCounts()
    repair_memories: int = 0
    # After a write: embedded memories whose payload update failed (exit 1),
    # and not-yet-embedded ones that have no point to update (reported only).
    payload_failures: list[UUID] = field(default_factory=list)
    payload_skipped: list[UUID] = field(default_factory=list)

    @property
    def transferred(self) -> int:
        return len(self.lines)

    @property
    def transferred_ids(self) -> list[UUID]:
        return [line.context_id for line in self.lines]

    @property
    def memories(self) -> int:
        return sum(line.memory_count for line in self.lines)

    @property
    def edges_moved(self) -> int:
        """Edges that change hands with the contexts in scope (sweep excluded)."""
        return sum(line.edges.moved for line in self.lines)

    @property
    def edges_touched(self) -> int:
        """Edges of ``from`` the transfer moves or drops (sweep excluded)."""
        return sum(line.edges.total for line in self.lines)

    @property
    def planned(self) -> int:
        """Units of work the run would do: contexts to move plus repairs."""
        return self.transferred + self.repair_moved + self.repair_edges.total + self.repair_memories

    def summary(self) -> str:
        """What the run changes, by kind, for the prompt and the report line."""
        parts = [
            (self.transferred, "context(s)"),
            (self.memories + self.repair_moved, "memory row(s)"),
            # Dropped duplicates are rows the run deletes: they count as changed.
            (self.edges_touched + self.repair_edges.total, "edge(s)"),
            (self.repair_memories, "vector payload repair(s)"),
        ]
        return ", ".join(f"{count} {label}" for count, label in parts if count) or "nothing"


async def _require_owner_or_admin(db: AsyncSession, *, workspace_id: UUID, user_id: str) -> None:
    """Refuse a target that could not list a private context it would own.

    Members and viewers are subject to suspension and ``allowed_context_ids``
    whitelists (``PermissionService``); only the workspace owner and admin
    members see every context unconditionally.
    """
    owner = await db.scalar(select(Workspace.owner_user_id).where(Workspace.id == workspace_id))
    if owner == user_id:
        return
    role = await db.scalar(
        select(WorkspaceMember.role).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == user_id,
        )
    )
    if role in (WorkspaceRole.OWNER, WorkspaceRole.ADMIN):
        return
    raise ValueError(
        f"--to {user_id!r} is not the owner or an admin member of workspace {workspace_id}"
    )


async def _repoint_payloads(
    db: AsyncSession, *, context_ids: list[UUID], to_user_id: str
) -> tuple[list[UUID], list[UUID]]:
    """Set ``user_id`` on the vector point of every live memory now owned by ``to``.

    Runs after the database commit. Every live memory is tried, whatever its
    ``embedding_status`` — a point can exist while the row still says
    ``processing`` — but a failed update only counts for a memory that is
    known to have a point (see the module docstring).

    Returns:
        ``(failed, skipped)``: ids of embedded memories whose update failed,
        and ids of not-yet-embedded memories whose update failed because
        there is no point to update.
    """
    failed: list[UUID] = []
    skipped: list[UUID] = []
    for context_id in context_ids:
        collection = await resolve_collection_name(db, context_id)
        memories = [
            (row.id, row.embedding_status)
            for row in await db.execute(
                select(Memory.id, Memory.embedding_status).where(
                    Memory.context_id == context_id,
                    Memory.user_id == to_user_id,
                    Memory.deleted_at.is_(None),
                )
            )
        ]
        for start in range(0, len(memories), _PAYLOAD_BATCH):
            batch = memories[start : start + _PAYLOAD_BATCH]
            outcomes = await asyncio.gather(
                *(
                    update_memory_payload_in_qdrant(
                        memory_id=memory_id,
                        payload_updates={"user_id": to_user_id},
                        collection_name=collection,
                    )
                    for memory_id, _ in batch
                ),
                return_exceptions=True,
            )
            for (memory_id, status), outcome in zip(batch, outcomes, strict=True):
                if isinstance(outcome, BaseException):
                    (failed if status == _EMBEDDED else skipped).append(memory_id)
    return failed, skipped


def _edges_to_move(context_ids: list[UUID], from_user_id: str) -> list:
    """WHERE clauses selecting ``from``'s non-Hebbian edges in the contexts.

    ``origin`` is NOT NULL (server default ``'hebbian'``), so ``!=`` needs no
    NULL arm.
    """
    return [
        NeuralMemoryEdge.context_id.in_(context_ids),
        NeuralMemoryEdge.user_id == from_user_id,
        NeuralMemoryEdge.origin != EDGE_ORIGIN_HEBBIAN,
    ]


def _outranked(theirs: type[NeuralMemoryEdge], mine: type[NeuralMemoryEdge]) -> ColumnElement[bool]:
    """``theirs`` (a row of ``to``) gives way to ``mine`` (the edge that moves).

    The edge upsert's precedence: anything non-Hebbian overwrites a Hebbian
    row, and a declared edge overwrites a semantic one. ``mine`` is never
    Hebbian here.
    """
    return or_(
        theirs.origin == EDGE_ORIGIN_HEBBIAN,
        and_(mine.origin == EDGE_ORIGIN_DECLARED, theirs.origin == EDGE_ORIGIN_SEMANTIC),
    )


async def _count_edges(
    db: AsyncSession, *, context_ids: list[UUID], from_user_id: str, to_user_id: str
) -> EdgeCounts:
    """Plan the edge move for the contexts — read-only."""
    if not context_ids:
        return EdgeCounts()
    theirs = aliased(NeuralMemoryEdge)
    # unique_edge spans contexts, so the pair is matched without a context filter.
    same_pair = [
        theirs.user_id == to_user_id,
        theirs.src_id == NeuralMemoryEdge.src_id,
        theirs.dst_id == NeuralMemoryEdge.dst_id,
    ]
    outranked = _outranked(theirs, NeuralMemoryEdge)
    kept = exists().where(*same_pair, not_(outranked))
    gives_way = exists().where(*same_pair, outranked)
    total, dropped, replaced = (
        await db.execute(
            select(
                func.count(),
                func.count().filter(kept),
                func.count().filter(gives_way),
            )
            .select_from(NeuralMemoryEdge)
            .where(*_edges_to_move(context_ids, from_user_id))
        )
    ).one()
    return EdgeCounts(moved=total - dropped, dropped=dropped, replaced=replaced)


async def _move_edges(
    db: AsyncSession, *, context_ids: list[UUID], from_user_id: str, to_user_id: str
) -> None:
    """Hand ``from``'s non-Hebbian edges in the contexts to ``to`` (not committed).

    Order matters: first make room where ``to``'s row is outranked, then drop
    ``from``'s duplicates of the pairs ``to`` still holds an edge on, then
    move what is left — by then no ``(to, src, dst)`` can collide.
    """
    mine = aliased(NeuralMemoryEdge)
    await db.execute(
        delete(NeuralMemoryEdge)
        .where(
            NeuralMemoryEdge.user_id == to_user_id,
            exists().where(
                mine.context_id.in_(context_ids),
                mine.user_id == from_user_id,
                mine.origin != EDGE_ORIGIN_HEBBIAN,
                mine.src_id == NeuralMemoryEdge.src_id,
                mine.dst_id == NeuralMemoryEdge.dst_id,
                _outranked(NeuralMemoryEdge, mine),
            ),
        )
        .execution_options(synchronize_session=False)
    )
    theirs = aliased(NeuralMemoryEdge)
    await db.execute(
        delete(NeuralMemoryEdge)
        .where(
            *_edges_to_move(context_ids, from_user_id),
            exists().where(
                theirs.user_id == to_user_id,
                theirs.src_id == NeuralMemoryEdge.src_id,
                theirs.dst_id == NeuralMemoryEdge.dst_id,
            ),
        )
        .execution_options(synchronize_session=False)
    )
    await db.execute(
        update(NeuralMemoryEdge)
        .where(*_edges_to_move(context_ids, from_user_id))
        # A change of owner is not a change of the edge: keep last_updated
        # (the column's onupdate would otherwise stamp it).
        .values(user_id=to_user_id, last_updated=NeuralMemoryEdge.last_updated)
        .execution_options(synchronize_session=False)
    )


async def _previously_transferred(
    db: AsyncSession, *, from_user_id: str, to_user_id: str, workspace_id: UUID
) -> list[UUID]:
    """Live contexts an earlier run of this command moved from ``from`` to ``to``.

    Identified by their audit rows, so the repair pass never touches a context
    ``to`` owned all along — ``from`` may legitimately have authored memories
    in a shared one, and those must keep their author.
    """
    rows = (
        await db.execute(
            select(AuditLog.resource).where(
                AuditLog.action == AUDIT_ACTION,
                AuditLog.resource.like("context:%"),
                AuditLog.user_metadata["from_user_id"].as_string() == from_user_id,
                AuditLog.user_metadata["to_user_id"].as_string() == to_user_id,
                AuditLog.user_metadata["workspace_id"].as_string() == str(workspace_id),
            )
        )
    ).scalars()
    moved: list[UUID] = []
    for resource in rows:
        try:
            moved.append(UUID(resource.removeprefix("context:")))
        except ValueError:
            continue
    if not moved:
        return []
    live = (
        await db.execute(
            select(Context.id).where(
                Context.id.in_(moved),
                Context.workspace_id == workspace_id,
                Context.created_by == to_user_id,
                Context.deleted_at.is_(None),
            )
        )
    ).scalars()
    return list(live)


async def transfer_context_creator(
    db: AsyncSession,
    *,
    from_user_id: str,
    to_user_id: str,
    workspace_id: UUID,
    dry_run: bool = True,
    repair_payloads: bool = False,
) -> TransferResult:
    """Move the workspace's live contexts, their memories and edges, between two users.

    Args:
        db: Async session; committed only when ``dry_run`` is False and at
            least one context moved.
        from_user_id: The ``users.user_id`` the contexts are attributed to now.
        to_user_id: The ``users.user_id`` that should own them — the
            workspace owner or an admin member.
        workspace_id: The workspace whose contexts are in scope.
        dry_run: Plan only — nothing is written.
        repair_payloads: Also sweep the contexts an earlier run moved to
            ``to`` (per their audit rows): move memories still authored by
            ``from`` and the non-Hebbian edges it still holds, and re-point
            the vector payloads of every live memory ``to`` owns there — the
            re-run path after a payload failure or a late write. The
            ``from`` user row may be gone by then, so it is not required.

    Returns:
        TransferResult with one PlanLine per context in scope and, after a
        write, the memory ids whose vector payload could not be updated
        (``payload_failures``) or had no point yet (``payload_skipped``).

    Raises:
        ValueError: ``from`` equals ``to``, ``to`` or the workspace does not
            exist, ``from`` does not exist and ``repair_payloads`` is False,
            or ``to`` is not the workspace owner / an admin member.
    """
    if from_user_id == to_user_id:
        raise ValueError("--from and --to name the same user")
    users = set(
        (await db.execute(select(User.user_id).where(User.user_id.in_([from_user_id, to_user_id]))))
        .scalars()
        .all()
    )
    # The sweep is keyed by audit rows and the ``user_id`` string, so it has
    # to keep working after the retired account's row was removed (#1872). A
    # plain transfer keeps the check: a typo must not read as "nothing to do".
    required = (to_user_id,) if repair_payloads else (from_user_id, to_user_id)
    for user_id in required:
        if user_id not in users:
            raise ValueError(f"no user with user_id {user_id!r}")
    if await db.scalar(select(Workspace.id).where(Workspace.id == workspace_id)) is None:
        raise ValueError(f"no workspace {workspace_id}")
    await _require_owner_or_admin(db, workspace_id=workspace_id, user_id=to_user_id)

    stmt = (
        select(Context)
        .where(
            Context.workspace_id == workspace_id,
            Context.created_by == from_user_id,
            Context.deleted_at.is_(None),
        )
        .order_by(Context.created_at, Context.id)
    )
    contexts = list((await db.execute(stmt)).scalars().all())

    result = TransferResult(
        from_user_id=from_user_id,
        to_user_id=to_user_id,
        workspace_id=workspace_id,
        dry_run=dry_run,
    )
    for context in contexts:
        memory_count = (
            await db.scalar(
                select(func.count())
                .select_from(Memory)
                .where(Memory.context_id == context.id, Memory.user_id == from_user_id)
            )
            or 0
        )
        edges = await _count_edges(
            db, context_ids=[context.id], from_user_id=from_user_id, to_user_id=to_user_id
        )
        result.lines.append(
            PlanLine(
                context_id=context.id,
                name=context.name,
                is_private=context.is_private,
                memory_count=memory_count,
                edges=edges,
            )
        )
        if dry_run:
            continue
        context.created_by = to_user_id
        # Tombstoned rows move too, so a restore keeps the context consistent.
        await db.execute(
            update(Memory)
            .where(Memory.context_id == context.id, Memory.user_id == from_user_id)
            .values(user_id=to_user_id)
        )
        if edges.total:
            await _move_edges(
                db, context_ids=[context.id], from_user_id=from_user_id, to_user_id=to_user_id
            )
        db.add(
            AuditLog(
                user_email=AUDIT_ACTOR_EMAIL,
                user_id=AUDIT_ACTOR_ID,
                action=AUDIT_ACTION,
                resource=f"context:{context.id}",
                user_metadata={
                    "from_user_id": from_user_id,
                    "to_user_id": to_user_id,
                    "workspace_id": str(workspace_id),
                    "memories": memory_count,
                    "edges": edges.moved,
                    "edges_dropped": edges.dropped,
                    "edges_replaced": edges.replaced,
                },
            )
        )

    repair_scope: list[UUID] = []
    if repair_payloads:
        repair_scope = [
            cid
            for cid in await _previously_transferred(
                db, from_user_id=from_user_id, to_user_id=to_user_id, workspace_id=workspace_id
            )
            if cid not in result.transferred_ids
        ]
        if repair_scope:
            # Late writes by ``from`` (see the module docstring) — swept here.
            result.repair_moved = (
                await db.scalar(
                    select(func.count())
                    .select_from(Memory)
                    .where(Memory.context_id.in_(repair_scope), Memory.user_id == from_user_id)
                )
                or 0
            )
            result.repair_edges = await _count_edges(
                db, context_ids=repair_scope, from_user_id=from_user_id, to_user_id=to_user_id
            )
            # Live rows by either identity: the swept ones are owned by ``to``
            # by the time the payload pass runs, so they are re-pointed too.
            result.repair_memories = (
                await db.scalar(
                    select(func.count())
                    .select_from(Memory)
                    .where(
                        Memory.context_id.in_(repair_scope),
                        Memory.user_id.in_([from_user_id, to_user_id]),
                        Memory.deleted_at.is_(None),
                    )
                )
                or 0
            )

    if dry_run:
        return result
    swept = bool(repair_scope) and bool(result.repair_moved or result.repair_edges.total)
    if swept:
        if result.repair_moved:
            await db.execute(
                update(Memory)
                .where(Memory.context_id.in_(repair_scope), Memory.user_id == from_user_id)
                .values(user_id=to_user_id)
            )
        if result.repair_edges.total:
            await _move_edges(
                db, context_ids=repair_scope, from_user_id=from_user_id, to_user_id=to_user_id
            )
        db.add(
            AuditLog(
                user_email=AUDIT_ACTOR_EMAIL,
                user_id=AUDIT_ACTOR_ID,
                action=AUDIT_ACTION,
                resource=f"workspace:{workspace_id}",
                user_metadata={
                    "from_user_id": from_user_id,
                    "to_user_id": to_user_id,
                    "workspace_id": str(workspace_id),
                    "memories": result.repair_moved,
                    "edges": result.repair_edges.moved,
                    "edges_dropped": result.repair_edges.dropped,
                    "edges_replaced": result.repair_edges.replaced,
                    "sweep": True,
                },
            )
        )
    if result.transferred or swept:
        await db.commit()
    payload_scope = list(result.transferred_ids) + repair_scope
    if payload_scope:
        try:
            result.payload_failures, result.payload_skipped = await _repoint_payloads(
                db, context_ids=payload_scope, to_user_id=to_user_id
            )
        except Exception as exc:
            # The database write is committed; only the vector payloads are
            # behind. Say so instead of looking like the whole run failed.
            raise RuntimeError(
                "database write committed, but the vector-store update stopped "
                f"({exc}); re-run with --repair-payloads"
            ) from exc
    return result


def _edge_note(edges: EdgeCounts) -> str:
    """The edge part of a plan line; duplicates and replaced rows only when present."""
    note = f"{edges.moved} edge(s)"
    extras = []
    if edges.dropped:
        extras.append(f"{edges.dropped} duplicate(s) dropped")
    if edges.replaced:
        extras.append(f"{edges.replaced} outranked row(s) of --to replaced")
    return f"{note} [{', '.join(extras)}]" if extras else note


def _print_plan(result: TransferResult) -> None:
    print(
        f"workspace {result.workspace_id}: created_by {result.from_user_id!r} -> "
        f"{result.to_user_id!r}"
    )
    for line in result.lines:
        visibility = "private" if line.is_private else "shared"
        print(
            f"  {line.context_id}  {visibility:7} {line.name}  "
            f"({line.memory_count} memor{'y' if line.memory_count == 1 else 'ies'}, "
            f"{_edge_note(line.edges)})"
        )
    verb = "would transfer" if result.dry_run else "transferred"
    print(
        f"{verb} {result.transferred} context(s), {result.memories} memory row(s) "
        f"(tombstones included), {result.edges_moved} non-Hebbian edge(s)"
    )
    if result.repair_moved:
        verb = "would move" if result.dry_run else "moved"
        print(
            f"repair: {verb} {result.repair_moved} memory row(s) still authored by "
            f"{result.from_user_id!r} in contexts an earlier run moved to {result.to_user_id!r}"
        )
    if result.repair_edges.total:
        verb = "would move" if result.dry_run else "moved"
        print(
            f"repair: {verb} {_edge_note(result.repair_edges)} still held by "
            f"{result.from_user_id!r} in contexts an earlier run moved to {result.to_user_id!r}"
        )
    if result.repair_memories:
        verb = "would re-point" if result.dry_run else "re-pointed"
        print(
            f"repair: {verb} the vector payload of {result.repair_memories} live memor(ies) "
            f"{result.to_user_id!r} already owns"
        )
    if result.payload_skipped:
        print(
            f"skipped {len(result.payload_skipped)} memor(ies) not embedded yet — no vector "
            "point to update; the embed writes the new user_id:"
        )
        for memory_id in result.payload_skipped:
            print(f"  {memory_id}")
    if result.payload_failures:
        print(
            f"vector payload NOT updated for {len(result.payload_failures)} memor(ies) — "
            "recall may miss them until repaired:",
            file=sys.stderr,
        )
        for memory_id in result.payload_failures:
            print(f"  {memory_id}", file=sys.stderr)


async def _main(args: argparse.Namespace) -> int:
    configure_logging(args.log_level)
    outcome: dict[str, TransferResult] = {}

    async def run(db: AsyncSession, dry_run: bool) -> TransferResult:
        result = await transfer_context_creator(
            db,
            from_user_id=args.from_user,
            to_user_id=args.to_user,
            workspace_id=args.workspace,
            dry_run=dry_run,
            repair_payloads=args.repair_payloads,
        )
        if not dry_run:
            outcome["applied"] = result
        return result

    code = await run_plan_apply(
        run=run,
        print_plan=_print_plan,
        changes=lambda result: result.planned,
        noun="row",
        summary=TransferResult.summary,
        apply=args.apply,
        assume_yes=args.yes,
    )
    applied = outcome.get("applied")
    if code == 0 and applied and (applied.payload_failures or applied.payload_skipped):
        _print_plan(applied)
        # Only an embedded memory's failed update is an error; a skipped one
        # has no point yet and gets the new user_id when it is embedded.
        return 1 if applied.payload_failures else 0
    return code


def _parse(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--from", dest="from_user", required=True, help="users.user_id the contexts belong to now"
    )
    parser.add_argument(
        "--to",
        dest="to_user",
        required=True,
        help="users.user_id that should own them (workspace owner or admin member)",
    )
    parser.add_argument("--workspace", type=UUID, required=True, help="workspace in scope")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--plan", action="store_true", help="print what would change (default, read-only)"
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="re-point created_by, memories and non-Hebbian edges, write audit rows",
    )
    parser.add_argument("--yes", action="store_true", help="no confirmation prompt")
    add_log_level_argument(parser)
    parser.add_argument(
        "--repair-payloads",
        action="store_true",
        help="also sweep the contexts an earlier run moved to --to (planned without --apply, "
        "written with it): move memories still authored by --from and non-Hebbian edges it "
        "still holds, and re-point the vector payloads of every live memory --to owns (the "
        "re-run path after a payload failure or a late write; the --from user row may "
        "already be gone)",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(_parse())))
