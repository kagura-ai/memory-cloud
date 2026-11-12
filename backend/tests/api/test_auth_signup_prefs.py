"""The sign-in page's language and time zone reach a new account (Issue #2700).

``GET /auth/{google,github}/login?locale=<en|ja>&timezone=<IANA name>`` stores
``oauth2_signup_prefs:{state}`` beside the CSRF state, the way
``accepted_terms`` travels (#1665); the callback reads and deletes it after the
state check and hands the values to ``ensure_user``, which applies them to a
new row only. Pinned here:

- the value is bound to one state with the state's lifetime, consumed once,
  and validated both when stored and when read;
- both providers store it, in JSON and redirect mode, alongside add-account
  and invite flows; without the params nothing is stored;
- the callback passes it to ``ensure_user``; an invalid CSRF state never reads
  it; a link round trip and a terms refusal consume it without creating
  anything.
"""

# ruff: noqa: F811 — the callback fixtures are shared with test_auth_terms_acceptance
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import Response
from fastapi.responses import RedirectResponse

from api.routes import auth as auth_routes
from auth.session import SESSION_COOKIE_NAME
from config.settings import get_settings
from tests.api.test_auth_terms_acceptance import (  # noqa: F401
    INVITE,
    VERSION,
    FakeRequest,
    _github_callback,
    _google_callback,
    _pending,
    _set_identity_exists,
    _states,
    github_idp,
    google_idp,
    redis,
    terms_off,
    terms_on,
)

KEY = "oauth2_signup_prefs:{state}"
# One character over users.timezone (String(50)).
TZ_51 = "America/" + "A" * 43
assert len(TZ_51) == 51


def _stored(redis, state: str) -> dict[str, object]:
    return json.loads(redis.store[KEY.format(state=state)])


class TestStateBinding:
    def test_bound_to_its_state_with_the_state_lifetime(self, redis) -> None:
        auth_routes._remember_signup_prefs("st1", "ja", "Asia/Tokyo")

        assert list(redis.store) == [KEY.format(state="st1")]
        assert _stored(redis, "st1") == {"locale": "ja", "timezone": "Asia/Tokyo"}
        assert redis.ttls[KEY.format(state="st1")] == 300

    def test_consumed_once(self, redis) -> None:
        auth_routes._remember_signup_prefs("st1", "ja", "Asia/Tokyo")

        assert auth_routes._take_signup_prefs("st1") == ("ja", "Asia/Tokyo")
        assert redis.store == {}
        assert auth_routes._take_signup_prefs("st1") == (None, None)

    def test_not_readable_from_another_state(self, redis) -> None:
        auth_routes._remember_signup_prefs("st1", "ja", "Asia/Tokyo")

        assert auth_routes._take_signup_prefs("other-state") == (None, None)
        assert KEY.format(state="st1") in redis.store

    def test_one_value_alone_is_kept(self, redis) -> None:
        auth_routes._remember_signup_prefs("st1", "ja", None)
        auth_routes._remember_signup_prefs("st2", None, "Asia/Tokyo")

        assert auth_routes._take_signup_prefs("st1") == ("ja", None)
        assert auth_routes._take_signup_prefs("st2") == (None, "Asia/Tokyo")

    @pytest.mark.parametrize(
        "locale", [None, "", "EN", "xx", "ja; x", "../etc/passwd", "ja\r\nSet-Cookie: x"]
    )
    def test_malformed_locale_is_dropped(self, redis, locale) -> None:
        auth_routes._remember_signup_prefs("st1", locale, "Asia/Tokyo")

        assert _stored(redis, "st1") == {"locale": None, "timezone": "Asia/Tokyo"}

    @pytest.mark.parametrize(
        "timezone",
        [None, "", "../etc/passwd", "/etc/passwd", "Nope/Zone", TZ_51],
    )
    def test_malformed_timezone_is_dropped(self, redis, timezone) -> None:
        auth_routes._remember_signup_prefs("st1", "ja", timezone)

        assert _stored(redis, "st1") == {"locale": "ja", "timezone": None}

    @pytest.mark.parametrize(("locale", "timezone"), [(None, None), ("xx", "Nope/Zone"), ("", "")])
    def test_nothing_valid_stores_nothing(self, redis, locale, timezone) -> None:
        auth_routes._remember_signup_prefs("st1", locale, timezone)

        assert redis.store == {}

    @pytest.mark.parametrize(
        "raw",
        [
            json.dumps({"locale": "xx", "timezone": "../etc/passwd"}),
            json.dumps({"locale": 1, "timezone": ["Asia/Tokyo"]}),
            json.dumps(["ja", "Asia/Tokyo"]),
            "not json",
            "",
        ],
    )
    def test_tampered_value_is_dropped(self, redis, raw) -> None:
        redis.store[KEY.format(state="st1")] = raw

        assert auth_routes._take_signup_prefs("st1") == (None, None)
        assert redis.store == {}

    def test_a_tampered_half_does_not_drop_the_other(self, redis) -> None:
        redis.store[KEY.format(state="st1")] = json.dumps({"locale": "ja", "timezone": "Nope/Zone"})

        assert auth_routes._take_signup_prefs("st1") == ("ja", None)

    def test_without_a_session_manager(self, monkeypatch) -> None:
        monkeypatch.setattr(auth_routes, "_session_manager", None)

        auth_routes._remember_signup_prefs("st1", "ja", "Asia/Tokyo")
        assert auth_routes._take_signup_prefs("st1") == (None, None)


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


