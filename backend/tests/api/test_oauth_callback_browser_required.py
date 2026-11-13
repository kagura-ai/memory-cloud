"""A Google / GitHub sign-in finishes only in the browser that started it (#2791).

``GET /auth/{google,github}/login`` gives the browser a nonce cookie and binds
its digest to the flow's state (#2786). Pinned here, for both providers:

- a callback whose browser does not present the bound nonce (no cookie, another
  server-issued nonce, an unknown or malformed one, or a flow with no binding at
  all) answers ``303 /login?error=oauth_other_browser&provider=<p>`` before the
  provider is asked anything: no exchange, no signup gate, no account, no
  session, no ``Set-Cookie``, no ``return_to``, and nothing bound to the state
  is left in Redis;
- the browser that ran ``/login`` signs in as before (JSON and redirect mode,
  plain and ``__Host-`` cookie, two flows in two tabs);
- flows started from Profile (``refresh`` / ``link`` intents, which never ran
  ``/login``) still reach their own branch; one whose intent is gone by then is
  refused before an account check (``link``) or before any session (both);
- the refusal is logged without the nonce, the state or the code;
- the Google sign-in always uses the configured redirect URI.
"""

from __future__ import annotations

import ast
import inspect
import re
import textwrap
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import Response
from fastapi.responses import RedirectResponse
from structlog.testing import capture_logs

from api.routes import auth as auth_routes
from api.routes.auth import SessionOwner
from auth import oauth_browser
from auth.oauth2 import OAuth2Manager
from tests.api.test_auth_terms_acceptance import (  # noqa: F401
    FakeRequest,
    github_idp,
    google_idp,
    redis,
    terms_off,
)
from tests.oauth_browser_helpers import BOUND_NONCE, bind_browser, from_bound_browser
from utils.datetime import utcnow

PLAIN = "kagura_oauth_browser"
HOST = "__Host-kagura_oauth_browser"
RETURN_TO = "http://localhost:3000/device?c=1"
OWNER_ID = "owner-1"
PROVIDERS = ["github", "google"]


@pytest.fixture
def secure_cookies(monkeypatch) -> None:
    attrs = {"path": "/", "httponly": True, "secure": True, "samesite": "lax"}
    monkeypatch.setattr(oauth_browser, "browser_cookie_attrs", lambda: dict(attrs))
    monkeypatch.setattr(auth_routes, "browser_cookie_attrs", lambda: dict(attrs))


@pytest.fixture
def signed_in_path(monkeypatch, redis) -> SimpleNamespace:  # noqa: F811
    """Everything after the provider exchange, stubbed to sign the identity in."""
    manager = auth_routes._session_manager
    manager.delete_user_sessions.return_value = 0
    manager.create_session.return_value = "sess-1"
    gate = AsyncMock(return_value=None)
    monkeypatch.setattr(auth_routes, "check_signup_access", gate)
    terms = AsyncMock(return_value=None)
    monkeypatch.setattr(auth_routes, "_terms_refusal", terms)
    monkeypatch.setattr(auth_routes, "_record_terms_acceptance", AsyncMock())
    monkeypatch.setattr(auth_routes, "_note_provider_sign_in", AsyncMock())
    role = MagicMock(value="user")
    ensure_user = AsyncMock(return_value=role)
    monkeypatch.setattr(
        auth_routes, "get_role_manager", lambda: SimpleNamespace(ensure_user=ensure_user)
    )
    owner = SessionOwner(
        OWNER_ID,
        "n@example.test",
        "N",
        None,
        provider_linked_at=None,
        account_created_at=utcnow() - timedelta(days=30),
    )
    monkeypatch.setattr(auth_routes, "_session_owner", AsyncMock(return_value=owner))
    monkeypatch.setattr(auth_routes, "_account_exists", AsyncMock(return_value=False))

    async def _fake_db():
        yield MagicMock()

    monkeypatch.setattr(auth_routes, "get_db", _fake_db)
    invitations = MagicMock()
    invitations.return_value.get_pending_invitations_for_email = AsyncMock(return_value=[])
    monkeypatch.setattr("services.invitation_service.InvitationService", invitations)
    workspaces = MagicMock()
    workspaces.return_value.ensure_personal_workspace = AsyncMock()
    monkeypatch.setattr(auth_routes, "WorkspaceService", workspaces)
    monkeypatch.setenv("GITHUB_CLIENT_ID", "client-id")
    google = auth_routes._oauth2_manager
    if isinstance(google, MagicMock):
        google.get_authorization_url_web.return_value = "https://idp.example.test/auth"
    return SimpleNamespace(
        manager=manager, gate=gate, terms=terms, ensure_user=ensure_user, redis=redis
    )


