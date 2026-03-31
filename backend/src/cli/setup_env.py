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


_PLACEHOLDER_VALUES = {"change-me-api-key-secret", "change-me-jwt-secret", ""}


def _set_env_value(key: str, value: str):
    """Set key=value in .env.local (append if missing, replace if placeholder)."""
    env = _read_env_file(_env_local)
    existing = env.get(key)
    if existing and existing not in _PLACEHOLDER_VALUES:
        return  # Already set with real value, don't duplicate

    if existing is not None:
        # Replace placeholder in-place
        content = _env_local.read_text()
        lines = content.splitlines()
        new_lines = []
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                k, _, _ = stripped.partition("=")
                if k.strip() == key:
                    new_lines.append(f"{key}={value}")
                    continue
            new_lines.append(line)
        _env_local.write_text("\n".join(new_lines) + "\n")
    else:
        # Append new key
        with open(_env_local, "a") as f:
            f.write(f"\n{key}={value}\n")


def setup_env():
    """Interactive environment setup."""
    print("=" * 50)
    print("Kagura Memory Cloud - Environment Setup")
    print("=" * 50)

    # 1. Ensure .env.local exists (backup existing with timestamp)
    if _env_local.exists():
        import shutil
        from datetime import datetime

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        bak_path = _project_root / f".env.local.bak.{timestamp}"
        shutil.copy(_env_local, bak_path)
        print(f"\n✓ Backed up existing {_env_local.name} → {bak_path.name}")
    else:
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

    # 4. OPENAI_API_KEY (optional — check env var, .env.local, then prompt)
    import os

    existing_key = env.get("OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")
    if existing_key:
        if not env.get("OPENAI_API_KEY"):
            _set_env_value("OPENAI_API_KEY", existing_key)
            print("✓ OPENAI_API_KEY found in environment and saved to .env.local")
        else:
            print("✓ OPENAI_API_KEY already set")
    else:
        print("\nOpenAI API key (for embeddings). Press Enter to skip if using Ollama.")
        key = input("  OPENAI_API_KEY: ").strip()
        if key:
            _set_env_value("OPENAI_API_KEY", key)
            print("  ✓ Saved")
        else:
            print("  → Skipped (Ollama or manual setup later)")

    # 5. Ollama detection — re-read env to pick up any changes
    env = _read_env_file(_env_local)
    has_openai = bool(env.get("OPENAI_API_KEY"))
    ollama_url = env.get("OLLAMA_BASE_URL", "http://localhost:11434")
    try:
        import urllib.request

        req = urllib.request.Request(ollama_url, method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            if resp.status == 200:
                print(f"✓ Ollama detected at {ollama_url}")
                if not has_openai:
                    print("  → You can use Ollama for embeddings:")
                    print("    Set EMBEDDING_PROVIDER=ollama in .env.local")
    except Exception:
        if not has_openai:
            print(f"\n⚠ No Ollama at {ollama_url} and no OPENAI_API_KEY.")
            print("  Memory features require one of these. Configure before using remember/recall.")

    print("\n" + "=" * 50)
    print("✓ Environment setup complete!")
    print(f"  Config: {_env_local}")
    print("\nNext steps:")
    print("  1. cd backend && pip install -e '.[dev]' && pip install kagura-memory")
    print("  2. docker compose up -d")
    print("  3. cd backend && alembic upgrade head")
    print("  4. cd backend && python -m src.cli.create_admin")
    print("=" * 50)


if __name__ == "__main__":
    setup_env()
