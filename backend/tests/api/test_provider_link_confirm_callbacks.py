"""The callbacks' answer to a sign-in that may be confirmed by email (Issue #2786).

``ensure_user`` raises ``ProviderLinkConfirmable`` for a GitHub sign-in (or a
Google one without authority over the mailbox) with the address of an email
sign-up account. Pinned here:

- the response is byte-identical to today's ``email_in_use`` redirect (status,
  location, headers, no cookie) in every case;
- an offer is started (detached) only for the browser whose ``/login`` bound
  the flow: a flow that never ran ``/login`` (refresh, link) and an
  add-account or link-proof flow get the plain answer and nothing is recorded
  or mailed; a callback opened in another browser never gets that far (#2791:
  it is refused before the provider is asked, so no offer either);
- the flow's ``return_to`` travels with the offer only when it is safe;
- a plain ``ConflictError`` starts nothing.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import Response

from api.routes import auth as auth_routes
from tests.api.test_auth_terms_acceptance import (  # noqa: F401
    FakeRequest,
    github_idp,
    google_idp,
    redis,
    terms_off,
)
from utils.exceptions import ConflictError, ProviderLinkConfirmable
from utils.hashing import sha256_hex

PLAIN = "kagura_oauth_browser"


@pytest.fixture
def confirmable(monkeypatch) -> MagicMock:
    monkeypatch.setattr(auth_routes, "check_signup_access", AsyncMock(return_value=None))
    monkeypatch.setattr(auth_routes, "_maybe_link_redirect", AsyncMock(return_value=None))
    monkeypatch.setattr(auth_routes, "_terms_refusal", AsyncMock(return_value=None))
    ensure_user = AsyncMock(
        side_effect=ProviderLinkConfirmable(account_user_id="email:abc", account_stamp="stamp-1")
    )
    monkeypatch.setattr(
        auth_routes, "get_role_manager", lambda: SimpleNamespace(ensure_user=ensure_user)
    )
    spawn = MagicMock()
    monkeypatch.setattr(auth_routes, "spawn_link_offer", spawn)
    monkeypatch.setenv("GITHUB_CLIENT_ID", "client-id")
    manager = auth_routes._oauth2_manager
    if isinstance(manager, MagicMock):
        manager.get_authorization_url_web.return_value = "https://idp.example.test/auth"
    return spawn


async def _login(provider: str, *, return_to: str | None = "http://localhost:3000/device?c=1"):
    login = auth_routes.google_login if provider == "google" else auth_routes.github_login
    injected = Response()
    result = await login(FakeRequest(), response=injected, return_to=return_to)
    carrier = result if return_to else injected
    cookie = next(v.decode() for k, v in carrier.raw_headers if k.lower() == b"set-cookie")
    nonce = re.match(rf"{PLAIN}=([^;]+);", cookie)
    assert nonce
    return nonce.group(1)


def _state(redis) -> str:  # noqa: F811
    states = [k.split(":", 1)[1] for k in redis.store if k.startswith("oauth2_state:")]
    assert len(states) == 1
    return states[0]


async def _callback(provider: str, state: str, cookie: str | None):
    request = FakeRequest()
    if cookie is not None:
        request.cookies[PLAIN] = cookie
    callback = auth_routes.google_callback if provider == "google" else auth_routes.github_callback
    return await callback(request, code="c", state=state, error=None, error_description=None)


def _set_cookies_of(response) -> list[bytes]:
    return [v for k, v in response.raw_headers if k.lower() == b"set-cookie"]


def _same_as_email_in_use(response) -> None:
    plain = auth_routes._email_in_use_redirect()
    assert response.status_code == plain.status_code == 303
    assert response.raw_headers == plain.raw_headers
    assert response.body == plain.body
    assert not any(k.lower() == b"set-cookie" for k, _ in response.raw_headers)


@pytest.mark.parametrize("provider", ["github", "google"])
@pytest.mark.asyncio
async def test_the_bound_browser_gets_the_plain_answer_and_an_offer(
    redis,  # noqa: F811
    terms_off,  # noqa: F811
    github_idp,  # noqa: F811
    google_idp,  # noqa: F811
    confirmable,
    provider,
) -> None:
    nonce = await _login(provider)
    state = _state(redis)

    response = await _callback(provider, state, nonce)

    _same_as_email_in_use(response)
    confirmable.assert_called_once()
    kwargs = confirmable.call_args.kwargs
    assert kwargs["account_user_id"] == "email:abc"
    assert kwargs["account_stamp"] == "stamp-1"
    assert kwargs["provider"] == provider
    assert kwargs["browser_hash"] == sha256_hex(nonce)
    assert kwargs["return_to"] == "http://localhost:3000/device?c=1"
    assert kwargs["user_info"]["sub"] in ("583231", "108")
    # Nothing bound to the state is left behind.
    assert not any(state in key for key in redis.store)


@pytest.mark.parametrize("cookie", [None, "M" * 43])
@pytest.mark.parametrize("provider", ["github", "google"])
@pytest.mark.asyncio
async def test_a_callback_opened_in_another_browser_offers_nothing(
    redis,  # noqa: F811
    terms_off,  # noqa: F811
    github_idp,  # noqa: F811
    google_idp,  # noqa: F811
    confirmable,
    provider,
    cookie,
) -> None:
    await _login(provider)
    state = _state(redis)

    response = await _callback(provider, state, cookie)

    # #2791: refused before the provider is asked, so no account check runs.
    assert "error=oauth_other_browser" in response.headers["location"]
    assert _set_cookies_of(response) == []
    confirmable.assert_not_called()
    assert f"oauth2_flow:{state}" not in redis.store
    assert f"oauth2_state:{state}" not in redis.store


@pytest.mark.asyncio
async def test_a_flow_that_never_ran_login_offers_nothing(
    redis,  # noqa: F811
    terms_off,  # noqa: F811
    github_idp,  # noqa: F811
    confirmable,
) -> None:
    """A refresh or link flow writes its own state without a binding."""
    redis.store["oauth2_state:st1"] = "pending"
    redis.store["oauth2_state_intent:st1"] = "refresh"

    response = await _callback("github", "st1", "N" * 43)

    _same_as_email_in_use(response)
    confirmable.assert_not_called()


@pytest.mark.asyncio
async def test_an_add_account_flow_offers_nothing(
    redis,  # noqa: F811
    terms_off,  # noqa: F811
    github_idp,  # noqa: F811
    confirmable,
) -> None:
    nonce = await _login("github")
    state = _state(redis)
    redis.store[f"oauth2_add_to_session:{state}"] = "sess-1"

    response = await _callback("github", state, nonce)

    _same_as_email_in_use(response)
    confirmable.assert_not_called()
    assert f"oauth2_add_to_session:{state}" not in redis.store


@pytest.mark.asyncio
async def test_a_link_proof_flow_offers_nothing(
    redis,  # noqa: F811
    terms_off,  # noqa: F811
    google_idp,  # noqa: F811
    confirmable,
) -> None:
    nonce = await _login("google")
    state = _state(redis)
    redis.store[f"oauth2_link_proof:{state}"] = "1"

    response = await _callback("google", state, nonce)

    _same_as_email_in_use(response)
    confirmable.assert_not_called()


@pytest.mark.parametrize("return_to", ["https://evil.example/x", None])
@pytest.mark.asyncio
async def test_an_unsafe_or_missing_return_to_does_not_travel(
    redis,  # noqa: F811
    terms_off,  # noqa: F811
    github_idp,  # noqa: F811
    confirmable,
    return_to,
) -> None:
    nonce = await _login("github", return_to=return_to)
    state = _state(redis)

    await _callback("github", state, nonce)

    assert confirmable.call_args.kwargs["return_to"] is None


@pytest.mark.asyncio
async def test_a_plain_conflict_offers_nothing(
    redis,  # noqa: F811
    terms_off,  # noqa: F811
    github_idp,  # noqa: F811
    confirmable,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        auth_routes,
        "get_role_manager",
        lambda: SimpleNamespace(ensure_user=AsyncMock(side_effect=ConflictError("in use"))),
    )
    nonce = await _login("github")

    response = await _callback("github", _state(redis), nonce)

    _same_as_email_in_use(response)
    confirmable.assert_not_called()


@pytest.mark.asyncio
async def test_a_failing_offer_still_answers_email_in_use(
    redis,  # noqa: F811
    terms_off,  # noqa: F811
    github_idp,  # noqa: F811
    confirmable,
) -> None:
    confirmable.side_effect = RuntimeError("boom")
    nonce = await _login("github")

    response = await _callback("github", _state(redis), nonce)

    _same_as_email_in_use(response)