def _set_cookies(response) -> list[str]:
    return [v.decode() for k, v in response.raw_headers if k.lower() == b"set-cookie"]


async def _login(
    provider: str,
    *,
    return_to: str | None = RETURN_TO,
    request: FakeRequest | None = None,
    cookie_name: str = PLAIN,
) -> str:
    """Run ``/login`` like a browser; returns the nonce the response sets."""
    login = auth_routes.google_login if provider == "google" else auth_routes.github_login
    injected = Response()
    result = await login(request or FakeRequest(), response=injected, return_to=return_to)
    carrier = result if return_to else injected
    found = [c for c in _set_cookies(carrier) if c.startswith(f"{cookie_name}=")]
    assert len(found) == 1, _set_cookies(carrier)
    nonce = re.match(rf"{re.escape(cookie_name)}=([^;]+);", found[0])
    assert nonce
    return nonce.group(1)


def _states(redis) -> set[str]:  # noqa: F811
    return {k.split(":", 1)[1] for k in redis.store if k.startswith("oauth2_state:")}


async def _callback(
    provider: str, state: str, cookie: str | None, *, cookie_name: str = PLAIN, code: str = "c"
):
    request = FakeRequest()
    if cookie is not None:
        request.cookies[cookie_name] = cookie
    callback = auth_routes.google_callback if provider == "google" else auth_routes.github_callback
    return await callback(request, code=code, state=state, error=None, error_description=None)


def _exchange(provider: str):
    if provider == "github":
        return auth_routes._github_exchange_code
    return auth_routes._oauth2_manager.exchange_code_web


def _assert_refused(response, provider: str) -> None:
    assert response.status_code == 303
    location = urlparse(response.headers["location"])
    assert location.path == "/login"
    assert parse_qs(location.query) == {"error": ["oauth_other_browser"], "provider": [provider]}
    assert "return_to" not in response.headers["location"]
    assert _set_cookies(response) == []


def _assert_nothing_happened(path: SimpleNamespace) -> None:
    path.gate.assert_not_awaited()
    path.ensure_user.assert_not_awaited()
    path.manager.create_session.assert_not_called()
    path.manager.add_account.assert_not_called()
    path.manager.delete_user_sessions.assert_not_called()


def _with_carriers(redis, state: str) -> None:  # noqa: F811
    """Everything else a flow may carry beside its state."""
    redis.store[f"oauth2_beta_invite:{state}"] = "invite-hash"
    redis.store[f"oauth2_accepted_terms:{state}"] = "2026-09"
    redis.store[f"oauth2_signup_prefs:{state}"] = "{}"
    redis.store[f"oauth2_add_to_session:{state}"] = "sess-0"
    redis.store[f"oauth2_link_proof:{state}"] = "1"


# --- the refusal --------------------------------------------------------------


