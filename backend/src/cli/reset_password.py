"""Reset password and/or MFA for a local admin user.

Issue #51: Password + MFA login for initial admin.

Usage:
    cd backend && python -m src.cli.reset_password

A password reset is the account's compromise-recovery path, so resetting the
password here (choices 1 and 3) does what the emailed-link reset does
(``PasswordAccountService.complete_reset``), in one transaction (#1866):

- the owner's ``users`` row is locked and every OAuth2 / MCP token, pending
  authorization code and device code of the account is revoked with the
  shared statements of ``services/oauth_grant_revocation.py``, in their lock
  order ``users -> oauth_authorization_codes -> oauth_device_codes ->
  oauth_tokens``;
- emailed reset / set-up links are invalidated (#1678) and the known devices
  are forgotten (#1769), so the next sign-in from each browser is reported;
- every browser session of the account is deleted from the session store
  (Redis, ``REDIS_URL``) with ``strict=True`` before the commit.

The session store must be reachable. It is probed before the password prompt
and the reset is refused when it cannot be reached; a delete that fails later
rolls the transaction back. Either way the password, MFA and grants are left
as they were and the CLI exits non-zero: a reset that reported success while the old sessions survived would
not contain the incident. There is no "continue anyway" prompt — while the
session store is down nobody can sign in either, so the operator loses
nothing by bringing it back first. Run the CLI where ``REDIS_URL`` points at
the Redis the API uses (the default ``redis://localhost:6379`` is the port
Docker Compose publishes on the host).

API keys and OAuth client secrets are not revoked; the CLI says so. No audit
row and no notification email are written (operator CLI actions run outside
the API).

Decision — "Disable MFA only" (choice 2) revokes nothing. It is the recovery
for a lost authenticator, not for a leaked credential: the password, and with
it everything a session or grant was obtained with, stays the same, so ending
the sessions would contain nothing and would only sign the owner out of every
browser and MCP client. It also keeps the lost-authenticator recovery usable
while the session store is down. After a suspected compromise the operator
resets the password (choice 1 or 3), and the CLI prints that hint.
"""

import getpass
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from auth.password import hash_password  # noqa: E402
from auth.password_policy import (  # noqa: E402
    PASSWORD_REQUIREMENT_LINES,
    PasswordPolicyError,
    validate_password_policy,
)
from auth.session import SessionManager  # noqa: E402
from cli.db import get_sync_database_url  # noqa: E402
from config.database import get_redis_url  # noqa: E402
from models.auth import User  # noqa: E402
from services.email_action_token_service import (  # noqa: E402
    PASSWORD_LINK_PURPOSES,
    invalidation_statement,
)
from services.known_device_service import known_devices_delete  # noqa: E402
from services.oauth_grant_revocation import (  # noqa: E402
    RevokedGrants,
    revoke_oauth_grants_sync,
)

_project_root = Path(__file__).parent.parent.parent.parent


def _get_env_from_docker(key: str) -> str | None:
    """Get env var from running API container."""
    try:
        result = subprocess.run(
            ["docker", "compose", "exec", "-T", "api", "printenv", key],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(_project_root),
        )
        value = result.stdout.strip()
        return value if result.returncode == 0 and value else None
    except Exception:
        return None  # Docker not running or not accessible


def _redis_location(redis_url: str) -> str:
    """The store's host part, without credentials, for operator messages."""
    return redis_url.rsplit("@", 1)[-1]


