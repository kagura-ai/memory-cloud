"""Run the production secret guard in the admin CLIs (#2239).

``create_admin``, ``delete_admin``, ``reset_password`` and ``seed_e2e_admin``
open their own synchronous engine from ``DATABASE_URL`` and never build
``Settings`` on their own, so the ``ENVIRONMENT=production`` refusal of
development-default secrets (#2225) did not reach them. Each calls
:func:`require_settings` first, before it touches the database.

A CLI that encrypts with ``API_KEY_SECRET`` resolves that secret (environment,
then the running API container) *before* calling this: ``get_settings()`` is
cached for the life of the process, and ``utils.encryption.get_encryptor``
reads the key from it. Passing the resolved value as ``api_key_secret`` makes
the order checkable — a mismatch means the settings were cached before the
secret was resolved, and data encrypted now could not be decrypted by the API.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import TYPE_CHECKING

from pydantic import ValidationError

if TYPE_CHECKING:
    from config.settings import Settings

# The ``container_name`` pinned by ``docker-compose.yml`` (the dev stack, the
# host-side case the README documents) and by
# ``terraform/single-server/docker-compose.{prod,app}.yml`` (the blue/green
# colors). Both colors take the same ``env_file``, so whichever is running
# holds the same values and the active-color marker is not needed.
API_CONTAINERS: tuple[str, ...] = ("kagura-api", "kagura-api-blue", "kagura-api-green")


def get_env_from_api_container(key: str) -> str | None:
    """Read ``key`` from the first running API container that has it (#2264).

    ``docker exec`` by container name needs no compose project, ``-f`` or
    working directory, so the same lookup works for the dev stack and the
    single-server stack. Any failure (no ``docker`` on ``PATH`` — the CLI runs
    inside a container —, the container is not running, a timeout) moves on to
    the next name. Worst case is 10 s per name on a hung Docker daemon.

    Returns:
        The non-empty value, or ``None`` when no container provided one.
    """
    for name in API_CONTAINERS:
        try:
            result = subprocess.run(
                ["docker", "exec", name, "printenv", key],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception:
            continue  # Docker not installed, not running or not answering
        value = result.stdout.strip()
        if result.returncode == 0 and value:
            return value
    return None


def resolve_api_key_secret() -> str | None:
    """Resolve ``API_KEY_SECRET``: the environment, then the API container.

    A value found in the container is exported into ``os.environ`` so the
    ``Settings`` built next (:func:`require_settings`) and
    ``utils.encryption.get_encryptor`` use it. Call this *before*
    :func:`require_settings`: the settings are cached for the process, and
    the other order would encrypt with this host's default instead of the
    API's key (#2239).
    """
    secret = os.getenv("API_KEY_SECRET")
    if secret:
        return secret
    secret = get_env_from_api_container("API_KEY_SECRET")
    if secret:
        os.environ["API_KEY_SECRET"] = secret
    return secret


def require_api_key_secret() -> str:
    """:func:`resolve_api_key_secret`, or exit 1 when there is none."""
    secret = resolve_api_key_secret()
    if secret:
        return secret
    print("✗ API_KEY_SECRET not available.")
    print("  Ensure Docker is running: docker compose up -d")
    sys.exit(1)


def require_settings(api_key_secret: str | None = None) -> Settings:
    """Build (or return the cached) ``Settings``; exit 1 when they are refused.

    Args:
        api_key_secret: The ``API_KEY_SECRET`` this CLI resolved, if any. The
            cached settings must hold the same value.

    Returns:
        The process-wide settings.
    """
    from config.settings import get_settings

    try:
        settings = get_settings()
    except ValidationError as exc:
        # The guard's message names the refused variables, never their values
        # (``hide_input_in_errors``); print it without a traceback.
        for error in exc.errors():
            print(f"✗ {error['msg']}", file=sys.stderr)
        sys.exit(1)
    if api_key_secret is not None and settings.api_key_secret != api_key_secret:
        print(
            "✗ API_KEY_SECRET changed after the settings were loaded; refusing to "
            "encrypt with a key the API would not use. Run the command again.",
            file=sys.stderr,
        )
        sys.exit(1)
    return settings
