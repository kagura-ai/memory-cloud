"""Delete password admin user (for re-creation).

Issue #51: Password + MFA login for initial admin.
#2352: a connector's live write key the admin holds in a workspace someone
else owns is handed to that owner instead of being deleted.

Usage:
    cd backend && python -m src.cli.delete_admin
"""

import sys
from pathlib import Path
from typing import Any, cast

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import create_engine, delete, func, select  # noqa: E402
from sqlalchemy.engine import CursorResult  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from cli._env_guard import require_settings  # noqa: E402
from cli.db import get_sync_database_url  # noqa: E402
from models.auth import APIKey, User, Workspace, WorkspaceMember  # noqa: E402
from services.connector_key_ownership import reown_connector_keys_stmt  # noqa: E402


def _delete_api_keys(db: Session, user_id: str) -> None:
    """Hand the user's live connector keys to their workspace owners (#2352),
    then delete the rest of the user's keys."""
    moved = cast(CursorResult[Any], db.execute(reown_connector_keys_stmt(user_id))).rowcount or 0
    if moved:
        print(f"  ✓ Connector write keys handed to their workspace owners: {moved}")
    db.execute(delete(APIKey).where(APIKey.user_id == user_id))


def delete_admin() -> None:
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
            print("  ⚠ Other users exist. Workspace will be preserved.")
            confirm = input("  Delete admin user only? [y/N]: ").strip().lower()
            if confirm != "y":
                print("  Cancelled.")
                sys.exit(0)

            _delete_api_keys(db, admin.user_id)
            db.delete(admin)
            db.commit()

            print("\n✓ Admin user and API keys deleted (workspace preserved).")
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


if __name__ == "__main__":
    delete_admin()
