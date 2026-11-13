"""The browser nonce bound at ``/auth/{google,github}/login`` (Issue #2786).

Pinned here:

- both ``/login`` routes, in JSON and in redirect mode, give the browser an
  HttpOnly ``SameSite=Lax`` nonce cookie on ``Path=/`` (``__Host-`` prefixed
  when cookies are Secure) and store its digest under ``oauth2_flow:{state}``
  plus a server-side marker; the nonce itself is never stored;
- a presented nonce is reused only while its marker exists; an unknown or
  malformed value is replaced;
- the callbacks' ``_take_oauth_flow`` returns the digest only for the
  browser that presents the bound nonce, and always consumes the key.
"""

from __future__ import annotations

import re
from unittest.mock import MagicMock

import pytest
from fastapi import Response

import auth.oauth_browser as oauth_browser
from api.routes import auth as auth_routes
from tests.api.test_auth_terms_acceptance import FakeRequest, redis  # noqa: F401
from utils.hashing import sha256_hex

PLAIN = "kagura_oauth_browser"
HOST = "__Host-kagura_oauth_browser"


@pytest.fixture
def google_manager(monkeypatch) -> MagicMock:
    manager = MagicMock()
    manager.get_authorization_url_web.return_value = "https://idp.example.test/auth"
    monkeypatch.setattr(auth_routes, "_oauth2_manager", manager)
    monkeypatch.setenv("GOOGLE_REDIRECT_URI", "http://localhost:8080/cb")
    return manager


