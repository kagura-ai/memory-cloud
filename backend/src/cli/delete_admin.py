"""Delete password admin user (for re-creation).

Issue #51: Password + MFA login for initial admin.
#2352: a connector's live write key the admin holds in a workspace someone
else owns is handed to that owner instead of being deleted.
#2371: when other users exist, the live workspaces the admin owns go to
``--transfer-to`` first, so no workspace is left with a deleted owner and the
connector keys there follow the new owner. A workspace other users are in
needs ``--transfer-to`` to be one of its members; one only the admin is in is
handed over as the break-glass transfer does. Without ``--transfer-to`` the
delete is refused with the workspaces listed. The admin's memberships and its
soft-deleted workspaces are deleted, as the admin user delete does.

Usage:
    cd backend && python -m src.cli.delete_admin [--transfer-to USER] [--yes]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import delete, func, or_, select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from cli._env_guard import require_settings  # noqa: E402
from cli._oneshot import confirm  # noqa: E402
from db.base import get_db  # noqa: E402
from models.auth import APIKey, User, Workspace, WorkspaceMember  # noqa: E402
from services.connector_key_ownership import reown_connector_keys  # noqa: E402
from services.workspace_ownership_service import WorkspaceOwnershipService  # noqa: E402

AUDIT_ACTOR_EMAIL = "cli@local"
SOLE_MEMBER_REASON = "cli.delete_admin: the deleted admin was the only member (#2371)"


@dataclass
class _Handover:
    """The admin's live workspaces, split by who else is in them."""

    shared: list[Workspace]  # other members: the target must be one of them
    sole: list[Workspace]  # only the admin: handed over to the target


async def _password_admin(db: AsyncSession) -> User | None:
    return (
        await db.execute(select(User).where(User.auth_method == "password", User.role == "admin"))
    ).scalar_one_or_none()


async def _owned_workspaces(db: AsyncSession, user_id: str) -> list[Workspace]:
    """The live (not soft-deleted) workspaces ``user_id`` owns."""
    return list(
        (
            await db.execute(
                select(Workspace)
                .where(Workspace.owner_user_id == user_id, Workspace.deleted_at.is_(None))
                .order_by(Workspace.id)
            )
        )
        .scalars()
        .all()
    )


async def _resolve_target(db: AsyncSession, ident: str, admin: User) -> User | str:
    """The user ``--transfer-to`` names (user_id, login_id or email, the email
    case-insensitive as sign-in compares it), or why there is none."""
    users = (
        (
            await db.execute(
                select(User).where(
                    or_(
                        User.user_id == ident,
                        User.login_id == ident,
                        func.lower(User.email) == ident.lower(),
                    ),
                    User.user_id != admin.user_id,
                )
            )
        )
        .scalars()
        .all()
    )
    if not users:
        return f"--transfer-to {ident}: no such user (other than the admin)."
    if len(users) > 1:
        ids = ", ".join(sorted(u.user_id for u in users))
        return f"--transfer-to {ident} matches {len(users)} users ({ids}); pass the user_id."
    return users[0]


async def _split(db: AsyncSession, workspaces: list[Workspace], admin_id: str) -> _Handover:
    others = set(
        (
            await db.execute(
                select(WorkspaceMember.workspace_id).where(
                    WorkspaceMember.workspace_id.in_([ws.id for ws in workspaces]),
                    WorkspaceMember.user_id != admin_id,
                )
            )
        )
        .scalars()
        .all()
    )
    return _Handover(
        shared=[ws for ws in workspaces if ws.id in others],
        sole=[ws for ws in workspaces if ws.id not in others],
    )


async def _member_of(db: AsyncSession, workspaces: list[Workspace], user_id: str) -> set:
    return set(
        (
            await db.execute(
                select(WorkspaceMember.workspace_id).where(
                    WorkspaceMember.user_id == user_id,
                    WorkspaceMember.workspace_id.in_([ws.id for ws in workspaces]),
                )
            )
        )
        .scalars()
        .all()
    )


async def _hand_over(db: AsyncSession, handover: _Handover, admin: User, target: User) -> None:
    """Each transfer commits on its own (the services lock the workspace row,
    re-check, bump the epoch and write the audit row): a failure part-way
    leaves every workspace with a live owner, and a re-run picks up the rest."""
    service = WorkspaceOwnershipService(db)
    for ws in handover.shared:
        await service.transfer_ownership(
            workspace_id=ws.id,
            current_owner_id=admin.user_id,
            target_user_id=target.user_id,
            performed_by_email=AUDIT_ACTOR_EMAIL,
        )
    for ws in handover.sole:
        await service.force_transfer_ownership(
            workspace_id=ws.id,
            target_user_id=target.user_id,
            performed_by_user_id=admin.user_id,
            performed_by_email=AUDIT_ACTOR_EMAIL,
            reason=SOLE_MEMBER_REASON,
        )


