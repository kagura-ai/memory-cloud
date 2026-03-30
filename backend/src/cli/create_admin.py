"""Create initial admin user with password authentication.

Issue #51: Password + MFA login for initial admin.

Usage:
    cd backend && python -m src.cli.create_admin

    # Inside Docker:
    docker compose exec api python -m src.cli.create_admin
"""

import getpass
import sys
from pathlib import Path

# Add backend/src to path when run directly
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from auth.password import hash_password
from auth.totp import generate_totp_secret, get_provisioning_uri, verify_totp
from config.database import get_database_url
from db.base import Base  # noqa: F401
from models.auth import User
from utils.datetime import utcnow


def get_sync_database_url() -> str:
    """Get synchronous database URL (psycopg2)."""
    url = get_database_url()
    return url.replace("+asyncpg", "").replace("postgresql://", "postgresql+psycopg2://")


def create_admin():
    """Interactive admin user creation."""
    print("=" * 50)
    print("Kagura Memory Cloud - Create Initial Admin")
    print("=" * 50)

    engine = create_engine(get_sync_database_url())

    with Session(engine) as db:
        user_count = db.execute(select(func.count()).select_from(User)).scalar()
        if user_count and user_count > 0:
            print(f"\n✗ {user_count} user(s) already exist.")
            print("  This command is for initial setup only.")
            sys.exit(1)

        # Login ID
        print("\nEnter a login ID (arbitrary string, e.g., 'admin'):")
        login_id = input("  Login ID: ").strip()
        if not login_id or len(login_id) > 255:
            print("✗ Login ID must be 1-255 characters.")
            sys.exit(1)

        # Password
        print("\nEnter a password (minimum 12 characters):")
        password = getpass.getpass("  Password: ")
        if len(password) < 12:
            print("✗ Password must be at least 12 characters.")
            sys.exit(1)

        password_confirm = getpass.getpass("  Confirm:  ")
        if password != password_confirm:
            print("✗ Passwords do not match.")
            sys.exit(1)

        # Optional MFA
        totp_secret = None
        totp_enabled = False

        print("\nSetup MFA (TOTP) now? Adds an extra layer of security.")
        mfa_choice = input("  Enable MFA? [y/N]: ").strip().lower()

        if mfa_choice == "y":
            totp_secret = generate_totp_secret()
            uri = get_provisioning_uri(totp_secret, login_id)

            print(f"\n  Scan this URI with your authenticator app:\n  {uri}")

            try:
                import qrcode

                qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L)
                qr.add_data(uri)
                qr.make(fit=True)
                qr.print_ascii(invert=True)
            except Exception:
                pass

            verify_code = input("\n  Enter 6-digit code to verify: ").strip()

            if not verify_totp(totp_secret, verify_code):
                print("✗ Invalid code. MFA setup aborted. You can enable MFA later.")
                totp_secret = None
            else:
                totp_enabled = True
                print("  ✓ MFA verified!")

                try:
                    from utils.encryption import get_encryptor

                    totp_secret = get_encryptor().encrypt(totp_secret)
                except Exception as e:
                    print(f"  ⚠ Could not encrypt TOTP secret: {e}")
                    print("  Set API_KEY_SECRET environment variable for encryption.")
                    totp_secret = None
                    totp_enabled = False

        admin = User(
            login_id=login_id,
            email=f"{login_id}@local",
            user_id=f"local:{login_id}",
            name=login_id,
            role="admin",
            auth_method="password",
            password_hash=hash_password(password),
            totp_secret=totp_secret,
            totp_enabled=totp_enabled,
            is_initial_admin=True,
            last_login_at=utcnow(),
        )
        db.add(admin)
        db.commit()

        print("\n" + "=" * 50)
        print("✓ Admin user created successfully!")
        print(f"  Login ID: {login_id}")
        print(f"  MFA: {'enabled' if totp_enabled else 'disabled'}")
        print("=" * 50)

    engine.dispose()


if __name__ == "__main__":
    create_admin()