@pytest.fixture
def github_client(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_CLIENT_ID", "client-id")


@pytest.fixture
def secure_cookies(monkeypatch) -> None:
    attrs = {"path": "/", "httponly": True, "secure": True, "samesite": "lax"}
    monkeypatch.setattr(oauth_browser, "browser_cookie_attrs", lambda: dict(attrs))
    monkeypatch.setattr(auth_routes, "browser_cookie_attrs", lambda: dict(attrs))


def _set_cookies(response) -> list[str]:
    return [v.decode() for k, v in response.raw_headers if k.lower() == b"set-cookie"]


def _nonce_cookie(response, name: str = PLAIN) -> str:
    cookies = [c for c in _set_cookies(response) if c.startswith(f"{name}=")]
    assert len(cookies) == 1, _set_cookies(response)
    return cookies[0]


def _nonce(cookie: str) -> str:
    match = re.match(r"[^=]+=([^;]+);", cookie)
    assert match
    return match.group(1)


def _state_of(redis) -> str:  # noqa: F811
    states = [k.split(":", 1)[1] for k in redis.store if k.startswith("oauth2_state:")]
    assert len(states) == 1
    return states[0]


async def _login(provider: str, request=None, *, return_to: str | None = None):
    request = request or FakeRequest()
    response = Response()
    login = auth_routes.google_login if provider == "google" else auth_routes.github_login
    result = await login(request, response=response, return_to=return_to)
    # JSON mode answers the model; the cookie rides on the injected response.
    return (result if return_to else response), result


class TestLoginSetsTheBinding:
    @pytest.mark.parametrize("provider", ["google", "github"])
    @pytest.mark.parametrize("return_to", [None, "http://localhost:3000/workspace/dashboard"])
    @pytest.mark.asyncio
    async def test_both_modes_set_the_cookie_and_bind_the_state(
        self,
        redis,  # noqa: F811
        google_manager,
        github_client,
        provider,
        return_to,
    ) -> None:
        carrier, _ = await _login(provider, return_to=return_to)

        cookie = _nonce_cookie(carrier)
        nonce = _nonce(cookie)
        assert oauth_browser.well_formed(nonce)
        lowered = cookie.lower()
        assert "httponly" in lowered
        assert "samesite=lax" in lowered
        assert "path=/;" in lowered or lowered.endswith("path=/")
        assert "max-age=1200" in lowered
        assert "domain=" not in lowered
        state = _state_of(redis)
        digest = sha256_hex(nonce)
        assert redis.store[f"oauth2_flow:{state}"] == digest
        assert redis.ttls[f"oauth2_flow:{state}"] == 300
        assert redis.store[f"oauth_browser:{digest}"] == "1"
        assert redis.ttls[f"oauth_browser:{digest}"] == 1200
        # Only digests are stored.
        assert nonce not in repr(redis.store)

    @pytest.mark.parametrize("provider", ["google", "github"])
    @pytest.mark.asyncio
    async def test_production_cookie_has_the_host_prefix(
        self,
        redis,  # noqa: F811
        google_manager,
        github_client,
        secure_cookies,
        provider,
    ) -> None:
        carrier, _ = await _login(provider, return_to="http://localhost:3000/")

        cookie = _nonce_cookie(carrier, HOST)
        lowered = cookie.lower()
        assert "secure" in lowered
        assert "path=/" in lowered
        assert "domain=" not in lowered
        assert not any(c.startswith(f"{PLAIN}=") for c in _set_cookies(carrier))

    @pytest.mark.asyncio
    async def test_a_server_issued_nonce_is_reused_and_refreshed(
        self,
        redis,  # noqa: F811
        github_client,
    ) -> None:
        first, _ = await _login("github")
        nonce = _nonce(_nonce_cookie(first))
        redis.store.clear()
        redis.store[f"oauth_browser:{sha256_hex(nonce)}"] = "1"
        request = FakeRequest()
        request.cookies[PLAIN] = nonce

        second, _ = await _login("github", request)

        assert _nonce(_nonce_cookie(second)) == nonce
        assert redis.ttls[f"oauth_browser:{sha256_hex(nonce)}"] == 1200

    @pytest.mark.parametrize("presented", ["A" * 43, "short", "x" * 43 + ";", ""])
    @pytest.mark.asyncio
    async def test_an_unknown_or_malformed_value_is_replaced(
        self,
        redis,  # noqa: F811
        github_client,
        presented,
    ) -> None:
        request = FakeRequest()
        request.cookies[PLAIN] = presented

        carrier, _ = await _login("github", request)

        nonce = _nonce(_nonce_cookie(carrier))
        assert nonce != presented
        assert oauth_browser.well_formed(nonce)
        assert redis.store[f"oauth2_flow:{_state_of(redis)}"] == sha256_hex(nonce)

    @pytest.mark.asyncio
    async def test_without_a_session_manager_nothing_is_bound(self, monkeypatch) -> None:
        monkeypatch.setattr(auth_routes, "_session_manager", None)
        assert auth_routes._bind_oauth_flow(FakeRequest(), "st1") is None
        response = Response()
        auth_routes._set_oauth_browser_cookie(response, None)
        assert _set_cookies(response) == []


class TestTakeBinding:
    def _bound(self, redis, nonce: str = "N" * 43) -> str:  # noqa: F811
        redis.store["oauth2_flow:st1"] = sha256_hex(nonce)
        return nonce

    def test_the_bound_browser_gets_the_digest(self, redis) -> None:  # noqa: F811
        nonce = self._bound(redis)
        request = FakeRequest()
        request.cookies[PLAIN] = nonce

        assert auth_routes._take_oauth_flow("st1", request).digest == sha256_hex(nonce)
        assert "oauth2_flow:st1" not in redis.store

    @pytest.mark.parametrize("cookie", [None, "M" * 43, "bad"])
    def test_another_browser_gets_nothing_and_the_key_is_consumed(
        self,
        redis,  # noqa: F811
        cookie,
    ) -> None:
        self._bound(redis)
        request = FakeRequest()
        if cookie is not None:
            request.cookies[PLAIN] = cookie

        assert auth_routes._take_oauth_flow("st1", request).digest is None
        assert "oauth2_flow:st1" not in redis.store

    def test_a_flow_without_a_binding_gets_nothing(self, redis) -> None:  # noqa: F811
        request = FakeRequest()
        request.cookies[PLAIN] = "N" * 43
        assert auth_routes._take_oauth_flow("st1", request).digest is None

    def test_the_cookie_name_follows_secure(self, redis, secure_cookies) -> None:  # noqa: F811
        nonce = self._bound(redis)
        plain = FakeRequest()
        plain.cookies[PLAIN] = nonce
        assert auth_routes._take_oauth_flow("st1", plain).digest is None

        self._bound(redis)
        prefixed = FakeRequest()
        prefixed.cookies[HOST] = nonce
        assert auth_routes._take_oauth_flow("st1", prefixed).digest == sha256_hex(nonce)

    def test_only_the_login_routes_write_the_binding(self) -> None:
        """No callback binds a flow: the binding belongs to the browser that started it."""
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(auth_routes))
        callers = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "_bind_oauth_flow"
                for call in ast.walk(node)
            )
        }
        assert callers == {"github_login", "google_login"}
