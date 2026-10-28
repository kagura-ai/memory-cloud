"""Delete password admin user (for re-creation).

Issue #51: Password + MFA login for initial admin.
#2352: a connector's live write key the admin holds in a workspace someone
else owns is handed to that owner instead of being deleted.
#2371: when other users exist, no workspace is left with a deleted owner. A
live workspace the admin owns that other users are in goes to
``--transfer-to`` (one of its members) first, so its connector keys follow
the new owner; without it the delete is refused with the workspaces listed.
One only the admin is in is deleted with it, as account erasure does. A
workspace under a running subscription stops the command (it is the admin's
subscription, as for account erasure); so does any change to the admin's
workspaces or the user count while the prompt waits.
#2417: a private context the admin created passes to a linked account that
wrote in it (#1784); one nobody else wrote in is deleted with the admin, files
included. Every other context the admin created in a surviving workspace (a
shared one, a connector's write target, a private one someone else wrote in)
passes to that workspace's owner; anything in a soft-deleted workspace the
admin owns stays with it (#2406).

Usage:
    cd backend && python -m src.cli.delete_admin [--transfer-to USER] [--yes]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import func, or_, select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from cli._env_guard import require_settings  # noqa: E402
from cli._oneshot import confirm  # noqa: E402
from db.base import get_db  # noqa: E402
from models.auth import User, Workspace, WorkspaceMember  # noqa: E402
from services.billing_contract import owned_workspaces_under_contract  # noqa: E402
from services.password_account_service import normalize_email  # noqa: E402
from services.workspace_ownership_service import WorkspaceOwnershipService  # noqa: E402
from utils.logger import get_logger  # noqa: E402

AUDIT_ACTOR_EMAIL = "cli@local"

logger = get_logger(__name__)


@dataclass(frozen=True)
class _Ws:
    id: UUID
    name: str


async def _password_admin(db: AsyncSession) -> User | None:
    return (
        await db.execute(select(User).where(User.auth_method == "password", User.role == "admin"))
    ).scalar_one_or_none()


async def _owned_workspaces(db: AsyncSession, user_id: str, *, live: bool = True) -> list[_Ws]:
    """The live (or, with ``live=False``, soft-deleted) workspaces ``user_id`` owns."""
    deleted = Workspace.deleted_at.is_(None) if live else Workspace.deleted_at.is_not(None)
    rows = await db.execute(
        select(Workspace.id, Workspace.name)
        .where(Workspace.owner_user_id == user_id, deleted)
        .order_by(Workspace.id)
    )
    return [_Ws(ws_id, name) for ws_id, name in rows.all()]


async def _members(db: AsyncSession, workspace_ids: list[UUID]) -> dict[UUID, set[str]]:
    rows = await db.execute(
        select(WorkspaceMember.workspace_id, WorkspaceMember.user_id).where(
            WorkspaceMember.workspace_id.in_(workspace_ids)
        )
    )
    members: dict[UUID, set[str]] = defaultdict(set)
    for ws_id, user_id in rows.all():
        members[ws_id].add(user_id)
    return members


async def _split(
    db: AsyncSession, admin_id: str
) -> tuple[list[_Ws], list[_Ws], dict[UUID, set[str]]]:
    """The admin's live workspaces other users are in, those only it is in,
    and every one's members."""
    owned = await _owned_workspaces(db, admin_id)
    members = await _members(db, [ws.id for ws in owned])
    shared = [ws for ws in owned if members[ws.id] - {admin_id}]
    sole = [ws for ws in owned if not members[ws.id] - {admin_id}]
    return shared, sole, members


async def _resolve_target(db: AsyncSession, ident: str, admin_id: str) -> str:
    """The user_id ``--transfer-to`` names (user_id, login_id or email, the
    email compared as sign-in does), or raises ``LookupError`` with why not."""
    ident = ident.strip()
    user_ids = (
        (
            await db.execute(
                select(User.user_id).where(
                    or_(
                        User.user_id == ident,
                        User.login_id == ident,
                        func.lower(User.email) == normalize_email(ident),
                    ),
                    User.user_id != admin_id,
                )
            )
        )
        .scalars()
        .all()
    )
    if not user_ids:
        raise LookupError(f"--transfer-to {ident}: no such user (other than the admin).")
    if len(user_ids) > 1:
        ids = ", ".join(sorted(user_ids))
        raise LookupError(f"--transfer-to {ident} matches {len(user_ids)} users ({ids}).")
    return user_ids[0]


