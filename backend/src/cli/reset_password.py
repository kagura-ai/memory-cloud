"""Reset password and/or MFA for a local admin user.

Issue #51: Password + MFA login for initial admin.

Usage:
    cd backend && python -m src.cli.reset_password

    # Inside Docker:
    docker compose exec api python -m src.cli.reset_password
"""

import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from auth.password import hash_password
from cli import get_sync_database_url
from db.base import Base  # noqa: F401
from models.auth import User


def reset_password():
    print("=" * 50)
    print("Kagura Memory Cloud - Reset Password / MFA")
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

        print(f"\n  Current MFA: {'enabled' if user.totp_enabled else 'disabled'}")
        print("\n  What do you want to reset?")
        print("  1) Password only")
        print("  2) Disable MFA only")
        print("  3) Both (password + disable MFA)")
        choice = input("  Choice [1/2/3]: ").strip()

        if choice not in ("1", "2", "3"):
            print("✗ Invalid choice.")
            sys.exit(1)

        # Reset password
        if choice in ("1", "3"):
            while True:
                print("\nEnter new password (minimum 12 characters):")
                password = getpass.getpass("  New Password: ")
                if len(password) < 12:
                    print("  ✗ Password must be at least 12 characters. Try again.")
                    continue

                password_confirm = getpass.getpass("  Confirm:      ")
                if password != password_confirm:
                    print("  ✗ Passwords do not match. Try again.")
                    continue

                break

            user.password_hash = hash_password(password)
            print("  ✓ Password updated.")

        # Disable MFA
        if choice in ("2", "3"):
            user.totp_enabled = False
            user.totp_secret = None
            print("  ✓ MFA disabled.")

        db.commit()

        # Offer to re-enable MFA
        if choice in ("2", "3"):
            re_enable = input("\n  Re-enable MFA now? [y/N]: ").strip().lower()
            if re_enable == "y":
                from auth.totp import generate_totp_secret, get_provisioning_uri, verify_totp

                api_key_secret = os.getenv("API_KEY_SECRET")
                if not api_key_secret:
                    print("  ⚠ Set API_KEY_SECRET env var to enable MFA.")
                else:
                    totp_secret = generate_totp_secret()
                    uri = get_provisioning_uri(totp_secret, login_id)
                    print(f"\n  Scan this URI:\n  {uri}")

                    try:
                        import qrcode

                        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L)
                        qr.add_data(uri)
                        qr.make(fit=True)
                        qr.print_ascii(invert=True)
                    except ImportError:
                        pass

                    verify_code = input("\n  Enter 6-digit code: ").strip()

                    if verify_totp(totp_secret, verify_code):
                        from utils.encryption import get_encryptor

                        user.totp_secret = get_encryptor().encrypt(totp_secret)
                        user.totp_enabled = True
                        db.commit()
                        print("  ✓ MFA re-enabled!")
                    else:
                        print("  ✗ Invalid code. MFA remains disabled.")

        print("\n" + "=" * 50)
        print(f"✓ Done for '{login_id}'.")
        print("=" * 50)

    engine.dispose()


if __name__ == "__main__":
    reset_password()
