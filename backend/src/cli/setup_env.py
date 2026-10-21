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
    for line in path.read_text(encoding="utf-8").splitlines():
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


# The server secrets setup_env generates (#2239). Each is replaced while it
# is missing or a placeholder; a private value is never touched.
_GENERATED_SECRETS: tuple[str, ...] = ("API_KEY_SECRET", "AUDIT_HMAC_KEY", "AUDIT_PSEUDO_SALT")

# The same placeholder rule as the production guard in ``config.settings``
# (``_DEV_SECRET_PLACEHOLDERS`` / ``_DEV_SECRET_PREFIXES``), so every value
# that guard refuses as a placeholder is replaced here. Duplicated because this
# bootstrap script stays stdlib-only; ``tests/cli/test_setup_env.py`` keeps the
# two in step.
_PLACEHOLDER_VALUES: frozenset[str] = frozenset({"", "kagura-erasure-v1"})
_PLACEHOLDER_PREFIXES: tuple[str, ...] = ("change-me", "dev-secret", "dev-audit")


def _is_placeholder(value: str) -> bool:
    """Whether ``value`` is a shipped placeholder (case-insensitive)."""
    folded = value.strip().lower()
    return folded in _PLACEHOLDER_VALUES or folded.startswith(_PLACEHOLDER_PREFIXES)


def _set_env_value(key: str, value: str):
    """Set key=value in .env.local (append if missing, replace if placeholder)."""
    env = _read_env_file(_env_local)
    existing = env.get(key)
    if existing and not _is_placeholder(existing):
        return  # Already set with real value, don't duplicate

    if existing is not None:
        # Replace placeholder in-place
        content = _env_local.read_text(encoding="utf-8")
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
        _env_local.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    else:
        # Append new key
        with open(_env_local, "a", encoding="utf-8") as f:
            f.write(f"\n{key}={value}\n")


def setup_env():
    """Interactive environment setup."""
    print("=" * 50)
    print("Kagura Memory Cloud - Environment Setup")
    print("=" * 50)

    # 1. Ensure .env.local exists (backup existing with timestamp)
    existed = _env_local.exists()
    if existed:
        import shutil
        from datetime import UTC, datetime

        # Bootstrap script — runs before utils.* imports are on sys.path,
        # so use stdlib directly (datetime.now(UTC) is aware, not DTZ-flagged).
        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
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

    # 2. Server secrets: API_KEY_SECRET, AUDIT_HMAC_KEY, AUDIT_PSEUDO_SALT
    print()
    replaced = False
    for key in _GENERATED_SECRETS:
        if not _is_placeholder(env.get(key, "")):
            print(f"✓ {key} already set")
        else:
            replaced = replaced or key in env
            _set_env_value(key, _generate_secret())
            print(f"✓ {key} generated and saved")
    if existed and replaced:
        print("  ⚠ A placeholder in the existing .env.local was replaced. If this install")
        print("    already holds data, read 'Production secrets' in docs/deployment.md.")

    # 3. OPENAI_API_KEY (optional — check env var, .env.local, then prompt)
    import os

    existing_key = env.get("OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")
    if existing_key:
        if not env.get("OPENAI_API_KEY"):
            _set_env_value("OPENAI_API_KEY", existing_key)
            print("✓ OPENAI_API_KEY found in environment and saved to .env.local")
        else:
            print("✓ OPENAI_API_KEY already set")
    else:
        print(
            "\nOpenAI API key (for embeddings). Press Enter to skip if using a "
            "self-hosted backend (e.g. Ollama/vLLM)."
        )
        key = input("  OPENAI_API_KEY: ").strip()
        if key:
            _set_env_value("OPENAI_API_KEY", key)
            print("  ✓ Saved")
        else:
            print("  → Skipped (self-hosted or manual setup later)")

    # 4. Self-hosted backend detection — re-read env to pick up any changes.
    # Probes the OpenAI-compatible /v1/models endpoint (Ollama, vLLM).
    env = _read_env_file(_env_local)
    has_openai = bool(env.get("OPENAI_API_KEY"))
    self_hosted_url = env.get("SELF_HOSTED_BASE_URL", "http://localhost:11434")
    self_hosted_key = env.get("SELF_HOSTED_API_KEY")
    from cli._env_probe import probe_self_hosted_available

    if probe_self_hosted_available(self_hosted_url, self_hosted_key):
        print(f"✓ Self-hosted backend detected at {self_hosted_url}")
        if not has_openai:
            print("  → You can use your self-hosted backend for embeddings:")
            print("    Set EMBEDDING_PROVIDER=self_hosted in .env.local")
    elif not has_openai:
        print(f"\n⚠ No self-hosted backend at {self_hosted_url} and no OPENAI_API_KEY.")
        print("  Memory features require one of these. Configure before using remember/recall.")

    print("\n" + "=" * 50)
    print("✓ Environment setup complete!")
    print(f"  Config: {_env_local}")
    print("\nNext steps:")
    print(
        "  1. cd backend && uv sync --locked --extra dev && uv tool install --upgrade kagura-memory"
    )
    print("  2. docker compose up -d")
    print("  3. cd backend && alembic upgrade head")
    print("  4. cd backend && python -m src.cli.create_admin")
    print("=" * 50)


if __name__ == "__main__":
    setup_env()
