"""Initialize .env.local with required secrets.

Issue #55: Auto-configure environment before Docker startup.
No Docker dependency — runs before `docker compose up -d`.

Usage:
    cd backend && python -m src.cli.setup_env
"""

import secrets
from pathlib import Path

_project_root = Path(__file__).parent.parent.parent.parent
_env_example = _project_root / ".env.example"
_env_local = _project_root / ".env.local"


def _generate_secret() -> str:
    """Generate a cryptographically secure secret."""
    return secrets.token_urlsafe(32)


def _read_env_file(path: Path) -> dict[str, str]:
    """Read .env file into dict (key=value, ignoring comments and handling quotes)."""
    env = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            value = value.strip()
            # Remove surrounding quotes
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            env[key.strip()] = value
    return env


def _set_env_value(key: str, value: str):
    """Set key=value in .env.local (append if missing, skip if exists)."""
    env = _read_env_file(_env_local)
    if key in env:
        return  # Already set, don't duplicate
    with open(_env_local, "a") as f:
        f.write(f"\n{key}={value}\n")


def setup_env():
    """Interactive environment setup."""
    print("=" * 50)
    print("Kagura Memory Cloud - Environment Setup")
    print("=" * 50)

    # 1. Ensure .env.local exists
    if not _env_local.exists():
        if _env_example.exists():
            import shutil

            shutil.copy(_env_example, _env_local)
            print(f"\n✓ Created {_env_local.name} from {_env_example.name}")
        else:
            _env_local.touch()
            print(f"\n✓ Created empty {_env_local.name}")

    env = _read_env_file(_env_local)

    # 2. API_KEY_SECRET
    if env.get("API_KEY_SECRET") and env["API_KEY_SECRET"] != "change-me-api-key-secret":
        print("\n✓ API_KEY_SECRET already set")
    else:
        secret = _generate_secret()
        _set_env_value("API_KEY_SECRET", secret)
        print("\n✓ API_KEY_SECRET generated and saved")

    # 3. JWT_SECRET
    if env.get("JWT_SECRET") and env["JWT_SECRET"] != "change-me-jwt-secret":
        print("✓ JWT_SECRET already set")
    else:
        secret = _generate_secret()
        _set_env_value("JWT_SECRET", secret)
        print("✓ JWT_SECRET generated and saved")

    # 4. OPENAI_API_KEY (optional)
    if env.get("OPENAI_API_KEY"):
        print("✓ OPENAI_API_KEY already set")
    else:
        print("\nOpenAI API key (for embeddings). Press Enter to skip if using Ollama.")
        key = input("  OPENAI_API_KEY: ").strip()
        if key:
            _set_env_value("OPENAI_API_KEY", key)
            print("  ✓ Saved")
        else:
            print("  → Skipped (Ollama or manual setup later)")

    # 5. Ollama detection
    ollama_url = env.get("OLLAMA_BASE_URL", "http://localhost:11434")
    try:
        import urllib.request

        req = urllib.request.Request(ollama_url, method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            if resp.status == 200:
                print(f"✓ Ollama detected at {ollama_url}")
                if not env.get("OPENAI_API_KEY"):
                    print("  → You can use Ollama for embeddings:")
                    print("    Set EMBEDDING_PROVIDER=ollama in .env.local")
    except Exception:
        if not env.get("OPENAI_API_KEY"):
            print(f"\n⚠ No Ollama at {ollama_url} and no OPENAI_API_KEY.")
            print("  Memory features require one of these. Configure before using remember/recall.")

    print("\n" + "=" * 50)
    print("✓ Environment setup complete!")
    print(f"  Config: {_env_local}")
    print("\nNext steps:")
    print("  1. docker compose up -d")
    print("  2. cd backend && alembic upgrade head")
    print("  3. cd backend && python -m src.cli.create_admin")
    print("=" * 50)


if __name__ == "__main__":
    setup_env()
