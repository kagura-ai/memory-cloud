"""CSRF token for the OAuth consent form (#2199).

The token is a keyed hash of the browser session id: the consent page renders
it in both forms and the consent POST recomputes it from the session cookie.
Nothing is stored, every tab of one session shares the token, and a new
sign-in (a new session id) invalidates an open consent page. A cross-site page
cannot compute it, since the session id lives in an HttpOnly cookie and the
key is a server secret (``Settings.audit_hmac_key``, domain-separated by the
prefix below, as for the known-device cookie hash).
"""

from __future__ import annotations

import hmac

from config.settings import get_settings
from utils.hashing import hmac_sha256_hex

_PREFIX = "oauth-consent-csrf:v1:"


def consent_csrf_token(session_id: str) -> str:
    """Return the consent form's CSRF token for ``session_id``.

    Args:
        session_id: The browser session id (the session cookie's value).

    Returns:
        64-character lowercase hex digest.
    """
    return hmac_sha256_hex(_PREFIX + session_id, get_settings().audit_hmac_key)


def consent_csrf_valid(session_id: object, presented: object) -> bool:
    """Whether ``presented`` is the consent CSRF token of ``session_id``.

    Type-strict and fail-closed: anything but two non-empty strings is
    invalid, so a missing value (or a test double) never reaches the HMAC.

    Args:
        session_id: The browser session id of the consenting request.
        presented: The ``csrf_token`` field of the consent form.

    Returns:
        True only when the token matches, compared in constant time.
    """
    if not isinstance(session_id, str) or not session_id:
        return False
    if not isinstance(presented, str) or not presented:
        return False
    expected = consent_csrf_token(session_id)
    return hmac.compare_digest(expected.encode(), presented.encode())
