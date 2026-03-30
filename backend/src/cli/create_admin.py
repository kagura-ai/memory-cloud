"""Create initial admin user with password authentication.

Issue #51: Password + MFA login for initial admin.

Usage:
    cd backend && python -m src.cli.create_admin

    # Inside Docker:
    docker compose exec api python -m src.cli.create_admin
"""

import getpass
import json
import os
import secrets
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from auth.api_keys import APIKeyManager
from auth.password import hash_password
from auth.totp import generate_totp_secret, get_provisioning_uri, verify_totp
from cli import get_sync_database_url
from db.base import Base  # noqa: F401
from models.auth import APIKey, User, Workspace, WorkspaceMember
from utils.datetime import utcnow


def _ensure_api_key_secret() -> str:
    """Ensure API_KEY_SECRET is set, prompt if missing."""
    secret = os.getenv("API_KEY_SECRET")
    if not secret or secret == "change-me-to-random-hex-string-min-32-bytes":
        print("\n  ⚠ API_KEY_SECRET not set. Generating one...")
        secret = secrets.token_hex(32)
        os.environ["API_KEY_SECRET"] = secret
        print(f"  API_KEY_SECRET={secret}")
        print("  → Add this to your .env.local or docker-compose.yml")
    return secret


def _create_workspace(db: Session, user_id: str) -> "Workspace":
    """Create personal workspace and membership for admin."""
    workspace = Workspace(
        name="Personal Workspace",
        owner_user_id=user_id,
        plan_name="pro",
    )
    db.add(workspace)
    db.flush()  # get workspace.id

    member = WorkspaceMember(
        workspace_id=workspace.id,
        user_id=user_id,
        role="owner",
    )
    db.add(member)
    db.flush()
    return workspace


def _create_api_key(db: Session, user_id: str, workspace_id) -> str:
    """Create an API key for the admin user scoped to workspace."""
    _ensure_api_key_secret()
    from utils.encryption import get_encryptor

    raw_key = APIKeyManager._generate_key()
    key_hash = APIKeyManager._hash_key(raw_key)
    key_prefix = raw_key[:16]

    encryptor = get_encryptor()
    plaintext_encrypted = encryptor.encrypt(raw_key)

    api_key = APIKey(
        key_hash=key_hash,
        key_prefix=key_prefix,
        name="admin-cli",
        user_id=user_id,
        workspace_id=workspace_id,
        visibility_expires_at=utcnow() + timedelta(days=365 * 10),
        plaintext_encrypted=plaintext_encrypted,
    )
    db.add(api_key)
    db.flush()
    return raw_key


def _write_mcp_json(api_key: str):
    """Write .mcp.json to project root."""
    project_root = Path(__file__).parent.parent.parent.parent
    mcp_path = project_root / ".mcp.json"

    mcp_config = {
        "mcpServers": {
            "kagura-memory": {
                "type": "http",
                "url": "http://localhost:8080/mcp",
                "headers": {"Authorization": f"Bearer {api_key}"},
            }
        }
    }

    mcp_path.write_text(json.dumps(mcp_config, indent=2) + "\n")
    print(f"  → {mcp_path}")


def create_admin():
    """Interactive admin user creation."""
    print("=" * 50)
    print("Kagura Memory Cloud - Create Admin")
    print("=" * 50)

    engine = create_engine(get_sync_database_url())

    with Session(engine) as db:
        # Check if password admin already exists
        admin_count = db.execute(
            select(func.count())
            .select_from(User)
            .where(User.auth_method == "password", User.role == "admin")
        ).scalar()
        if admin_count and admin_count > 0:
            print("\n✗ Password admin already exists.")
            print("  To change password:  python -m src.cli.reset_password")
            print("  To delete and redo:  python -m src.cli.delete_admin")
            sys.exit(1)

        # Login ID
        print("\nEnter a login ID (arbitrary string, e.g., 'admin'):")
        login_id = input("  Login ID: ").strip()
        if not login_id or len(login_id) > 255:
            print("✗ Login ID must be 1-255 characters.")
            sys.exit(1)

        # Password (retry on failure)
        while True:
            print("\nEnter a password (minimum 12 characters):")
            password = getpass.getpass("  Password: ")
            if len(password) < 12:
                print("  ✗ Password must be at least 12 characters. Try again.")
                continue

            password_confirm = getpass.getpass("  Confirm:  ")
            if password != password_confirm:
                print("  ✗ Passwords do not match. Try again.")
                continue

            break

        # MFA (default: on)
        totp_secret = None
        totp_enabled = False

        print("\nMFA (TOTP) is recommended for admin accounts.")
        mfa_choice = input("  Enable MFA? [Y/n]: ").strip().lower()

        if mfa_choice != "n":
            _ensure_api_key_secret()

            totp_secret = generate_totp_secret()
            uri = get_provisioning_uri(totp_secret, login_id)

            print(f"\n  Scan this URI with your authenticator app:\n  {uri}")

            try:
                import qrcode

                qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L)
                qr.add_data(uri)
                qr.make(fit=True)
                qr.print_ascii(invert=True)
            except ImportError:
                pass

            verify_code = input("\n  Enter 6-digit code to verify: ").strip()

            if not verify_totp(totp_secret, verify_code):
                print("✗ Invalid code. MFA setup aborted. You can enable MFA later.")
                totp_secret = None
            else:
                totp_enabled = True
                print("  ✓ MFA verified!")

                from utils.encryption import get_encryptor

                totp_secret = get_encryptor().encrypt(totp_secret)

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
            is_initial_admin=(admin_count == 0),
            last_login_at=utcnow(),
        )
        db.add(admin)
        db.flush()

        # Create workspace
        print("\n==> Creating workspace...")
        workspace = _create_workspace(db, admin.user_id)
        admin.current_workspace_id = workspace.id
        print(f"  Workspace: {workspace.name} ({workspace.id})")

        # Generate API key (scoped to workspace)
        print("\n==> Generating API key...")
        api_key = _create_api_key(db, admin.user_id, workspace.id)
        print(f"  API Key: {api_key}")

        # Write .mcp.json
        print("\n==> Writing .mcp.json...")
        _write_mcp_json(api_key)

        db.commit()

        print("\n" + "=" * 50)
        print("✓ Admin setup complete!")
        print(f"  Login ID:  {login_id}")
        print(f"  MFA:       {'enabled' if totp_enabled else 'disabled'}")
        print(f"  API Key:   {api_key}")
        print("  MCP URL:   http://localhost:8080/mcp")
        print("  MCP:       .mcp.json written")
        print("  Login:     http://localhost:3000/login")
        print("=" * 50)

    engine.dispose()


if __name__ == "__main__":
    create_admin()