@pytest.mark.parametrize("presented", ["none", "other_issued", "unknown", "malformed"])
@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.asyncio
async def test_a_callback_opened_in_another_browser_is_refused_before_the_provider(
    redis,  # noqa: F811
    terms_off,  # noqa: F811
    github_idp,  # noqa: F811
    google_idp,  # noqa: F811
    signed_in_path,
    provider,
    presented,
) -> None:
    await _login(provider)
    (state,) = _states(redis)
    _with_carriers(redis, state)
    cookie = {
        "none": None,
        "unknown": "M" * 43,
        "malformed": "not-a-nonce",
    }.get(presented)
    if presented == "other_issued":
        # Another browser that ran /login itself holds a live, server-issued nonce.
        cookie = await _login(provider)

    response = await _callback(provider, state, cookie)

    _assert_refused(response, provider)
    _exchange(provider).assert_not_called()
    _assert_nothing_happened(signed_in_path)
    assert not any(state in key for key in redis.store)


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.asyncio
async def test_a_flow_without_a_browser_binding_is_refused(
    redis,  # noqa: F811
    terms_off,  # noqa: F811
    github_idp,  # noqa: F811
    google_idp,  # noqa: F811
    signed_in_path,
    provider,
) -> None:
    """A state with no binding (e.g. issued before the binding existed)."""
    redis.store["oauth2_state:st1"] = "pending"
    redis.store["oauth2_return_to:st1"] = RETURN_TO

    response = await _callback(provider, "st1", BOUND_NONCE)

    _assert_refused(response, provider)
    _exchange(provider).assert_not_called()
    _assert_nothing_happened(signed_in_path)
    assert not any("st1" in key for key in redis.store)


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.asyncio
async def test_both_providers_answer_the_same_refusal(
    redis,  # noqa: F811
    terms_off,  # noqa: F811
    github_idp,  # noqa: F811
    google_idp,  # noqa: F811
    signed_in_path,
    provider,
) -> None:
    redis.store["oauth2_state:st1"] = "pending"

    response = await _callback(provider, "st1", None)

    expected = auth_routes._oauth_error_redirect(provider, "oauth_other_browser")
    assert response.status_code == expected.status_code
    assert response.raw_headers == expected.raw_headers
    assert response.body == expected.body


def test_the_refusal_token_is_part_of_the_error_vocabulary() -> None:
    assert "oauth_other_browser" in auth_routes._OAUTH_ERROR_REASONS
    location = auth_routes._oauth_error_redirect("github", "oauth_other_browser").headers[
        "location"
    ]
    assert "error=oauth_other_browser" in location


# --- the browser that started it -----------------------------------------------


@pytest.mark.parametrize("return_to", [RETURN_TO, None], ids=["redirect_mode", "json_mode"])
@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.asyncio
async def test_the_browser_that_started_the_sign_in_finishes_it(
    redis,  # noqa: F811
    terms_off,  # noqa: F811
    github_idp,  # noqa: F811
    google_idp,  # noqa: F811
    signed_in_path,
    provider,
    return_to,
) -> None:
    nonce = await _login(provider, return_to=return_to)
    (state,) = _states(redis)

    response = await _callback(provider, state, nonce)

    assert response.status_code == 303
    assert "error=" not in response.headers["location"]
    if return_to:
        assert response.headers["location"] == return_to
    assert any(c.startswith("kagura_session=sess-1") for c in _set_cookies(response))
    signed_in_path.manager.create_session.assert_called_once()
    assert not any(state in key for key in redis.store)


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.asyncio
async def test_the_starting_browser_finishes_with_the_host_prefixed_cookie(
    redis,  # noqa: F811
    terms_off,  # noqa: F811
    github_idp,  # noqa: F811
    google_idp,  # noqa: F811
    signed_in_path,
    secure_cookies,
    provider,
) -> None:
    nonce = await _login(provider, cookie_name=HOST)
    (state,) = _states(redis)

    response = await _callback(provider, state, nonce, cookie_name=HOST)

    assert response.status_code == 303
    assert "error=" not in response.headers["location"]
    signed_in_path.manager.create_session.assert_called_once()


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.asyncio
async def test_two_sign_ins_in_two_tabs_of_one_browser_both_finish(
    redis,  # noqa: F811
    terms_off,  # noqa: F811
    github_idp,  # noqa: F811
    google_idp,  # noqa: F811
    signed_in_path,
    provider,
) -> None:
    nonce = await _login(provider)
    (first,) = _states(redis)
    tab = FakeRequest()
    tab.cookies[PLAIN] = nonce
    assert await _login(provider, request=tab) == nonce
    (second,) = _states(redis) - {first}

    for state in (first, second):
        response = await _callback(provider, state, nonce)
        assert "error=" not in response.headers["location"], state

    assert signed_in_path.manager.create_session.call_count == 2


def test_the_test_helper_binds_like_login(redis) -> None:  # noqa: F811
    redis.store["oauth2_state:st1"] = "pending"
    digest = bind_browser(redis, "st1")

    request = from_bound_browser(FakeRequest())

    assert auth_routes._take_oauth_flow_binding("st1", request) == digest


