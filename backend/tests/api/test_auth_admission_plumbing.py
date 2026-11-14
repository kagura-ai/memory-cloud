"""The OAuth callbacks hand the gate's admission to ``ensure_user`` (#2803).

``check_signup_access`` answers a redirect (blocked), an ``AdmissionRecord``
(a new account may be created, and this is how it was admitted) or None (an
existing account). An ``AdmissionRecord`` is truthy, so a callback that kept
``if blocked: return blocked`` would answer the browser with the record
itself. Driven through both real handlers:

- a redirect is returned as is and ``ensure_user`` never runs;
- a record goes to ``ensure_user(admission=...)`` and never becomes the
  response;
- None goes to ``ensure_user(admission=None)``.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.responses import RedirectResponse

from api.routes import auth as auth_routes
from auth.admission import AdmissionRecord
from tests.oauth_browser_helpers import bind_browser, from_bound_browser
from utils.exceptions import ConflictError


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def setex(self, key: str, _ttl: int, value: str) -> None:
        self.store[key] = value

    def set(self, key: str, value: str, **_kw) -> bool:
        self.store[key] = value
        return True

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def getdel(self, key: str) -> str | None:
        return self.store.pop(key, None)

    def delete(self, *keys: str) -> int:
        return sum(1 for k in keys if self.store.pop(k, None) is not None)


class FakeRequest:
    def __init__(self) -> None:
        self.cookies: dict[str, str] = {}
        self.headers = {"user-agent": "pytest"}
        self.client = MagicMock(host="203.0.113.7")


RECORD = AdmissionRecord("beta_invite", beta_invite_id=uuid.uuid4(), inviter_user_id="u-inviter")
BLOCKED = RedirectResponse("http://localhost:3000/signup-blocked", status_code=303)


@pytest.fixture
def wired(monkeypatch) -> SimpleNamespace:
    manager = MagicMock()
    manager._redis = FakeRedis()
    monkeypatch.setattr(auth_routes, "_session_manager", manager)
    monkeypatch.setattr(auth_routes, "_maybe_link_redirect", AsyncMock(return_value=None))
    monkeypatch.setattr(auth_routes, "_terms_refusal", AsyncMock(return_value=None))
    # ensure_user ends the callback with a known answer, so nothing after it runs.
    ensure_user = AsyncMock(side_effect=ConflictError("taken"))
    monkeypatch.setattr(
        auth_routes, "get_role_manager", lambda: SimpleNamespace(ensure_user=ensure_user)
    )
    google = MagicMock()
    google.get_user_info_web.return_value = {
        "sub": "108",
        "email": "n@example.test",
        "email_verified": True,
    }
    monkeypatch.setattr(auth_routes, "_oauth2_manager", google)
    monkeypatch.setenv("GOOGLE_REDIRECT_URI", "http://localhost:8080/cb")
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
    return SimpleNamespace(redis=manager._redis, ensure_user=ensure_user)


async def _callback(provider: str, wired: SimpleNamespace):
    state = f"st-{provider}"
    wired.redis.store[f"oauth2_state:{state}"] = "pending"
    bind_browser(wired.redis, state)
    handler = auth_routes.google_callback if provider == "google" else auth_routes.github_callback
    return await handler(
        from_bound_browser(FakeRequest()),
        code="c",
        state=state,
        error=None,
        error_description=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["google", "github"])
async def test_a_redirect_is_returned_and_no_account_is_made(provider, wired, monkeypatch) -> None:
    monkeypatch.setattr(auth_routes, "check_signup_access", AsyncMock(return_value=BLOCKED))

    response = await _callback(provider, wired)

    assert response is BLOCKED
    wired.ensure_user.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["google", "github"])
async def test_a_record_goes_to_ensure_user_and_never_answers(provider, wired, monkeypatch) -> None:
    monkeypatch.setattr(auth_routes, "check_signup_access", AsyncMock(return_value=RECORD))

    response = await _callback(provider, wired)

    wired.ensure_user.assert_awaited_once()
    assert wired.ensure_user.await_args.kwargs["admission"] is RECORD
    assert not isinstance(response, AdmissionRecord)
    assert isinstance(response, RedirectResponse)
    assert "email_in_use" in response.headers["location"]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["google", "github"])
async def test_a_login_passes_no_admission(provider, wired, monkeypatch) -> None:
    monkeypatch.setattr(auth_routes, "check_signup_access", AsyncMock(return_value=None))

    await _callback(provider, wired)

    wired.ensure_user.assert_awaited_once()
    assert wired.ensure_user.await_args.kwargs["admission"] is None


def test_the_callbacks_never_test_the_gate_by_truthiness() -> None:
    """The source of both callbacks tests the answer with isinstance only."""
    import inspect

    for handler in (auth_routes.google_callback, auth_routes.github_callback):
        source = inspect.getsource(handler)
        assert "isinstance(gate, RedirectResponse)" in source
        assert "if blocked" not in source
        assert "if gate:" not in source