def _only_state(redis) -> str:
    states = _states(redis)
    assert len(states) == 1
    return states[0]


class TestLoginRoutes:
    @pytest.mark.asyncio
    async def test_google_json_mode_stores_the_prefs(self, redis, google_manager) -> None:
        response = await auth_routes.google_login(
            FakeRequest(), response=Response(), locale="ja", timezone="Asia/Tokyo"
        )

        assert _stored(redis, response.state) == {"locale": "ja", "timezone": "Asia/Tokyo"}

    @pytest.mark.asyncio
    async def test_google_redirect_mode_stores_the_prefs(self, redis, google_manager) -> None:
        response = await auth_routes.google_login(
            FakeRequest(),
            response=Response(),
            return_to="http://localhost:3000/device?c=1",
            locale="ja",
            timezone="Asia/Tokyo",
        )

        assert response.status_code == 303
        assert _stored(redis, _only_state(redis)) == {"locale": "ja", "timezone": "Asia/Tokyo"}

    @pytest.mark.asyncio
    async def test_github_json_mode_stores_the_prefs(self, redis, github_client) -> None:
        response = await auth_routes.github_login(
            FakeRequest(), response=Response(), locale="ja", timezone="Asia/Tokyo"
        )

        assert _stored(redis, response.state) == {"locale": "ja", "timezone": "Asia/Tokyo"}

    @pytest.mark.asyncio
    async def test_github_redirect_mode_stores_the_prefs(self, redis, github_client) -> None:
        response = await auth_routes.github_login(
            FakeRequest(),
            response=Response(),
            return_to="http://localhost:3000/",
            locale="ja",
            timezone="Asia/Tokyo",
        )

        assert response.status_code == 303
        assert _stored(redis, _only_state(redis)) == {"locale": "ja", "timezone": "Asia/Tokyo"}

    @pytest.mark.asyncio
    async def test_google_add_account_flow_stores_the_prefs(self, redis, google_manager) -> None:
        request = FakeRequest()
        request.cookies[SESSION_COOKIE_NAME] = "sess-1"

        response = await auth_routes.google_login(
            request, response=Response(), add_account=True, locale="en", timezone="UTC"
        )

        assert redis.store[f"oauth2_add_to_session:{response.state}"] == "sess-1"
        assert _stored(redis, response.state) == {"locale": "en", "timezone": "UTC"}

    @pytest.mark.asyncio
    async def test_github_invite_flow_stores_the_prefs(
        self, redis, terms_on, github_client, monkeypatch
    ) -> None:
        monkeypatch.setattr(get_settings(), "enable_beta_invites", True)

        response = await auth_routes.github_login(
            FakeRequest(),
            response=Response(),
            return_to="http://localhost:3000/workspace/dashboard",
            invite=INVITE,
            accepted_terms=VERSION,
            locale="ja",
            timezone="Asia/Tokyo",
        )

        state = _only_state(redis)
        assert response.status_code == 303
        assert f"oauth2_beta_invite:{state}" in redis.store
        assert _stored(redis, state) == {"locale": "ja", "timezone": "Asia/Tokyo"}

    @pytest.mark.asyncio
    async def test_login_without_the_params_stores_nothing(
        self, redis, google_manager, github_client
    ) -> None:
        await auth_routes.google_login(FakeRequest(), response=Response())
        await auth_routes.github_login(
            FakeRequest(), response=Response(), return_to="http://localhost:3000/"
        )

        assert not any(k.startswith("oauth2_signup_prefs:") for k in redis.store)

    @pytest.mark.asyncio
    async def test_login_with_only_malformed_params_stores_nothing(
        self, redis, github_client
    ) -> None:
        await auth_routes.github_login(
            FakeRequest(), response=Response(), locale="xx", timezone="../etc/passwd"
        )

        assert not any(k.startswith("oauth2_signup_prefs:") for k in redis.store)


