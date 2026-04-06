"""Create initial admin user with password authentication.

Issue #51: Password + MFA login for initial admin.
Requires Docker API container to be running (reads env vars from it).

Usage:
    cd backend && python -m src.cli.create_admin
    python -m src.cli.create_admin --skip-mcp-json  # skip .mcp.json file write
"""

import argparse
import getpass
import json
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import create_engine, func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from auth.api_keys import APIKeyManager  # noqa: E402
from auth.password import hash_password  # noqa: E402
from auth.totp import generate_totp_secret, get_provisioning_uri, verify_totp  # noqa: E402
from cli.db import get_sync_database_url  # noqa: E402
from models.auth import APIKey, ExternalAPIKey, User, Workspace, WorkspaceMember  # noqa: E402
from utils.datetime import utcnow  # noqa: E402

# Project root for docker compose commands (always the repo root on host).
# Inside the Docker container this resolves to "/" (WORKDIR=/app, code at
# /app/src/cli/create_admin.py → 4 parents = /), which is unused because
# docker compose is only invoked from the host and Docker builds use
# MCP_JSON_DIR or the smart fallback in _resolve_mcp_json_path() below.
_project_root = Path(__file__).parent.parent.parent.parent


def _resolve_mcp_json_path() -> Path | None:
    """Resolve the .mcp.json output path.

    Priority:
    1. MCP_JSON_DIR env var (explicit operator override)
    2. _project_root if it looks like a real repo (has pyproject.toml or .git)
    3. $HOME if HOME is set and writable
    4. cwd if writable
    5. None → caller should skip and print fallback config to stdout
    """
    if env_dir := os.environ.get("MCP_JSON_DIR"):
        return Path(env_dir) / ".mcp.json"

    # On host, _project_root is the repo root (has pyproject.toml).
    # In Docker it resolves to "/" which has neither → fall through.
    if (_project_root / "pyproject.toml").exists() or (_project_root / ".git").exists():
        return _project_root / ".mcp.json"

    candidates: list[Path] = []
    if home := os.environ.get("HOME"):
        candidates.append(Path(home))
    candidates.append(Path.cwd())

    for candidate in candidates:
        if os.access(candidate, os.W_OK):
            return candidate / ".mcp.json"

    return None


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


def _require_api_key_secret() -> str:
    """Get API_KEY_SECRET from Docker container. Exit if unavailable."""
    # Check environment first (may already be set)
    secret = os.getenv("API_KEY_SECRET")
    if secret:
        return secret

    # Get from Docker
    secret = _get_env_from_docker("API_KEY_SECRET")
    if secret:
        os.environ["API_KEY_SECRET"] = secret
        return secret

    print("✗ API_KEY_SECRET not available.")
    print("  Ensure Docker is running: docker compose up -d")
    sys.exit(1)


def _create_workspace(db: Session, user_id: str) -> Workspace:
    """Create personal workspace and membership for admin."""
    workspace = Workspace(
        name="Personal Workspace",
        owner_user_id=user_id,
        plan_name="pro",
    )
    db.add(workspace)
    db.flush()

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
    _require_api_key_secret()
    from utils.encryption import get_encryptor  # noqa: E402

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
        visibility_expires_at=utcnow() + timedelta(minutes=10),
        plaintext_encrypted=plaintext_encrypted,
    )
    db.add(api_key)
    db.flush()
    return raw_key


def _build_mcp_config(api_key: str, workspace_id: str) -> dict:
    return {
        "mcpServers": {
            "kagura-memory": {
                "type": "http",
                "url": f"http://localhost:8080/mcp/w/{workspace_id}",
                "headers": {"Authorization": f"Bearer {api_key}"},
            }
        }
    }


def _write_mcp_json(api_key: str, workspace_id: str) -> bool:
    """Write .mcp.json. Best-effort: on failure prints config to stdout.

    Returns True if file was written, False if skipped/fell back to stdout.
    Never raises — admin creation has already been committed by the caller.
    """
    mcp_config = _build_mcp_config(api_key, workspace_id)
    mcp_path = _resolve_mcp_json_path()

    if mcp_path is None:
        print("  ⚠ No writable directory found for .mcp.json.")
        _print_mcp_config_fallback(mcp_config)
        return False

    try:
        mcp_path.write_text(json.dumps(mcp_config, indent=2) + "\n")
    except OSError as e:
        print(f"  ⚠ Could not write {mcp_path}: {e}")
        print("    (Admin account was created successfully — this is only the convenience file.)")
        _print_mcp_config_fallback(mcp_config)
        return False

    print(f"  → {mcp_path}")
    return True


def _print_mcp_config_fallback(mcp_config: dict) -> None:
    """Print the .mcp.json content so the operator can copy-paste it manually."""
    print("    Copy the following into ~/.mcp.json manually:")
    print("    " + "-" * 60)
    for line in json.dumps(mcp_config, indent=2).splitlines():
        print(f"    {line}")
    print("    " + "-" * 60)


