"""Delete password admin user (for re-creation).

Issue #51: Password + MFA login for initial admin.

Usage:
    cd backend && python -m src.cli.delete_admin

    # Inside Docker:
    docker compose exec api python -m src.cli.delete_admin
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

_project_root = Path(__file__).parent.parent.parent.parent
load_dotenv(_project_root / ".env.local")

from sqlalchemy import create_engine, func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from cli.db import get_sync_database_url  # noqa: E402
from db.base import Base  # noqa: E402, F401
from models.auth import APIKey, User, Workspace, WorkspaceMember  # noqa: E402


def delete_admin():
    print("=" * 50)
    print("Kagura Memory Cloud - Delete Password Admin")
    print("=" * 50)

    engine = create_engine(get_sync_database_url())

    with Session(engine) as db:
        result = db.execute(
            select(User).where(User.auth_method == "password", User.role == "admin")
        )
        admin = result.scalar_one_or_none()

        if not admin:
            print("\n✗ No password admin found.")
            sys.exit(1)

        # Count total users to determine safety
        total_users = db.execute(select(func.count()).select_from(User)).scalar() or 0

        print(f"\n  Login ID: {admin.login_id}")
        print(f"  Total users: {total_users}")

        if total_users > 1:
            # Other users exist — only delete admin user, keep workspace
            print("  ⚠ Other users exist. Workspace will be preserved.")
            confirm = input("  Delete admin user only? [y/N]: ").strip().lower()
            if confirm != "y":
                print("  Cancelled.")
                sys.exit(0)

            db.execute(APIKey.__table__.delete().where(APIKey.user_id == admin.user_id))
            db.delete(admin)
            db.commit()

            print("\n✓ Admin user and API keys deleted (workspace preserved).")
        else:
            # Solo admin — safe to delete everything
            confirm = input("  Delete admin, workspace, and all API keys? [y/N]: ").strip().lower()
            if confirm != "y":
                print("  Cancelled.")
                sys.exit(0)

            db.execute(APIKey.__table__.delete().where(APIKey.user_id == admin.user_id))
            db.execute(
                WorkspaceMember.__table__.delete().where(WorkspaceMember.user_id == admin.user_id)
            )
            db.execute(Workspace.__table__.delete().where(Workspace.owner_user_id == admin.user_id))
            db.delete(admin)
            db.commit()

            print("\n✓ Admin, workspace, and API keys deleted.")

        print("  Run: python -m src.cli.create_admin")

    engine.dispose()


if __name__ == "__main__":
    delete_admin()