# --- flows started from Profile ---------------------------------------------------


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.asyncio
async def test_a_profile_refresh_still_reaches_its_branch(
    redis,  # noqa: F811
    terms_off,  # noqa: F811
    github_idp,  # noqa: F811
    google_idp,  # noqa: F811
    signed_in_path,
    provider,
) -> None:
    redis.store["oauth2_state:st1"] = "pending"
    redis.store["oauth2_state_intent:st1"] = "refresh"
    redis.store["oauth2_state_user:st1"] = OWNER_ID

    response = await _callback(provider, "st1", None)

    location = urlparse(response.headers["location"])
    assert (location.path, location.query) == ("/profile", "refreshed=1")
    assert _set_cookies(response) == []
    signed_in_path.manager.create_session.assert_not_called()


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.asyncio
async def test_a_profile_link_still_reaches_its_branch(
    redis,  # noqa: F811
    terms_off,  # noqa: F811
    github_idp,  # noqa: F811
    google_idp,  # noqa: F811
    signed_in_path,
    monkeypatch,
    provider,
) -> None:
    redis.store["oauth2_state:st1"] = "pending"
    redis.store["oauth2_state_intent:st1"] = "link"
    linked = RedirectResponse("http://localhost:3000/profile?linked=1", status_code=303)
    link_branch = AsyncMock(return_value=linked)
    monkeypatch.setattr(auth_routes, "_maybe_link_redirect", link_branch)

    response = await _callback(provider, "st1", None)

    assert response is linked
    link_branch.assert_awaited_once()
    _assert_nothing_happened(signed_in_path)


def _drop_intent_during_the_exchange(provider: str, redis) -> None:  # noqa: F811
    """The intent expires between the early check and its own branch."""

    def drop(*_args, **_kwargs):
        redis.store.pop("oauth2_state_intent:st1", None)
        return "at"

    _exchange(provider).side_effect = drop


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.asyncio
async def test_a_refresh_intent_gone_during_the_exchange_opens_no_session(
    redis,  # noqa: F811
    terms_off,  # noqa: F811
    github_idp,  # noqa: F811
    google_idp,  # noqa: F811
    signed_in_path,
    provider,
) -> None:
    redis.store["oauth2_state:st1"] = "pending"
    redis.store["oauth2_state_intent:st1"] = "refresh"
    redis.store["oauth2_state_user:st1"] = OWNER_ID
    _drop_intent_during_the_exchange(provider, redis)

    response = await _callback(provider, "st1", None)

    _assert_refused(response, provider)
    signed_in_path.manager.create_session.assert_not_called()
    signed_in_path.manager.add_account.assert_not_called()
    signed_in_path.manager.delete_user_sessions.assert_not_called()
    assert not any("st1" in key for key in redis.store)


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.asyncio
async def test_a_link_intent_gone_during_the_exchange_creates_nothing(
    redis,  # noqa: F811
    terms_off,  # noqa: F811
    github_idp,  # noqa: F811
    google_idp,  # noqa: F811
    signed_in_path,
    provider,
) -> None:
    redis.store["oauth2_state:st1"] = "pending"
    redis.store["oauth2_state_intent:st1"] = "link"
    redis.store["oauth2_state_user:st1"] = OWNER_ID
    redis.store["oauth2_state_session:st1"] = "sess-0"
    _drop_intent_during_the_exchange(provider, redis)

    response = await _callback(provider, "st1", None)

    _assert_refused(response, provider)
    signed_in_path.terms.assert_not_awaited()
    _assert_nothing_happened(signed_in_path)
    assert not any("st1" in key for key in redis.store)


# --- the log ---------------------------------------------------------------------


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.asyncio
async def test_the_refusal_is_logged_without_the_nonce_state_or_code(
    redis,  # noqa: F811
    terms_off,  # noqa: F811
    github_idp,  # noqa: F811
    google_idp,  # noqa: F811
    signed_in_path,
    provider,
) -> None:
    await _login(provider)
    (state,) = _states(redis)
    other = "Q" * 43
    code = "code-value-7f3a"

    with capture_logs() as logs:
        await _callback(provider, state, other, code=code)

    events = [e for e in logs if e["event"] == "oauth_callback_not_from_starting_browser"]
    assert len(events) == 1
    assert events[0]["provider"] == provider
    assert events[0]["state_hash"] == auth_routes._state_hash(state)
    assert events[0]["cookie_present"] is True
    assert events[0]["flow_record"] is True
    text = repr(logs)
    for secret in (state, other, code):
        assert secret not in text