async def _delete_admin_rows(db: AsyncSession, admin: User) -> None:
    """Hand the admin's live connector keys to their workspace owners (#2352),
    then delete its other keys, its memberships, the workspaces it still owns
    and the user row, in one transaction."""
    moved = await reown_connector_keys(db, admin.user_id)
    if moved:
        print(f"  ✓ Connector write keys handed to their workspace owners: {moved}")
    await db.execute(delete(APIKey).where(APIKey.user_id == admin.user_id))
    await db.execute(delete(WorkspaceMember).where(WorkspaceMember.user_id == admin.user_id))
    await db.execute(delete(Workspace).where(Workspace.owner_user_id == admin.user_id))
    await db.delete(admin)
    await db.commit()


def _print_workspaces(workspaces: list[Workspace]) -> None:
    for ws in workspaces:
        print(f"    - {ws.name} ({ws.id})")


async def delete_password_admin(
    db: AsyncSession, *, transfer_to: str | None, ask: Callable[[str], bool]
) -> int:
    """Delete the password admin; returns the exit code.

    Args:
        db: The session.
        transfer_to: ``--transfer-to``: who takes over the admin's workspaces.
        ask: Asks the operator a yes/no question.
    """
    admin = await _password_admin(db)
    if admin is None:
        print("\n✗ No password admin found.")
        return 1

    total_users = (await db.execute(select(func.count()).select_from(User))).scalar() or 0
    print(f"\n  Login ID: {admin.login_id}")
    print(f"  Total users: {total_users}")

    if total_users <= 1:
        if not ask("  Delete admin, workspace, and all API keys?"):
            print("  Cancelled.")
            return 0
        await _delete_admin_rows(db, admin)
        print("\n✓ Admin, workspace, and API keys deleted.")
        print("  Run: python -m src.cli.create_admin")
        return 0

    owned = await _owned_workspaces(db, admin.user_id)
    handover: tuple[_Handover, User] | None = None
    if owned:
        if transfer_to is None:
            print("\n✗ The admin owns workspaces:")
            _print_workspaces(owned)
            print("  Re-run with --transfer-to USER to hand them over.")
            return 1
        target = await _resolve_target(db, transfer_to, admin)
        if isinstance(target, str):
            print(f"\n✗ {target}")
            return 1
        split = await _split(db, owned, admin.user_id)
        member_of = await _member_of(db, split.shared, target.user_id)
        outside = [ws for ws in split.shared if ws.id not in member_of]
        if outside:
            print(f"\n✗ {target.user_id} is not a member of these workspaces other users are in:")
            _print_workspaces(outside)
            print("  Add them as a member first. Nothing was changed.")
            return 1
        if split.shared:
            print(f"  Workspaces to transfer to {target.user_id}:")
            _print_workspaces(split.shared)
        if split.sole:
            print(f"  Only the admin is in these; they are handed to {target.user_id} too:")
            _print_workspaces(split.sole)
        handover = (split, target)

    print("  ⚠ Other users exist. Workspaces will be preserved.")
    if not ask("  Delete admin user only?"):
        print("  Cancelled.")
        return 0

    if handover is not None:
        split, target = handover
        await _hand_over(db, split, admin, target)
        print(f"  ✓ Workspaces transferred to {target.user_id}: {len(owned)}")
        # One the admin created while the prompt waited is not in the list.
        left = await _owned_workspaces(db, admin.user_id)
        if left:
            print("\n✗ The admin still owns workspaces; re-run to hand them over:")
            _print_workspaces(left)
            return 1

    await _delete_admin_rows(db, admin)
    print("\n✓ Admin user and API keys deleted (workspaces preserved).")
    print("  Run: python -m src.cli.create_admin")
    return 0


async def _main(args: argparse.Namespace) -> int:
    print("=" * 50)
    print("Kagura Memory Cloud - Delete Password Admin")
    print("=" * 50)

    # The production secret guard (#2239) before any DB work.
    require_settings()

    code = 0
    try:
        async for db in get_db():
            code = await delete_password_admin(
                db,
                transfer_to=args.transfer_to,
                ask=lambda prompt: confirm(prompt, args.yes),
            )
    except Exception as exc:  # noqa: BLE001 - CLI boundary: report and exit non-zero
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return code


def _parse(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--transfer-to",
        metavar="USER",
        help="user_id, login_id or email of the user who takes over the workspaces "
        "the admin owns (required when it owns any and other users exist)",
    )
    parser.add_argument("--yes", action="store_true", help="no confirmation prompt")
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(_parse())))
