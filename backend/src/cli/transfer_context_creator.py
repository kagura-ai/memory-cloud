"""Re-point ``contexts.created_by`` from one user to another (#1783).

Identities are keyed by ``user_id`` and never linked by email (#481): a CLI
admin (``local:<login>``) and an OAuth sign-in (the IdP ``sub``) are two users
even when they are one person. Contexts created through the CLI admin's API
key then read as another creator in the browser — the "Created by me" filter
is empty and the private ones are hidden — because ``created_by`` is compared
with the session's ``user_id``. This one-shot command moves the ownership of a
workspace's contexts to the identity that should hold it. Runs where the API
runs (same env: DATABASE_URL), e.g. inside the API container::

    python -m src.cli.transfer_context_creator --from local:admin --to <sub> --workspace <uuid>
    python -m src.cli.transfer_context_creator --from local:admin --to <sub> --workspace <uuid> --apply --yes

Only live contexts in the workspace whose ``created_by`` equals ``--from`` are
touched. A context is left alone (``skip``) when ``--to`` is neither a member
nor the owner of the workspace: a private context would otherwise become
visible to nobody. One ``audit_logs`` row is written per transferred context.
Running again after ``--apply`` changes 0 rows.

What this does NOT do: move API keys (mint a new key for ``--to`` if MCP
clients should keep seeing the private contexts), touch other ``created_by``
columns (resources, agents, files, secrets), or merge the two user rows.

Exit codes: 0 ok · 1 error.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from db.base import get_db  # noqa: E402
from models.auth import AuditLog, Context, User, Workspace, WorkspaceMember  # noqa: E402

AUDIT_ACTION = "context_creator_transferred"
# The "who" of the audit row: there is no session behind a CLI run.
AUDIT_ACTOR_ID = "cli:transfer_context_creator"
AUDIT_ACTOR_EMAIL = "cli@local"


@dataclass(frozen=True)
class PlanLine:
    """One context in scope and what the run does with it."""

    context_id: UUID
    name: str
    is_private: bool
    action: str  # "transfer" | "skip"
    reason: str | None = None


@dataclass
class TransferResult:
    """Outcome of one pass over the contexts in scope."""

    from_user_id: str
    to_user_id: str
    workspace_id: UUID
    dry_run: bool
    lines: list[PlanLine] = field(default_factory=list)

    @property
    def scanned(self) -> int:
        return len(self.lines)

    @property
    def transferred(self) -> int:
        return sum(1 for line in self.lines if line.action == "transfer")

    @property
    def skipped(self) -> int:
        return self.scanned - self.transferred

    @property
    def transferred_ids(self) -> list[UUID]:
        return [line.context_id for line in self.lines if line.action == "transfer"]


async def _target_can_see(db: AsyncSession, *, workspace_id: UUID, user_id: str) -> bool:
    """True when ``user_id`` is a member or the owner of ``workspace_id``."""
    owner = await db.scalar(select(Workspace.owner_user_id).where(Workspace.id == workspace_id))
    if owner == user_id:
        return True
    member = await db.scalar(
        select(WorkspaceMember.id).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == user_id,
        )
    )
    return member is not None


async def transfer_context_creator(
    db: AsyncSession,
    *,
    from_user_id: str,
    to_user_id: str,
    workspace_id: UUID,
    dry_run: bool = True,
) -> TransferResult:
    """Move ``created_by`` of the workspace's live contexts from one user to another.

    Args:
        db: Async session; committed only when ``dry_run`` is False and at
            least one context moved.
        from_user_id: The ``users.user_id`` the contexts are attributed to now.
        to_user_id: The ``users.user_id`` that should own them.
        workspace_id: The workspace whose contexts are in scope.
        dry_run: Plan only — nothing is written.

    Returns:
        TransferResult with one PlanLine per context scanned.

    Raises:
        ValueError: ``from`` equals ``to``, either user does not exist, or the
            workspace does not exist.
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
    visible = await _target_can_see(db, workspace_id=workspace_id, user_id=to_user_id)

    result = TransferResult(
        from_user_id=from_user_id,
        to_user_id=to_user_id,
        workspace_id=workspace_id,
        dry_run=dry_run,
    )
    for context in contexts:
        if not visible:
            result.lines.append(
                PlanLine(
                    context_id=context.id,
                    name=context.name,
                    is_private=context.is_private,
                    action="skip",
                    reason="target user is not a member of the workspace",
                )
            )
            continue
        result.lines.append(
            PlanLine(
                context_id=context.id,
                name=context.name,
                is_private=context.is_private,
                action="transfer",
            )
        )
        if not dry_run:
            context.created_by = to_user_id
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
                    },
                )
            )

    if not dry_run and result.transferred:
        await db.commit()
    return result


def _print_plan(result: TransferResult) -> None:
    print(
        f"workspace {result.workspace_id}: created_by {result.from_user_id!r} -> "
        f"{result.to_user_id!r}"
    )
    for line in result.lines:
        visibility = "private" if line.is_private else "shared"
        suffix = f"  ({line.reason})" if line.reason else ""
        print(f"  {line.action:8} {line.context_id}  {visibility:7} {line.name}{suffix}")
    verb = "would transfer" if result.dry_run else "transferred"
    print(f"{result.scanned} context(s): {verb} {result.transferred}, left alone {result.skipped}")


def _confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    answer = input(f"{prompt} [y/N] ").strip().lower()
    return answer in ("y", "yes")


async def _main(args: argparse.Namespace) -> int:
    try:
        async for db in get_db():
            plan = await transfer_context_creator(
                db,
                from_user_id=args.from_user,
                to_user_id=args.to_user,
                workspace_id=args.workspace,
                dry_run=True,
            )
            _print_plan(plan)
            if not args.apply:
                if plan.transferred:
                    print("dry run — pass --apply to write")
                return 0
            if not plan.transferred:
                return 0
            if not _confirm(f"Transfer {plan.transferred} context(s)?", args.yes):
                print("  skipped")
                return 0
            applied = await transfer_context_creator(
                db,
                from_user_id=args.from_user,
                to_user_id=args.to_user,
                workspace_id=args.workspace,
                dry_run=False,
            )
            print(f"transferred {applied.transferred} context(s)")
    except Exception as exc:  # noqa: BLE001 - CLI boundary: report and exit non-zero
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def _parse(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--from", dest="from_user", required=True, help="users.user_id the contexts belong to now"
    )
    parser.add_argument(
        "--to", dest="to_user", required=True, help="users.user_id that should own them"
    )
    parser.add_argument("--workspace", type=UUID, required=True, help="workspace in scope")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--plan", action="store_true", help="print what would change (default, read-only)"
    )
    mode.add_argument(
        "--apply", action="store_true", help="re-point created_by and write audit rows"
    )
    parser.add_argument("--yes", action="store_true", help="no confirmation prompt")
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(_parse())))
