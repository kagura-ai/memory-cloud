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

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from config.database import get_database_url
from db.base import Base  # noqa: F401
from models.auth import APIKey, User, Workspace, WorkspaceMember


def get_sync_database_url() -> str:
    url = get_database_url()
    return url.replace("+asyncpg", "").replace("postgresql://", "postgresql+psycopg2://")


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

        print(f"\n  Login ID: {admin.login_id}")
        confirm = input("  Delete this admin and its API keys? [y/N]: ").strip().lower()

        if confirm != "y":
            print("  Cancelled.")
            sys.exit(0)

        # Delete API keys, workspace memberships, workspaces, then user
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