async def _transfer(
    db: AsyncSession, shared: list[_Ws], admin_id: str, targets: dict[UUID, str]
) -> None:
    """Each transfer commits on its own (the service locks the workspace row,
    re-checks the owner and the target's membership, bumps the epoch and
    writes the audit row): a failure part-way leaves every workspace with a
    live owner, and a re-run picks up the rest."""
    from services.ownership_notifications import notify_new_owner_best_effort

    service = WorkspaceOwnershipService(db)
    for ws in shared:
        await service.transfer_ownership(
            workspace_id=ws.id,
            current_owner_id=admin_id,
            target_user_id=targets[ws.id],
            performed_by_email=AUDIT_ACTOR_EMAIL,
        )
        # Notify each committed transfer even if a later workspace fails.
        await notify_new_owner_best_effort(db, ws.id, targets[ws.id])
        # The notification only reads. End its transaction, including a failed
        # lookup, before the next transfer or deletion reuses this session.
        await db.rollback()


async def _delete_admin_rows(
    db: AsyncSession,
    admin_id: str,
    workspace_ids: list[UUID],
    transferred: list[UUID] | None = None,
) -> None:
    """Hand the admin's live connector keys to their workspace owners (#2352),
    then delete its other keys, its memberships, ``workspace_ids`` and the user
    row, in one transaction."""
    from services.user_deletion_service import delete_user_rows, finish_user_deletion

    result = await delete_user_rows(
        db, admin_id, delete_workspace_ids=workspace_ids, preserve_created_contexts=True
    )
    await db.commit()
    await finish_user_deletion(result)
    if result.connector_api_keys:
        print(
            f"  ✓ Connector write keys handed to their workspace owners: {result.connector_api_keys}"
        )
    logger.info(
        "cli_delete_admin",
        user_id=admin_id,
        workspaces_transferred=[str(ws_id) for ws_id in transferred or []],
        workspaces_deleted=[str(ws_id) for ws_id in workspace_ids],
        contexts_deleted=result.contexts,
        connector_api_keys_reowned=result.connector_api_keys,
    )


def _print_workspaces(workspaces: list[_Ws]) -> None:
    for ws in workspaces:
        print(f"    - {ws.name} ({ws.id})")


async def _user_count(db: AsyncSession) -> int:
    return (await db.execute(select(func.count()).select_from(User))).scalar() or 0


async def _refuse_billing_contracts(db: AsyncSession, admin_id: str) -> bool:
    """Print and return True when a live workspace the admin owns has a
    running subscription: it is the admin's, so it must end first."""
    under_contract = await owned_workspaces_under_contract(db, admin_id)
    if not under_contract:
        return False
    print("\n✗ These workspaces have a running subscription; end it first:")
    _print_workspaces([_Ws(ws.id, ws.name) for ws in under_contract])
    return True


async def _delete_solo_admin(db: AsyncSession, admin_id: str, ask: Callable[[str], bool]) -> int:
    await db.rollback()  # no transaction stays open while the prompt waits
    if not ask("  Delete admin, workspace, and all API keys?"):
        print("  Cancelled.")
        return 0
    # Someone who signed up while the prompt waited may have joined a workspace
    # the delete below would take with it.
    if await _user_count(db) > 1:
        await db.rollback()
        print("\n✗ Another user signed up while the prompt waited; re-run.")
        return 1
    from services.user_deletion_service import lock_deletion_users

    await lock_deletion_users(db, admin_id)
    owned = await db.execute(select(Workspace.id).where(Workspace.owner_user_id == admin_id))
    await _delete_admin_rows(db, admin_id, list(owned.scalars().all()))
    print("\n✓ Admin, workspace, and API keys deleted.")
    print("  Run: python -m src.cli.create_admin")
    return 0


