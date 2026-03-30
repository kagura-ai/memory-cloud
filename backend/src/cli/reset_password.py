"""Reset password for a local admin user.

Issue #51: Password + MFA login for initial admin.

Usage:
    cd backend && python -m src.cli.reset_password

    # Inside Docker:
    docker compose exec api python -m src.cli.reset_password
"""

import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from auth.password import hash_password
from config.database import get_database_url
from db.base import Base  # noqa: F401
from models.auth import User


def get_sync_database_url() -> str:
    """Get synchronous database URL (psycopg2)."""
    url = get_database_url()
    return url.replace("+asyncpg", "").replace("postgresql://", "postgresql+psycopg2://")


def reset_password():
    """Interactive password reset."""
    print("=" * 50)
    print("Kagura Memory Cloud - Reset Password")
    print("=" * 50)

    engine = create_engine(get_sync_database_url())

    with Session(engine) as db:
        login_id = input("\n  Login ID: ").strip()
        if not login_id:
            print("✗ Login ID cannot be empty.")
            sys.exit(1)

        user = db.execute(
            select(User).where(User.login_id == login_id, User.auth_method == "password")
        ).scalar_one_or_none()

        if not user:
            print(f"✗ No password user found with login_id '{login_id}'.")
            sys.exit(1)

        print("\nEnter new password (minimum 12 characters):")
        password = getpass.getpass("  New Password: ")
        if len(password) < 12:
            print("✗ Password must be at least 12 characters.")
            sys.exit(1)

        password_confirm = getpass.getpass("  Confirm:      ")
        if password != password_confirm:
            print("✗ Passwords do not match.")
            sys.exit(1)

        user.password_hash = hash_password(password)
        db.commit()

        print("\n" + "=" * 50)
        print(f"✓ Password reset successfully for '{login_id}'!")
        print("=" * 50)

    engine.dispose()


if __name__ == "__main__":
    reset_password()
