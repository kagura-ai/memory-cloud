"""Callback tests: finish a Google / GitHub sign-in in the browser that started it.

``GET /auth/{google,github}/login`` gives the browser a nonce cookie and binds
its digest to the flow's state under ``oauth2_flow:{state}``; the callbacks
finish a sign-in only for the browser that presents that nonce (#2791). A test
that drives a callback past the state check writes the same binding with
``bind_browser`` and sends the cookie with ``from_bound_browser``:

    redis.store["oauth2_state:st1"] = "pending"
    bind_browser(redis, "st1")
    await google_callback(from_bound_browser(FakeRequest()), code="c", state="st1", ...)

``assume_bound_browser`` replaces the binding check itself. It is meant only
for the callback tests whose Redis is a ``MagicMock`` (nothing can be bound
there) and that are not about the browser: import it explicitly and request it
by name; it is never autouse and never lives in a ``conftest.py``.
"""

from __future__ import annotations

from typing import Any, Final

import pytest

from api.routes import auth as auth_routes
from auth import oauth_browser

# The shape ``secrets.token_urlsafe(32)`` produces (43 URL-safe characters).
BOUND_NONCE: Final = "b" * 43


def bind_browser(redis: Any, state: str) -> str:
    """Write what ``/login`` writes for the browser holding ``BOUND_NONCE``.

    Returns the digest stored under ``oauth2_flow:{state}``.
    """
    digest = oauth_browser.nonce_digest(BOUND_NONCE)
    redis.setex(
        oauth_browser.OAUTH_FLOW_KEY.format(state=state),
        oauth_browser.OAUTH_FLOW_TTL_SECONDS,
        digest,
    )
    return digest


def from_bound_browser(request: Any) -> Any:
    """Give a fake request (with a real ``cookies`` dict) the bound browser's cookie."""
    request.cookies[oauth_browser.cookie_name()] = BOUND_NONCE
    return request


@pytest.fixture
def assume_bound_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    """Treat every callback as coming from the browser that started it.

    Only for MagicMock-Redis callback tests that are not about the browser.
    """
    digest = oauth_browser.nonce_digest(BOUND_NONCE)
    monkeypatch.setattr(
        auth_routes,
        "_take_oauth_flow",
        lambda state, request: auth_routes.OAuthFlowBinding(record=True, digest=digest),
    )