async def delete_password_admin(
    db: AsyncSession,
    *,
    transfer_to: str | None,
    ask: Callable[[str], bool],
    workspace_transfers: dict[UUID, str] | None = None,
) -> int:
    """Delete the password admin; returns the exit code.

    Args:
        db: The session.
        transfer_to: ``--transfer-to``: who takes over the workspaces the
            admin owns that other users are in.
        ask: Asks the operator a yes/no question.
    """
    admin = await _password_admin(db)
    if admin is None:
        print("\n✗ No password admin found.")
        return 1
    admin_id = admin.user_id

    total_users = await _user_count(db)
    print(f"\n  Login ID: {admin.login_id}")
    print(f"  Total users: {total_users}")

    if await _refuse_billing_contracts(db, admin_id):
        return 1
    if total_users <= 1:
        return await _delete_solo_admin(db, admin_id, ask)

    shared, sole, members = await _split(db, admin_id)

    targets: dict[UUID, str] = {}
    selections = workspace_transfers or {}
    unknown = set(selections) - {ws.id for ws in shared}
    if unknown:
        print("\n✗ --workspace-transfer must name an owned shared workspace.")
        return 1
    for ws in shared:
        ident = selections.get(ws.id, transfer_to)
        if ident is None:
            print("\n✗ The admin owns workspaces other users are in:")
            _print_workspaces(shared)
            print("  Re-run with --transfer-to USER or --workspace-transfer WORKSPACE=USER.")
            return 1
        try:
            target_id = await _resolve_target(db, ident, admin_id)
        except LookupError as exc:
            print(f"\n✗ {exc}")
            return 1
        if target_id not in members[ws.id]:
            print(f"\n✗ {target_id} is not a member of:")
            _print_workspaces([ws])
            print("  Add them as a member first. Nothing was changed.")
            return 1
        targets[ws.id] = target_id
        print(f"  Workspace to transfer to {target_id}: {ws.name} ({ws.id})")
    if sole:
        print("  Only the admin is in these; they are deleted with it:")
        _print_workspaces(sole)
    soft_deleted = await _owned_workspaces(db, admin_id, live=False)
    if soft_deleted:
        print("  Soft-deleted workspaces the admin owns are left as they are:")
        _print_workspaces(soft_deleted)

    await db.rollback()  # no transaction stays open while the prompt waits
    if not ask("  Delete admin user only?"):
        print("  Cancelled.")
        return 0

    # What changed while the prompt waited (a workspace the admin created, a
    # member joining or leaving one, a subscription started) stops the command
    # before anything is written.
    now_shared, now_sole, now_members = await _split(db, admin_id)
    target_left = any(targets[ws.id] not in now_members[ws.id] for ws in shared)
    if now_shared != shared or now_sole != sole or target_left:
        await db.rollback()
        print("\n✗ The admin's workspaces changed while the prompt waited; re-run.")
        return 1
    if await _refuse_billing_contracts(db, admin_id):
        await db.rollback()
        return 1
    await db.rollback()

    if targets:
        await _transfer(db, shared, admin_id, targets)
        for target_id in sorted(set(targets.values())):
            count = sum(value == target_id for value in targets.values())
            print(f"  ✓ Workspaces transferred to {target_id}: {count}")

    # The transfers commit one by one; under the row locks, check once more
    # that the workspaces left to delete are still the admin's alone.
    from services.user_deletion_service import lock_deletion_users

    await lock_deletion_users(db, admin_id)
    sole_ids = [ws.id for ws in sole]
    await db.execute(
        select(Workspace.id)
        .where(Workspace.id.in_(sole_ids))
        .order_by(Workspace.id)
        .with_for_update(nowait=True)
    )
    now_owned = await _owned_workspaces(db, admin_id)
    now_members = await _members(db, sole_ids)
    joined = [ws for ws in sole if now_members[ws.id] - {admin_id}]
    if [ws.id for ws in now_owned] != sole_ids or joined:
        await db.rollback()
        print("\n✗ The admin's workspaces changed while the prompt waited; re-run:")
        _print_workspaces(now_owned)
        return 1

    await _delete_admin_rows(db, admin_id, sole_ids, [ws.id for ws in shared])
    print("\n✓ Admin user and API keys deleted (shared workspaces preserved).")
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
                workspace_transfers=dict(args.workspace_transfer),
                ask=lambda prompt: confirm(prompt, args.yes),
            )
    except Exception as exc:  # noqa: BLE001 - CLI boundary: report and exit non-zero
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return code


def _workspace_transfer(value: str) -> tuple[UUID, str]:
    """Parse an explicit per-workspace successor without shell interpolation."""
    workspace, separator, user = value.partition("=")
    try:
        workspace_id = UUID(workspace)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Expected WORKSPACE_UUID=USER") from exc
    if not separator or not user.strip():
        raise argparse.ArgumentTypeError("Expected WORKSPACE_UUID=USER")
    return workspace_id, user.strip()


def _parse(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--transfer-to",
        metavar="USER",
        help="user_id, login_id or email of the member who takes over the workspaces "
        "the admin owns that other users are in",
    )
    parser.add_argument(
        "--workspace-transfer",
        action="append",
        default=[],
        type=_workspace_transfer,
        metavar="WORKSPACE_UUID=USER",
        help="per-workspace successor; repeat for different workspaces",
    )
    parser.add_argument("--yes", action="store_true", help="no confirmation prompt")
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(_parse())))