def _session_store_or_exit() -> SessionManager:
    """Connect to the browser-session store, or refuse the reset (#1866).

    A password reset that cannot sign the old sessions out must not happen:
    exit before anything is prompted for or written.
    """
    redis_url = get_redis_url()
    try:
        return SessionManager(redis_url=redis_url)
    except Exception as exc:
        print(
            f"\n✗ Cannot reach the session store at {_redis_location(redis_url)}"
            f" ({type(exc).__name__})."
        )
        print("  A password reset must sign out every browser session of the account.")
        print("  Nothing was changed. Start Redis, or set REDIS_URL to the Redis the API")
        print("  uses, and run this command again.")
        sys.exit(1)


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

        # Reset password. The session store is probed before the prompt: without it there is
        # no password reset (#1866). Choice 2 does not need it.
        session_store = _session_store_or_exit() if choice in ("1", "3") else None
        grants: RevokedGrants | None = None
        sessions_deleted = 0
        if session_store is not None:
            while True:
                print("\nPassword requirements:")
                for line in PASSWORD_REQUIREMENT_LINES:
                    print(f"  - {line}")
                password = getpass.getpass("\n  New Password: ")

                # The shared policy: composition, UTF-8 (#1718), bcrypt's 72 bytes
                # (#1707) — refused before the confirmation prompt.
                try:
                    validate_password_policy(password)
                except PasswordPolicyError as exc:
                    print(f"  ✗ {exc} Try again.")
                    continue

                password_confirm = getpass.getpass("  Confirm:      ")
                if password != password_confirm:
                    print("  ✗ Passwords do not match. Try again.")
                    continue

                break

            password_hash = hash_password(password)
            # First the owner lock and the OAuth2 / MCP grants, in the shared
            # lock order (#1738, #1770): every grant writer waits on the user
            # row from here until the commit.
            grants = revoke_oauth_grants_sync(db, user.user_id)
            user.password_hash = password_hash
            # Kill emailed reset / set-up links in the same commit (#1678): one
            # issued before this reset must not overwrite the new password.
            db.execute(
                invalidation_statement(user_id=user.user_id, purposes=PASSWORD_LINK_PURPOSES)
            )
            # #1769: forget every known browser, so the next sign-in from each
            # is a new device.
            db.execute(known_devices_delete(user.user_id))

        # Disable MFA
        if choice in ("2", "3"):
            user.totp_enabled = False
            user.totp_secret = None

        if session_store is not None:
            # Send every write before the sessions go, so a database error
            # surfaces while nothing has been signed out yet.
            db.flush()
            try:
                sessions_deleted = session_store.delete_user_sessions(user.user_id, strict=True)
            except Exception as exc:
                # The old sessions may have survived: the new password and the
                # revocations must not be committed without them (#1866).
                db.rollback()
                print(f"\n✗ Could not delete the browser sessions ({type(exc).__name__}).")
                print("  The reset was rolled back: the password, MFA and OAuth grants are as")
                print("  before. Sessions the store already dropped stay signed out.")
                print("  Check the session store (REDIS_URL) and run this command again.")
                sys.exit(1)

        db.commit()

        if grants is not None:
            pending_codes = grants.authorization_codes + grants.device_codes
            print("  ✓ Password updated.")
            print(
                f"  ✓ Revoked: {sessions_deleted} browser session(s),"
                f" {grants.tokens} OAuth / MCP token(s),"
                f" {pending_codes} pending authorization / device code(s)."
            )
            print("  ✓ Known devices forgotten.")
            print("  ⚠ API keys and OAuth client secrets are NOT revoked. After a suspected")
            print("    compromise, review them in Settings.")
        if choice in ("2", "3"):
            print("  ✓ MFA disabled.")
        if choice == "2":
            print("  ⚠ Browser sessions and OAuth / MCP grants were NOT revoked. After a")
            print("    suspected compromise, reset the password (choice 1 or 3).")

        # Offer to re-enable MFA
        if choice in ("2", "3"):
            re_enable = input("\n  Re-enable MFA now? [y/N]: ").strip().lower()
            if re_enable == "y":
                from auth.totp import (  # noqa: E402
                    generate_totp_secret,
                    get_provisioning_uri,
                    verify_totp,
                )

                api_key_secret = os.getenv("API_KEY_SECRET") or _get_env_from_docker(
                    "API_KEY_SECRET"
                )
                if not api_key_secret:
                    print("  ⚠ API_KEY_SECRET not available. Ensure Docker is running.")
                else:
                    os.environ["API_KEY_SECRET"] = api_key_secret
                    totp_secret = generate_totp_secret()
                    uri = get_provisioning_uri(totp_secret, login_id)
                    print(f"\n  Scan this URI:\n  {uri}")

                    try:
                        import qrcode
                        import qrcode.constants

                        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L)
                        qr.add_data(uri)
                        qr.make(fit=True)
                        qr.print_ascii(invert=True)
                    except ImportError:
                        print("  (Install 'qrcode' package to display QR code in terminal)")

                    verify_code = input("\n  Enter 6-digit code: ").strip()

                    if verify_totp(totp_secret, verify_code):
                        from utils.encryption import get_encryptor  # noqa: E402

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
