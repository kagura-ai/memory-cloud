"""The browser nonce of a Google / GitHub sign-in (Issue #2786).

``GET /auth/{google,github}/login`` gives the browser a random nonce in an
HttpOnly cookie and binds its digest to the flow's OAuth ``state``. A provider
link confirmed by email (``services/provider_link_confirmation_service.py``)
completes only in a browser that presents the nonce the sign-in was bound to:
the emailed token proves the mailbox, the nonce proves the browser that started
the sign-in, and neither links anything alone.

The binding is written only by the two ``/login`` routes, never by a callback:
the confirmation is bound to the browser that started the sign-in.

- In production (``Secure`` cookies) the cookie is ``__Host-kagura_oauth_browser``
  with ``Path=/`` and no ``Domain``, so no other host of the domain can set or
  shadow it. Over plain http (development) the prefix is not allowed and the
  plain name is used.
- A presented nonce is reused only while the server-side marker of its digest
  exists (the server issued it and it has not expired); anything else is
  replaced by a new one. Every ``/login`` refreshes the marker and the cookie.
- Only digests are stored server-side; the nonce itself is never logged.
"""

from __future__ import annotations

import hmac
import re

from auth.session import browser_cookie_attrs
from utils.hashing import sha256_hex

# Cookie + marker lifetime: the OAuth state (5 minutes) plus the pending link
# (15 minutes), so a nonce never outlives the last record it can match.
OAUTH_BROWSER_TTL_SECONDS = 20 * 60
# oauth2_flow:{state} lives as long as the state itself.
OAUTH_FLOW_TTL_SECONDS = 300

_PLAIN_NAME = "kagura_oauth_browser"
_HOST_NAME = "__Host-kagura_oauth_browser"
_NONCE_RE = re.compile(r"[A-Za-z0-9_-]{43}")

OAUTH_FLOW_KEY = "oauth2_flow:{state}"
_MARKER_KEY = "oauth_browser:{digest}"


def cookie_name() -> str:
    """The nonce cookie's name: ``__Host-`` prefixed whenever cookies are Secure."""
    return _HOST_NAME if browser_cookie_attrs()["secure"] else _PLAIN_NAME


def well_formed(nonce: str | None) -> bool:
    """Whether ``nonce`` has the shape ``secrets.token_urlsafe(32)`` produces."""
    return bool(nonce) and _NONCE_RE.fullmatch(nonce or "") is not None


def nonce_digest(nonce: str) -> str:
    """SHA-256 hex of the nonce: what Redis and a pending link hold."""
    return sha256_hex(nonce)


def marker_key(digest: str) -> str:
    """Redis key that says the server issued the nonce with this digest."""
    return _MARKER_KEY.format(digest=digest)


def presented_nonce(cookies: dict[str, str] | object) -> str | None:
    """The well-formed nonce the request carries under the current name, or None."""
    getter = getattr(cookies, "get", None)
    value = getter(cookie_name()) if callable(getter) else None
    return value if isinstance(value, str) and well_formed(value) else None


def matches(nonce: str | None, digest: str | None) -> bool:
    """Constant-time check that ``nonce`` hashes to ``digest``."""
    if not nonce or not digest:
        return False
    return hmac.compare_digest(nonce_digest(nonce), digest)


def cookie_attrs() -> dict[str, object]:
    """``set_cookie`` attributes shared by setting and deleting the cookie."""
    return dict(browser_cookie_attrs())
