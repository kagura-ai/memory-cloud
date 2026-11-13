"""Both OAuth callbacks tell the signup gate whether the address is verified (#2785).

An address matches a pending workspace invitation (or a pending Google row)
only when the provider reports it verified. Driven through the real handlers
up to the gate, which is stubbed to block so nothing is created:

- Google: ``email_verified is True`` only — ``False``, a missing claim or the
  string ``"true"`` all reach the gate as False;
- GitHub: True — ``_github_get_user_info`` keeps only the verified primary
  address and raises otherwise (``tests/api/test_github_provider_user_info.py``).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.responses import RedirectResponse

from api.routes import auth as auth_routes
from tests.api.test_auth_beta_invite_plumbing import FakeRedis, FakeRequest
from tests.oauth_browser_helpers import bind_browser, from_bound_browser

_MISSING = object()


@pytest.fixture
def redis(monkeypatch) -> FakeRedis:
    manager = MagicMock()
    manager._redis = FakeRedis()
    monkeypatch.setattr(auth_routes, "_session_manager", manager)
    return manager._redis


@pytest.fixture
def gate(monkeypatch) -> AsyncMock:
    blocked = RedirectResponse("http://localhost:3000/signup-blocked", status_code=303)
    gate = AsyncMock(return_value=blocked)
    monkeypatch.setattr(auth_routes, "check_signup_access", gate)
    monkeypatch.setattr(auth_routes, "_maybe_link_redirect", AsyncMock(return_value=None))
    return gate


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "claim,expected",
    [(True, True), (False, False), (_MISSING, False), ("true", False), (1, False)],
)
async def test_google_callback_passes_the_strict_verified_claim(
    redis, gate, monkeypatch, claim, expected
) -> None:
    user_info: dict = {"sub": "108", "email": "n@example.test"}
    if claim is not _MISSING:
        user_info["email_verified"] = claim
    manager = MagicMock()
    manager.get_user_info_web.return_value = user_info
    monkeypatch.setattr(auth_routes, "_oauth2_manager", manager)
    monkeypatch.setenv("GOOGLE_REDIRECT_URI", "http://localhost:8080/cb")
    redis.store["oauth2_state:st1"] = "pending"
    bind_browser(redis, "st1")

    await auth_routes.google_callback(
        from_bound_browser(FakeRequest()),
        code="c",
        state="st1",
        error=None,
        error_description=None,
    )

    assert gate.await_args.kwargs["provider"] == "google"
    assert gate.await_args.kwargs["email_verified"] is expected


@pytest.mark.asyncio
async def test_github_callback_passes_the_verified_primary_address(
    redis, gate, monkeypatch
) -> None:
    monkeypatch.setattr(auth_routes, "_oauth2_manager", MagicMock())
    monkeypatch.setattr(auth_routes, "_github_exchange_code", AsyncMock(return_value="at"))
    monkeypatch.setattr(
        auth_routes,
        "_github_get_user_info",
        AsyncMock(
            return_value={
                "sub": "583231",
                "email": "n@example.test",
                "login": "octo",
                "email_verified": True,
            }
        ),
    )
    redis.store["oauth2_state:st2"] = "pending"
    bind_browser(redis, "st2")

    await auth_routes.github_callback(
        from_bound_browser(FakeRequest()),
        code="c",
        state="st2",
        error=None,
        error_description=None,
    )

    assert gate.await_args.kwargs["provider"] == "github"
    assert gate.await_args.kwargs["email_verified"] is True