def _configure_embedding_provider(db: Session, user_id: str, workspace_id) -> str | None:
    """Auto-detect and configure embedding provider.

    Priority: OPENAI_API_KEY env → Ollama running → warn.
    Returns provider name or None.
    """
    from utils.encryption import get_encryptor  # noqa: E402

    # 1. Check OPENAI_API_KEY (Docker/env.local first, host env as fallback)
    openai_key = _get_env_from_docker("OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")
    if openai_key:
        encryptor = get_encryptor()
        ext_key = ExternalAPIKey(
            key_name="OPENAI_API_KEY",
            provider="openai",
            encrypted_value=encryptor.encrypt(openai_key),
            user_id=user_id,
            workspace_id=workspace_id,
            enabled=True,
        )
        db.add(ext_key)
        db.flush()
        return "openai"

    # 2. Check Ollama
    ollama_url = (
        os.getenv("OLLAMA_BASE_URL")
        or _get_env_from_docker("OLLAMA_BASE_URL")
        or "http://localhost:11434"
    )
    try:
        import urllib.request

        req = urllib.request.Request(ollama_url, method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            if resp.status == 200:
                return "ollama"
    except Exception:
        pass  # Ollama not running or unreachable — expected on most setups

    return None


def create_admin(skip_mcp_json: bool = False):
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
            print("\nPassword requirements:")
            print("  - Minimum 12 characters")
            print("  - At least 1 uppercase letter (A-Z)")
            print("  - At least 1 lowercase letter (a-z)")
            print("  - At least 1 digit (0-9)")
            print("  - At least 1 special character (!@#$%^&*...)")
            password = getpass.getpass("\n  Password: ")

            # Validate
            errors = []
            if len(password) < 12:
                errors.append("at least 12 characters")
            if not any(c.isupper() for c in password):
                errors.append("1 uppercase letter")
            if not any(c.islower() for c in password):
                errors.append("1 lowercase letter")
            if not any(c.isdigit() for c in password):
                errors.append("1 digit")
            if not any(not c.isalnum() for c in password):
                errors.append("1 special character")
            if errors:
                print(f"  ✗ Missing: {', '.join(errors)}. Try again.")
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
            _require_api_key_secret()

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
                print("  (Install 'qrcode' package to display QR code in terminal)")

            verify_code = input("\n  Enter 6-digit code to verify: ").strip()

            if not verify_totp(totp_secret, verify_code):
                print("✗ Invalid code. MFA setup aborted. You can enable MFA later.")
                totp_secret = None
            else:
                totp_enabled = True
                print("  ✓ MFA verified!")

                from utils.encryption import get_encryptor  # noqa: E402

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

        # Auto-configure embedding provider
        print("\n==> Configuring embedding provider...")
        provider = _configure_embedding_provider(db, admin.user_id, workspace.id)
        if provider == "openai":
            print("  ✓ OpenAI API key registered for workspace")
        elif provider == "ollama":
            print("  ✓ Ollama detected — set EMBEDDING_PROVIDER=ollama in .env.local")
        else:
            print("  ⚠ No embedding provider found.")
            print("    Set OPENAI_API_KEY in .env.local, or start Ollama.")
            print("    Memory features (remember/recall) require an embedding provider.")

        # Commit BEFORE any best-effort file writes so a PermissionError
        # on .mcp.json can never roll back admin / workspace / API key.
        db.commit()

        # Best-effort .mcp.json write (never raises — falls back to stdout).
        mcp_written = False
        if skip_mcp_json:
            print("\n==> Skipping .mcp.json (--skip-mcp-json)")
            _print_mcp_config_fallback(_build_mcp_config(api_key, str(workspace.id)))
        else:
            print("\n==> Writing .mcp.json...")
            mcp_written = _write_mcp_json(api_key, str(workspace.id))

        print("\n" + "=" * 50)
        print("✓ Admin setup complete!")
        print(f"  Login ID:     {login_id}")
        print(f"  MFA:          {'enabled' if totp_enabled else 'disabled'}")
        print(f"  Workspace ID: {workspace.id}")
        print(f"  API Key:      {api_key}")
        print(f"  MCP URL:      http://localhost:8080/mcp/w/{workspace.id}")
        print(f"  MCP:          {'.mcp.json written' if mcp_written else 'config printed above'}")
        print("  Login:        http://localhost:3000/login")
        print("=" * 50)

    engine.dispose()


def _main() -> None:
    parser = argparse.ArgumentParser(
        prog="create_admin",
        description="Create initial admin user for Kagura Memory Cloud.",
    )
    parser.add_argument(
        "--skip-mcp-json",
        action="store_true",
        help="Skip writing .mcp.json file (useful in Docker/CI). "
        "The config is printed to stdout so it can be copied manually.",
    )
    args = parser.parse_args()
    create_admin(skip_mcp_json=args.skip_mcp_json)


if __name__ == "__main__":
    _main()
