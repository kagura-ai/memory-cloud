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

# Auto-load .env.local so API_KEY_SECRET etc. are available
from dotenv import load_dotenv

_project_root = Path(__file__).parent.parent.parent.parent
load_dotenv(_project_root / ".env.local")

from sqlalchemy import create_engine, func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from auth.api_keys import APIKeyManager  # noqa: E402
from auth.password import hash_password  # noqa: E402
from auth.totp import generate_totp_secret, get_provisioning_uri, verify_totp  # noqa: E402
from cli.db import get_sync_database_url  # noqa: E402
from db.base import Base  # noqa: E402, F401
from models.auth import APIKey, User, Workspace, WorkspaceMember  # noqa: E402
from utils.datetime import utcnow  # noqa: E402

_secrets_were_generated = False


def _get_secret_from_docker(key: str) -> str | None:
    """Try to get a secret from the running API container."""
    import subprocess

    try:
        compose_file = _project_root / "docker-compose.yml"
        if not compose_file.exists():
            return None
        result = subprocess.run(
            ["docker", "compose", "-f", str(compose_file), "exec", "-T", "api", "env"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        for line in result.stdout.splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1]
    except Exception:
        pass
    return None


def _ensure_api_key_secret() -> str:
    """Ensure API_KEY_SECRET is set. Try Docker → .env.local → generate new."""
    global _secrets_were_generated
    secret = os.getenv("API_KEY_SECRET")
    placeholders = {"change-me-api-key-secret", "change-me-to-random-hex-string-min-32-bytes", ""}

    if secret and secret not in placeholders:
        return secret

    # Try to get from running Docker container
    docker_secret = _get_secret_from_docker("API_KEY_SECRET")
    if docker_secret and docker_secret not in placeholders:
        print("  ✓ API_KEY_SECRET loaded from Docker container")
        os.environ["API_KEY_SECRET"] = docker_secret
        _update_env_local("API_KEY_SECRET", docker_secret)
        print("  ✓ API_KEY_SECRET saved to .env.local")
        return docker_secret

    # Generate new
    print("\n  ⚠ API_KEY_SECRET not set. Generating one...")
    secret = secrets.token_hex(32)
    os.environ["API_KEY_SECRET"] = secret
    _secrets_were_generated = True

    _update_env_local("API_KEY_SECRET", secret)
    print("  ✓ API_KEY_SECRET saved to .env.local")

    # Also generate JWT_SECRET if it's a placeholder
    jwt_secret = os.getenv("JWT_SECRET", "")
    jwt_placeholders = {"change-me-jwt-secret", "change-me-to-random-hex-string-min-32-bytes", ""}
    if not jwt_secret or jwt_secret in jwt_placeholders:
        docker_jwt = _get_secret_from_docker("JWT_SECRET")
        if docker_jwt and docker_jwt not in jwt_placeholders:
            os.environ["JWT_SECRET"] = docker_jwt
            _update_env_local("JWT_SECRET", docker_jwt)
            print("  ✓ JWT_SECRET loaded from Docker and saved to .env.local")
        else:
            new_jwt = secrets.token_hex(32)
            os.environ["JWT_SECRET"] = new_jwt
            _update_env_local("JWT_SECRET", new_jwt)
            print("  ✓ JWT_SECRET generated and saved to .env.local")

    return secret


def _update_env_local(key: str, value: str) -> None:
    """Update or add a key in .env.local."""
    import re

    env_file = _project_root / ".env.local"
    if not env_file.exists():
        return

    content = env_file.read_text()
    pattern = rf"^{key}=.*$"
    if re.search(pattern, content, re.MULTILINE):
        content = re.sub(pattern, f"{key}={value}", content, flags=re.MULTILINE)
    else:
        content += f"\n{key}={value}\n"
    env_file.write_text(content)


def _restart_api_if_needed() -> None:
    """Restart API container if secrets were generated (so it picks up new .env.local)."""
    if not _secrets_were_generated:
        return

    import subprocess

    print("\n==> Restarting API container (new secrets generated)...")
    try:
        compose_file = _project_root / "docker-compose.yml"
        if compose_file.exists():
            subprocess.run(
                ["docker", "compose", "-f", str(compose_file), "restart", "api"],
                capture_output=True,
                timeout=30,
            )
            print("  ✓ API container restarted")
        else:
            print("  ⚠ docker-compose.yml not found. Restart API manually.")
    except Exception:
        print("  ⚠ Could not restart API. Run: docker compose restart api")


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

        # Restart API container if API_KEY_SECRET was generated
        _restart_api_if_needed()

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