@pytest.mark.asyncio
async def test_the_log_tells_a_missing_cookie_from_a_missing_record(
    redis,  # noqa: F811
    terms_off,  # noqa: F811
    github_idp,  # noqa: F811
    signed_in_path,
) -> None:
    redis.store["oauth2_state:st1"] = "pending"

    with capture_logs() as logs:
        await _callback("github", "st1", None)

    (event,) = [e for e in logs if e["event"] == "oauth_callback_not_from_starting_browser"]
    assert event["cookie_present"] is False
    assert event["flow_record"] is False


# --- the order inside the callbacks (measured on the AST) --------------------------


def _calls(fn) -> dict[str, list[int]]:
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    found: dict[str, list[int]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else func.id
                if isinstance(func, ast.Name)
                else None
            )
            if name:
                found.setdefault(name, []).append(node.lineno)
    return found


def _refusal_guards(fn) -> dict[str, list[int]]:
    """Line of each ``if`` that ends the flow with the other-browser refusal, by test."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    found: dict[str, list[int]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        body_calls = [
            n.func.id
            for stmt in node.body
            for n in ast.walk(stmt)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        ]
        if "_other_browser_refusal" not in body_calls:
            continue
        found.setdefault(ast.unparse(node.test), []).append(node.lineno)
    return found


@pytest.mark.parametrize(
    ("fn_name", "exchange"),
    [("google_callback", "_run_google_sync"), ("github_callback", "_github_exchange_code")],
)
def test_the_browser_is_checked_before_the_provider_and_the_account(fn_name, exchange) -> None:
    fn = getattr(auth_routes, fn_name)
    calls = _calls(fn)
    guards = _refusal_guards(fn)
    early = min(calls["_starting_browser_check"])
    (early_guard,) = guards["browser_check is None"]
    (link_guard,) = guards["browser_check == 'link'"]
    assert early < early_guard < min(calls[exchange])
    after_link_branch = min(calls["_maybe_link_redirect"])
    for name in ("_terms_refusal", "check_signup_access", "ensure_user"):
        assert early_guard < min(calls[name]), (fn_name, name)
        assert after_link_branch < link_guard < min(calls[name]), (fn_name, name)


@pytest.mark.parametrize("fn_name", ["google_callback", "github_callback"])
def test_no_session_is_opened_without_the_starting_browser(fn_name) -> None:
    fn = getattr(auth_routes, fn_name)
    calls = _calls(fn)
    (guard,) = _refusal_guards(fn)["browser_hash is None"]
    assert max(calls["_maybe_refresh_redirect"]) < guard
    for name in (
        "_take_add_account_intent",
        "delete_user_sessions",
        "create_session",
        "add_account",
        "_set_session_cookie",
    ):
        assert guard < min(calls[name]), (fn_name, name)


# --- the Google redirect URI ------------------------------------------------------


@pytest.mark.asyncio
async def test_google_sign_in_always_uses_the_configured_redirect_uri(
    redis,  # noqa: F811
    monkeypatch,
) -> None:
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_REDIRECT_URI", "http://localhost:8080/cb")
    builder = SimpleNamespace(WEB_SCOPES=OAuth2Manager.WEB_SCOPES, provider="google")
    manager = MagicMock()
    manager.get_authorization_url_web.side_effect = lambda *args, **kwargs: (
        OAuth2Manager.get_authorization_url_web(builder, *args, **kwargs)
    )
    monkeypatch.setattr(auth_routes, "_oauth2_manager", manager)

    response = await auth_routes.google_login(
        FakeRequest(),
        response=Response(),
        redirect_uri="https://example.com/cb&state=chosen#",
        return_to=RETURN_TO,
    )

    query = parse_qs(urlparse(response.headers["location"]).query)
    (state,) = _states(redis)
    assert query["state"] == [state]
    assert query["redirect_uri"] == ["http://localhost:8080/cb"]
