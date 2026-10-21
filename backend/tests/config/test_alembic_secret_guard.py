"""``alembic`` runs the production secret guard before it migrates (#2239).

``alembic/env.py`` builds the settings before it touches the database, so
``deploy.sh`` stops at its migration step when ``.env.prod`` still holds a
placeholder. Run as a subprocess: env.py executes at import, and inside this
pytest process the settings are already cached.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[2]
_SECRETS = ("API_KEY_SECRET", "JWT_SECRET", "AUDIT_HMAC_KEY", "AUDIT_PSEUDO_SALT")


def _alembic_current(**overrides: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k not in _SECRETS and k != "ENVIRONMENT"}
    env |= {
        # Nothing listens on port 1: a run that gets past the guard fails on
        # the connection instead.
        "DATABASE_URL": "postgresql+asyncpg://nobody:nobody@127.0.0.1:1/none",
        "ENV_FILE": str(_BACKEND / "nonexistent.env"),
        "ENVIRONMENT": "production",
    }
    env |= overrides
    return subprocess.run(
        [sys.executable, "-m", "alembic", "current"],
        cwd=_BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_alembic_refuses_a_placeholder_secret_in_production() -> None:
    result = _alembic_current(
        API_KEY_SECRET="change-me-api-key-secret",
        JWT_SECRET=secrets.token_hex(32),
        AUDIT_HMAC_KEY=secrets.token_hex(32),
        AUDIT_PSEUDO_SALT=secrets.token_hex(16),
    )

    assert result.returncode != 0
    assert "refuses development-default or short secrets: API_KEY_SECRET" in result.stderr
    assert "change-me-api-key-secret" not in result.stderr
    # Refused before any connection attempt.
    assert "Connect call failed" not in result.stderr


def test_alembic_with_private_secrets_gets_past_the_guard() -> None:
    result = _alembic_current(
        API_KEY_SECRET=secrets.token_hex(32),
        JWT_SECRET=secrets.token_hex(32),
        AUDIT_HMAC_KEY=secrets.token_hex(32),
        AUDIT_PSEUDO_SALT=secrets.token_hex(16),
    )

    # Fails on the unreachable database, not on the guard.
    assert result.returncode != 0
    assert "refuses development-default" not in result.stderr
    assert "127.0.0.1" in result.stderr or "Connect" in result.stderr
