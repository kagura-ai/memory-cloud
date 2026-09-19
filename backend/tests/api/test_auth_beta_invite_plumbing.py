"""Carrying a closed-beta invite across the OAuth round trip (Issue #1581).

``GET /auth/{google,github}/login?invite=<token>`` stores
``oauth2_beta_invite:{state}`` = ``sha256_hex(token)`` beside the CSRF state; the
callback reads-and-deletes it and hands the hash to the signup gate. Pinned here:

- the **plaintext never rests in Redis** — only the hash, state-bound, 300 s;
- a malformed or over-long value is ignored (the login still proceeds);
- ``ENABLE_BETA_INVITES=false`` makes ``invite=`` completely inert;
- the key is single-use (read-and-delete), and a tampered value is dropped;
- BOTH real callbacks deliver the hash to ``check_signup_access`` — driven
  through the actual handlers up to the gate, not pinned by source text.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.responses import RedirectResponse

from api.routes import auth as auth_routes
from config.settings import get_settings
from utils.hashing import sha256_hex

TOKEN = "Zk3v_9Qw-" + "a" * 34  # 43 chars, token_urlsafe alphabet
KEY = "oauth2_beta_invite:{state}"


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    def setex(self, key: str, ttl: int, value: str) -> None:
        self.store[key] = value
        self.ttls[key] = ttl

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def delete(self, *keys: str) -> int:
        return sum(1 for k in keys if self.store.pop(k, None) is not None)


class FakeRequest:
    def __init__(self) -> None:
        self.cookies: dict[str, str] = {}
        self.headers = {"user-agent": "pytest"}
        self.client = MagicMock(host="203.0.113.7")


@pytest.fixture
def redis(monkeypatch) -> FakeRedis:
    manager = MagicMock()
    manager._redis = FakeRedis()
    monkeypatch.setattr(auth_routes, "_session_manager", manager)
    return manager._redis


@pytest.fixture
def enabled(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "enable_beta_invites", True)


class TestRemember:
    def test_stores_only_the_hash_bound_to_the_state(self, redis, enabled) -> None:
        auth_routes._remember_beta_invite("st1", TOKEN)

        assert redis.store == {KEY.format(state="st1"): sha256_hex(TOKEN)}
        # Same lifetime as oauth2_state:{state}.
        assert redis.ttls[KEY.format(state="st1")] == 300
        assert all(TOKEN not in k and TOKEN not in v for k, v in redis.store.items())

    @pytest.mark.parametrize(
        "value",
        [
            None,
            "",
            "short",
            "x" * 129,
            "has spaces " + "x" * 30,
            "semi;colon" + "x" * 30,
            "../../etc/passwd" + "x" * 30,
            "ünïcödé" + "x" * 30,
        ],
    )
    def test_malformed_or_overlong_value_is_ignored_not_an_error(
        self, redis, enabled, value
    ) -> None:
        auth_routes._remember_beta_invite("st1", value)
        assert redis.store == {}

    def test_flag_off_makes_the_param_inert(self, redis, monkeypatch) -> None:
        monkeypatch.setattr(get_settings(), "enable_beta_invites", False)
        auth_routes._remember_beta_invite("st1", TOKEN)
        assert redis.store == {}


class TestTake:
    def test_read_and_delete(self, redis, enabled) -> None:
        auth_routes._remember_beta_invite("st1", TOKEN)

        assert auth_routes._take_beta_invite_hash("st1") == sha256_hex(TOKEN)
        assert redis.store == {}
        assert auth_routes._take_beta_invite_hash("st1") is None

    def test_bound_to_its_own_state(self, redis, enabled) -> None:
        auth_routes._remember_beta_invite("st1", TOKEN)
        assert auth_routes._take_beta_invite_hash("other-state") is None
        assert KEY.format(state="st1") in redis.store

    def test_a_value_that_is_not_a_sha256_is_dropped(self, redis, enabled) -> None:
        redis.store[KEY.format(state="st1")] = "not-a-hash"
        assert auth_routes._take_beta_invite_hash("st1") is None
        assert redis.store == {}

    def test_flag_off_still_clears_a_leftover_key_but_returns_nothing(
        self, redis, monkeypatch
    ) -> None:
        """Flag flipped off between login and callback: the hash must not reach
        the gate, and the key must not linger."""
        redis.store[KEY.format(state="st1")] = sha256_hex(TOKEN)
        monkeypatch.setattr(get_settings(), "enable_beta_invites", False)

        assert auth_routes._take_beta_invite_hash("st1") is None
        assert redis.store == {}


class TestLoginRoutes:
    @pytest.mark.asyncio
    async def test_google_login_binds_the_invite_to_its_state(
        self, redis, enabled, monkeypatch
    ) -> None:
        manager = MagicMock()
        manager.get_authorization_url_web.return_value = "https://idp.example.test/auth"
        monkeypatch.setattr(auth_routes, "_oauth2_manager", manager)
        monkeypatch.setenv("GOOGLE_REDIRECT_URI", "http://localhost:8080/cb")

        response = await auth_routes.google_login(FakeRequest(), invite=TOKEN)

        assert redis.store[KEY.format(state=response.state)] == sha256_hex(TOKEN)

    @pytest.mark.asyncio
    async def test_github_login_binds_the_invite_to_its_state(
        self, redis, enabled, monkeypatch
    ) -> None:
        monkeypatch.setenv("GITHUB_CLIENT_ID", "client-id")

        response = await auth_routes.github_login(FakeRequest(), invite=TOKEN)

        assert redis.store[KEY.format(state=response.state)] == sha256_hex(TOKEN)

    @pytest.mark.asyncio
    async def test_login_without_invite_stores_no_invite_key(
        self, redis, enabled, monkeypatch
    ) -> None:
        monkeypatch.setenv("GITHUB_CLIENT_ID", "client-id")

        await auth_routes.github_login(FakeRequest())

        assert not any(k.startswith("oauth2_beta_invite:") for k in redis.store)

    @pytest.mark.asyncio
    async def test_malformed_invite_does_not_break_the_login(
        self, redis, enabled, monkeypatch
    ) -> None:
        monkeypatch.setenv("GITHUB_CLIENT_ID", "client-id")

        response = await auth_routes.github_login(FakeRequest(), invite="<script>")

        assert response.authorization_url
        assert not any(k.startswith("oauth2_beta_invite:") for k in redis.store)


class TestCallbacksDeliverTheHashToTheGate:
    """Drive the real handlers up to the gate. The gate is stubbed to block, so
    the handler returns right there — no user is created, no session minted."""

    @pytest.fixture
    def gate(self, monkeypatch) -> AsyncMock:
        blocked = RedirectResponse("http://localhost:3000/signup-blocked", status_code=303)
        gate = AsyncMock(return_value=blocked)
        monkeypatch.setattr(auth_routes, "check_signup_access", gate)
        monkeypatch.setattr(auth_routes, "_maybe_link_redirect", AsyncMock(return_value=None))
        return gate

    def _pending(self, redis: FakeRedis, state: str, *, with_invite: bool) -> None:
        redis.store[f"oauth2_state:{state}"] = "pending"
        if with_invite:
            redis.store[KEY.format(state=state)] = sha256_hex(TOKEN)

    @pytest.mark.asyncio
    async def test_google_callback(self, redis, enabled, gate, monkeypatch) -> None:
        manager = MagicMock()
        manager.get_user_info_web.return_value = {"sub": "108", "email": "n@example.test"}
        monkeypatch.setattr(auth_routes, "_oauth2_manager", manager)
        monkeypatch.setenv("GOOGLE_REDIRECT_URI", "http://localhost:8080/cb")
        self._pending(redis, "st1", with_invite=True)

        await auth_routes.google_callback(
            FakeRequest(), code="c", state="st1", error=None, error_description=None
        )

        assert gate.await_args.kwargs["beta_invite_token_hash"] == sha256_hex(TOKEN)
        assert gate.await_args.kwargs["provider"] == "google"
        assert KEY.format(state="st1") not in redis.store  # consumed

    @pytest.mark.asyncio
    async def test_github_callback(self, redis, enabled, gate, monkeypatch) -> None:
        monkeypatch.setattr(auth_routes, "_oauth2_manager", MagicMock())
        monkeypatch.setattr(auth_routes, "_github_exchange_code", AsyncMock(return_value="at"))
        monkeypatch.setattr(
            auth_routes,
            "_github_get_user_info",
            AsyncMock(return_value={"sub": "583231", "email": "n@example.test", "login": "octo"}),
        )
        self._pending(redis, "st2", with_invite=True)

        await auth_routes.github_callback(
            FakeRequest(), code="c", state="st2", error=None, error_description=None
        )

        assert gate.await_args.kwargs["beta_invite_token_hash"] == sha256_hex(TOKEN)
        assert gate.await_args.kwargs["provider"] == "github"
        assert KEY.format(state="st2") not in redis.store

    @pytest.mark.asyncio
    async def test_callback_without_an_invite_passes_none(
        self, redis, enabled, gate, monkeypatch
    ) -> None:
        monkeypatch.setattr(auth_routes, "_oauth2_manager", MagicMock())
        monkeypatch.setattr(auth_routes, "_github_exchange_code", AsyncMock(return_value="at"))
        monkeypatch.setattr(
            auth_routes,
            "_github_get_user_info",
            AsyncMock(return_value={"sub": "583231", "email": "n@example.test", "login": "octo"}),
        )
        self._pending(redis, "st3", with_invite=False)

        await auth_routes.github_callback(
            FakeRequest(), code="c", state="st3", error=None, error_description=None
        )

        assert gate.await_args.kwargs["beta_invite_token_hash"] is None

    @pytest.mark.asyncio
    async def test_invalid_csrf_state_never_reads_the_invite(
        self, redis, enabled, gate, monkeypatch
    ) -> None:
        """The invite key is only touched AFTER the CSRF state check passes."""
        monkeypatch.setattr(auth_routes, "_oauth2_manager", MagicMock())
        redis.store[KEY.format(state="forged")] = sha256_hex(TOKEN)  # no oauth2_state:forged

        await auth_routes.github_callback(
            FakeRequest(), code="c", state="forged", error=None, error_description=None
        )

        gate.assert_not_awaited()
        assert KEY.format(state="forged") in redis.store
