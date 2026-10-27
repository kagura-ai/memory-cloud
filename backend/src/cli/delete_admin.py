"""Delete password admin user (for re-creation).

Issue #51: Password + MFA login for initial admin.
#2352: a connector's live write key the admin holds in a workspace someone
else owns is handed to that owner instead of being deleted.
#2371: when other users exist, the workspaces the admin owns are handed to
``--transfer-to`` first (a member of each of them); without it the delete is
refused with the workspaces listed. The connector keys there then follow the
new owner (#2352).

Usage:
    cd backend && python -m src.cli.delete_admin [--transfer-to USER]
"""

import argparse
import asyncio
import sys
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import create_engine, delete, func, or_, select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from cli._env_guard import require_settings  # noqa: E402
from cli.db import get_sync_database_url  # noqa: E402
from config.database import to_async_database_url  # noqa: E402
from models.auth import APIKey, User, Workspace, WorkspaceMember  # noqa: E402
from services.connector_key_ownership import reown_connector_keys_sync  # noqa: E402
from services.workspace_ownership_service import WorkspaceOwnershipService  # noqa: E402

AUDIT_ACTOR_EMAIL = "cli@local"


def _delete_api_keys(db: Session, user_id: str) -> None:
    """Hand the user's live connector keys to their workspace owners (#2352),
    then delete the rest of the user's keys."""
    moved = reown_connector_keys_sync(db, user_id)
    if moved:
        print(f"  ✓ Connector write keys handed to their workspace owners: {moved}")
    db.execute(delete(APIKey).where(APIKey.user_id == user_id))


def _owned_workspaces(db: Session, user_id: str) -> list[Workspace]:
    """The live (not soft-deleted) workspaces ``user_id`` owns."""
    return list(
        db.execute(
            select(Workspace)
            .where(Workspace.owner_user_id == user_id, Workspace.deleted_at.is_(None))
            .order_by(Workspace.id)
        )
        .scalars()
        .all()
    )


def _resolve_target(db: Session, ident: str, admin: User) -> User | None:
    """The user ``--transfer-to`` names (user_id, login_id or email), never the admin."""
    target = db.execute(
        select(User).where(or_(User.user_id == ident, User.login_id == ident, User.email == ident))
    ).scalar_one_or_none()
    if target is None or target.user_id == admin.user_id:
        return None
    return target


def _non_member_workspaces(
    db: Session, workspaces: list[Workspace], user_id: str
) -> list[Workspace]:
    """The ``workspaces`` that ``user_id`` is not a member of."""
    member_of = set(
        db.execute(
            select(WorkspaceMember.workspace_id).where(
                WorkspaceMember.user_id == user_id,
                WorkspaceMember.workspace_id.in_([ws.id for ws in workspaces]),
            )
        )
        .scalars()
        .all()
    )
    return [ws for ws in workspaces if ws.id not in member_of]


async def _transfer_workspaces(workspace_ids: list[UUID], admin_id: str, target_id: str) -> None:
    """Transfer each workspace with the service the API uses (row lock, owner
    re-check, epoch bump, audit row). Each transfer commits on its own: a
    failure part-way leaves every workspace with a live owner, and a re-run
    picks up the rest."""
    engine = create_async_engine(to_async_database_url(get_sync_database_url()))
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            service = WorkspaceOwnershipService(db)
            for workspace_id in workspace_ids:
                await service.transfer_ownership(
                    workspace_id=workspace_id,
                    current_owner_id=admin_id,
                    target_user_id=target_id,
                    performed_by_email=AUDIT_ACTOR_EMAIL,
                )
    finally:
        await engine.dispose()


def _print_workspaces(workspaces: list[Workspace]) -> None:
    for ws in workspaces:
        print(f"    - {ws.name} ({ws.id})")


def delete_admin(transfer_to: str | None = None) -> None:
    print("=" * 50)
    print("Kagura Memory Cloud - Delete Password Admin")
    print("=" * 50)

    # The production secret guard (#2239) before any DB work.
    require_settings()

    engine = create_engine(get_sync_database_url())

    with Session(engine) as db:
        result = db.execute(
            select(User).where(User.auth_method == "password", User.role == "admin")
        )
        admin = result.scalar_one_or_none()

        if not admin:
            print("\n✗ No password admin found.")
            sys.exit(1)

        total_users = db.execute(select(func.count()).select_from(User)).scalar() or 0

        print(f"\n  Login ID: {admin.login_id}")
        print(f"  Total users: {total_users}")

        if total_users > 1:
            owned = _owned_workspaces(db, admin.user_id)
            target: User | None = None
            if owned:
                if transfer_to is None:
                    print("\n✗ The admin owns workspaces other users may be in:")
                    _print_workspaces(owned)
                    print("  Re-run with --transfer-to USER (a member of each of them).")
                    sys.exit(1)
                target = _resolve_target(db, transfer_to, admin)
                if target is None:
                    print(f"\n✗ --transfer-to {transfer_to}: no such user (other than the admin).")
                    sys.exit(1)
                missing = _non_member_workspaces(db, owned, target.user_id)
                if missing:
                    print(f"\n✗ {target.user_id} is not a member of:")
                    _print_workspaces(missing)
                    print("  Add them as a member first. Nothing was changed.")
                    sys.exit(1)
                print(f"  Workspaces to transfer to {target.user_id}:")
                _print_workspaces(owned)
            print("  ⚠ Other users exist. Workspaces will be preserved.")
            confirm = input("  Delete admin user only? [y/N]: ").strip().lower()
            if confirm != "y":
                print("  Cancelled.")
                sys.exit(0)

            if target is not None:
                # Before the key re-own, so the admin's connector keys there
                # follow the new owner.
                asyncio.run(
                    _transfer_workspaces([ws.id for ws in owned], admin.user_id, target.user_id)
                )
                print(f"  ✓ Workspaces transferred to {target.user_id}: {len(owned)}")

            _delete_api_keys(db, admin.user_id)
            db.delete(admin)
            db.commit()

            print("\n✓ Admin user and API keys deleted (workspaces preserved).")
        else:
            confirm = input("  Delete admin, workspace, and all API keys? [y/N]: ").strip().lower()
            if confirm != "y":
                print("  Cancelled.")
                sys.exit(0)

            _delete_api_keys(db, admin.user_id)
            db.execute(delete(WorkspaceMember).where(WorkspaceMember.user_id == admin.user_id))
            db.execute(delete(Workspace).where(Workspace.owner_user_id == admin.user_id))
            db.delete(admin)
            db.commit()

            print("\n✓ Admin, workspace, and API keys deleted.")

        print("  Run: python -m src.cli.create_admin")

    engine.dispose()


def _parse(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--transfer-to",
        metavar="USER",
        help="user_id, login_id or email of the member who takes over the "
        "workspaces the admin owns (required when it owns any and other users exist)",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    delete_admin(_parse().transfer_to)
