"""Every CLI names the refused secrets without a traceback (#2264).

Run as subprocesses: inside this pytest process the settings are already
cached. A command that built the settings at import, before its guard ran
(as importing ``config.plan_tiers`` did until #2276), would print pydantic's
``ValidationError`` traceback instead of the ``✗`` lines.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2]
_SECRETS = ("API_KEY_SECRET", "AUDIT_HMAC_KEY", "AUDIT_PSEUDO_SALT")
_UUID = "00000000-0000-0000-0000-000000000001"

_COMMANDS = {
    "apply_rerank_defaults": ["--all"],
    "backfill_deleted_context_files": [],
    "restore_context": [_UUID],
    "sweep_orphan_vectors": [],
    "transfer_context_creator": ["--from", "local:x", "--to", _UUID, "--workspace", _UUID],
    "reprocess_erasures": ["--all"],
    "migrate_context_embedding": ["--context", _UUID, "--to", "any-model"],
    "sync_llm_pricing": [],
    "create_admin": ["--skip-mcp-json"],
    "delete_admin": [],
    "reset_password": [],
    "seed_e2e_admin": [],
}


@pytest.mark.parametrize(("command", "argv"), list(_COMMANDS.items()), ids=list(_COMMANDS))
def test_a_refused_install_stops_with_the_variables_named(command, argv) -> None:
    env = {k: v for k, v in os.environ.items() if k not in _SECRETS and k != "ENVIRONMENT"}
    env |= {
        # Nothing listens on port 1: a run that gets past the guard fails on
        # the connection instead.
        "DATABASE_URL": "postgresql+asyncpg://nobody:nobody@127.0.0.1:1/none",
        "ENV_FILE": str(_BACKEND / "nonexistent.env"),
        "ENVIRONMENT": "production",
        "API_KEY_SECRET": "change-me-api-key-secret",
        "AUDIT_HMAC_KEY": secrets.token_hex(32),
        "AUDIT_PSEUDO_SALT": secrets.token_hex(16),
        "E2E_ADMIN_LOGIN_ID": "e2e-admin",
        "E2E_ADMIN_PASSWORD": "Valid-passw0rd!",
    }
    result = subprocess.run(
        [sys.executable, "-m", f"src.cli.{command}", *argv],
        cwd=_BACKEND,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 1, result.stderr
    assert "✗ " in result.stderr
    assert "refuses development-default or short secrets: API_KEY_SECRET" in result.stderr
    assert "change-me-api-key-secret" not in result.stderr
    assert "Traceback" not in result.stderr