@pytest.fixture
def ensure_user(monkeypatch) -> AsyncMock:
    """Drive the callback through ``ensure_user`` and stop right after it."""
    monkeypatch.setattr(auth_routes, "check_signup_access", AsyncMock(return_value=None))
    stub = AsyncMock(return_value=MagicMock(value="user"))
    monkeypatch.setattr(auth_routes, "get_role_manager", lambda: SimpleNamespace(ensure_user=stub))
    monkeypatch.setattr(auth_routes, "_record_terms_acceptance", AsyncMock())
    monkeypatch.setattr(
        auth_routes,
        "_session_owner",
        AsyncMock(return_value=auth_routes.SessionOwner("owner", "n@example.test")),
    )
    monkeypatch.setattr(
        auth_routes,
        "_maybe_refresh_redirect",
        AsyncMock(return_value=RedirectResponse("http://localhost:3000/stop", 303)),
    )
    return stub


class TestCallback:
    @pytest.mark.asyncio
    async def test_google_new_account_gets_the_prefs(
        self, redis, terms_off, google_idp, ensure_user
    ) -> None:
        _pending(redis, "st1", terms=None)
        auth_routes._remember_signup_prefs("st1", "ja", "Asia/Tokyo")

        response = await _google_callback("st1")

        assert response.headers["location"] == "http://localhost:3000/stop"
        kwargs = ensure_user.await_args.kwargs
        assert kwargs["locale"] == "ja"
        assert kwargs["timezone"] == "Asia/Tokyo"
        assert KEY.format(state="st1") not in redis.store

    @pytest.mark.asyncio
    async def test_github_new_account_gets_the_prefs(
        self, redis, terms_off, github_idp, ensure_user
    ) -> None:
        _pending(redis, "st1", terms=None)
        auth_routes._remember_signup_prefs("st1", "ja", "Asia/Tokyo")

        await _github_callback("st1")

        kwargs = ensure_user.await_args.kwargs
        assert kwargs["locale"] == "ja"
        assert kwargs["timezone"] == "Asia/Tokyo"
        assert KEY.format(state="st1") not in redis.store

    @pytest.mark.asyncio
    async def test_without_prefs_ensure_user_gets_none(
        self, redis, terms_off, github_idp, ensure_user
    ) -> None:
        _pending(redis, "st1", terms=None)

        await _github_callback("st1")

        kwargs = ensure_user.await_args.kwargs
        assert kwargs["locale"] is None
        assert kwargs["timezone"] is None

    @pytest.mark.asyncio
    async def test_a_tampered_value_reaches_ensure_user_as_none(
        self, redis, terms_off, google_idp, ensure_user
    ) -> None:
        _pending(redis, "st1", terms=None)
        redis.store[KEY.format(state="st1")] = json.dumps(
            {"locale": "fr", "timezone": "/etc/passwd"}
        )

        await _google_callback("st1")

        kwargs = ensure_user.await_args.kwargs
        assert (kwargs["locale"], kwargs["timezone"]) == (None, None)
        assert KEY.format(state="st1") not in redis.store

    @pytest.mark.asyncio
    async def test_invalid_csrf_state_never_reads_the_prefs(
        self, redis, terms_off, github_idp, ensure_user
    ) -> None:
        auth_routes._remember_signup_prefs("forged", "ja", "Asia/Tokyo")  # no oauth2_state

        response = await _github_callback("forged")

        assert "error=oauth_expired" in response.headers["location"]
        ensure_user.assert_not_awaited()
        assert KEY.format(state="forged") in redis.store

    @pytest.mark.asyncio
    async def test_a_link_round_trip_consumes_the_prefs_and_creates_nothing(
        self, redis, terms_off, google_idp, ensure_user
    ) -> None:
        # The real link branch: an intent with no pinned session is refused
        # (link_failed) before any account work.
        _pending(redis, "st1", terms=None)
        redis.store["oauth2_state_intent:st1"] = "link"
        redis.store["oauth2_state_user:st1"] = "owner"
        auth_routes._remember_signup_prefs("st1", "ja", "Asia/Tokyo")

        response = await _google_callback("st1")

        assert "/profile?error=link_failed" in response.headers["location"]
        ensure_user.assert_not_awaited()
        assert KEY.format(state="st1") not in redis.store

    @pytest.mark.asyncio
    async def test_a_terms_refusal_leaves_no_prefs_behind(
        self, redis, terms_on, github_idp, ensure_user, monkeypatch
    ) -> None:
        _set_identity_exists(monkeypatch, False)
        _pending(redis, "st1", terms=None)
        auth_routes._remember_signup_prefs("st1", "ja", "Asia/Tokyo")

        response = await _github_callback("st1")

        assert "error=terms_required" in response.headers["location"]
        ensure_user.assert_not_awaited()
        assert redis.store == {}
