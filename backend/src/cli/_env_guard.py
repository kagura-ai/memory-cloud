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

import sys
from typing import TYPE_CHECKING

from pydantic import ValidationError

if TYPE_CHECKING:
    from config.settings import Settings


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
