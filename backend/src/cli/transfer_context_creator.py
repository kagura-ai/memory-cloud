"""Move a workspace's contexts (and their memories) from one user to another (#1783).

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

What moves, per live context in the workspace whose ``created_by`` is ``--from``:

* ``contexts.created_by``;
* every memory in it authored by ``--from`` — ``memories.user_id`` and the
  ``user_id`` field of its vector-store point. A private context shows its
  owner only the memories whose ``user_id`` matches, so without this step the
  new owner would see the context and none of its content.

``--to`` must be the workspace owner or an ``admin`` member — anyone else
could end up owning a private context they cannot list. One ``audit_logs``
row is written per transferred context. Running again after ``--apply``
changes 0 rows. Vector-store updates run after the database commit and are
reported if any fail (exit 1): the memory list is already right, recall may
miss those memories until the payload is repaired.

What this does NOT do: move API keys (mint a new key for ``--to`` if MCP
clients should keep seeing the private contexts), touch other ``created_by``
columns (resources, agents, files, secrets), per-user retrieval history
(neural edges, feedback, sleep reports — boosting starts over), or merge the
two user rows.

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

from sqlalchemy import func, select, update  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from auth.workspace_roles import WorkspaceRole  # noqa: E402
from cli._oneshot import run_plan_apply  # noqa: E402
from db.qdrant import update_memory_payload_in_qdrant  # noqa: E402
from models.auth import AuditLog, Context, User, Workspace, WorkspaceMember  # noqa: E402
from models.memory import Memory  # noqa: E402
from services.context_routing import resolve_collection_name  # noqa: E402

AUDIT_ACTION = "context_creator_transferred"
# The "who" of the audit row: there is no session behind a CLI run.
AUDIT_ACTOR_ID = "cli:transfer_context_creator"
AUDIT_ACTOR_EMAIL = "cli@local"
_PAYLOAD_BATCH = 32


@dataclass(frozen=True)
class PlanLine:
    """One context in scope: its live memories by ``--from`` move with it."""

    context_id: UUID
    name: str
    is_private: bool
    memory_count: int


@dataclass
class TransferResult:
    """Outcome of one pass over the contexts in scope."""

    from_user_id: str
    to_user_id: str
    workspace_id: UUID
    dry_run: bool
    lines: list[PlanLine] = field(default_factory=list)
    payload_failures: list[UUID] = field(default_factory=list)

    @property
    def transferred(self) -> int:
        return len(self.lines)

    @property
    def transferred_ids(self) -> list[UUID]:
        return [line.context_id for line in self.lines]

    @property
    def memories(self) -> int:
        return sum(line.memory_count for line in self.lines)


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
) -> list[UUID]:
    """Set ``user_id`` on the vector point of every live memory now owned by ``to``.

    Runs after the database commit; returns the ids whose update failed.
    """
    failed: list[UUID] = []
    for context_id in context_ids:
        collection = await resolve_collection_name(db, context_id)
        memory_ids = list(
            (
                await db.execute(
                    select(Memory.id).where(
                        Memory.context_id == context_id,
                        Memory.user_id == to_user_id,
                        Memory.deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        for start in range(0, len(memory_ids), _PAYLOAD_BATCH):
            batch = memory_ids[start : start + _PAYLOAD_BATCH]
            outcomes = await asyncio.gather(
                *(
                    update_memory_payload_in_qdrant(
                        memory_id=memory_id,
                        payload_updates={"user_id": to_user_id},
                        collection_name=collection,
                    )
                    for memory_id in batch
                ),
                return_exceptions=True,
            )
            failed.extend(
                memory_id
                for memory_id, outcome in zip(batch, outcomes, strict=True)
                if isinstance(outcome, BaseException)
            )
    return failed


async def transfer_context_creator(
    db: AsyncSession,
    *,
    from_user_id: str,
    to_user_id: str,
    workspace_id: UUID,
    dry_run: bool = True,
) -> TransferResult:
    """Move the workspace's live contexts, and their memories, between two users.

    Args:
        db: Async session; committed only when ``dry_run`` is False and at
            least one context moved.
        from_user_id: The ``users.user_id`` the contexts are attributed to now.
        to_user_id: The ``users.user_id`` that should own them — the
            workspace owner or an admin member.
        workspace_id: The workspace whose contexts are in scope.
        dry_run: Plan only — nothing is written.

    Returns:
        TransferResult with one PlanLine per context in scope and, after a
        write, the memory ids whose vector payload could not be updated.

    Raises:
        ValueError: ``from`` equals ``to``, either user or the workspace does
            not exist, or ``to`` is not the workspace owner / an admin member.
    """
    if from_user_id == to_user_id:
        raise ValueError("--from and --to name the same user")
    users = set(
        (await db.execute(select(User.user_id).where(User.user_id.in_([from_user_id, to_user_id]))))
        .scalars()
        .all()
    )
    for user_id in (from_user_id, to_user_id):
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
                .where(
                    Memory.context_id == context.id,
                    Memory.user_id == from_user_id,
                    Memory.deleted_at.is_(None),
                )
            )
            or 0
        )
        result.lines.append(
            PlanLine(
                context_id=context.id,
                name=context.name,
                is_private=context.is_private,
                memory_count=memory_count,
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
                },
            )
        )

    if not dry_run and result.transferred:
        await db.commit()
        result.payload_failures = await _repoint_payloads(
            db, context_ids=result.transferred_ids, to_user_id=to_user_id
        )
    return result


def _print_plan(result: TransferResult) -> None:
    print(
        f"workspace {result.workspace_id}: created_by {result.from_user_id!r} -> "
        f"{result.to_user_id!r}"
    )
    for line in result.lines:
        visibility = "private" if line.is_private else "shared"
        print(
            f"  {line.context_id}  {visibility:7} {line.name}  "
            f"({line.memory_count} memor{'y' if line.memory_count == 1 else 'ies'})"
        )
    verb = "would transfer" if result.dry_run else "transferred"
    print(f"{verb} {result.transferred} context(s), {result.memories} memor(ies)")
    if result.payload_failures:
        print(
            f"vector payload NOT updated for {len(result.payload_failures)} memor(ies) — "
            "recall may miss them until repaired:",
            file=sys.stderr,
        )
        for memory_id in result.payload_failures:
            print(f"  {memory_id}", file=sys.stderr)


async def _main(args: argparse.Namespace) -> int:
    outcome: dict[str, TransferResult] = {}

    async def run(db: AsyncSession, dry_run: bool) -> TransferResult:
        result = await transfer_context_creator(
            db,
            from_user_id=args.from_user,
            to_user_id=args.to_user,
            workspace_id=args.workspace,
            dry_run=dry_run,
        )
        if not dry_run:
            outcome["applied"] = result
        return result

    code = await run_plan_apply(
        run=run,
        print_plan=_print_plan,
        changes=lambda result: result.transferred,
        noun="context",
        apply=args.apply,
        assume_yes=args.yes,
    )
    if code == 0 and outcome.get("applied") and outcome["applied"].payload_failures:
        _print_plan(outcome["applied"])
        return 1
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
        "--apply", action="store_true", help="re-point created_by and memories, write audit rows"
    )
    parser.add_argument("--yes", action="store_true", help="no confirmation prompt")
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(_parse())))
