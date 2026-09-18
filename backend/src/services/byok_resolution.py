"""Deployment-level switch for resolving stored BYOK keys (#1569).

``ENABLE_BYOK=false`` (#1167) only blocks *provisioning*; keys stored before
the flip keep being resolved by the LLM, embedding and reranker services.
``RESOLVE_STORED_BYOK_KEYS=false`` is the opt-in hardening that makes those
three services skip their ``external_api_keys`` lookups and resolve the
platform env/settings credential only — "BYOK off" then means off.

The three services call :func:`stored_byok_keys_disabled` right before their
lookup so the switch is checked in one place and logged once per process.
"""

from __future__ import annotations

from config.settings import get_settings
from utils.logger import get_logger

logger = get_logger(__name__)

# Log the switch the first time it actually skips a lookup, then stay quiet:
# one line per call would flood a deployment that runs this on every request.
_disabled_logged = False


def stored_byok_keys_disabled() -> bool:
    """True when this deployment must ignore ``external_api_keys`` rows.

    Reads ``Settings.resolve_stored_byok_keys`` (default True → False here, no
    behaviour change). Emits ``byok_key_resolution_disabled`` once per process
    the first time it returns True.
    """
    global _disabled_logged
    if get_settings().resolve_stored_byok_keys:
        return False
    if not _disabled_logged:
        _disabled_logged = True
        logger.info(
            "byok_key_resolution_disabled",
            hint=(
                "RESOLVE_STORED_BYOK_KEYS=false: external_api_keys rows are ignored; "
                "the LLM / embedding / reranker services use the platform credential only"
            ),
        )
    return True
