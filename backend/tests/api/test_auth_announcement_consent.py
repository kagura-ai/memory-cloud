"""The sign-up opt-in for announcement emails rides the OAuth round trip (Issue #2761).

``GET /auth/{google,github}/login?announcement_emails=<true|false>
&announcement_consent_version=<v>`` puts the answer inside the existing
``oauth2_signup_prefs:{state}`` carrier (no new Redis key); the callback reads
it after the CSRF state check, revalidates it and hands it to ``ensure_user``,
which writes the row only when it creates the account. Pinned here:

- stored only while ``ANNOUNCEMENT_EMAILS_ENABLED`` is on, with a well-formed
  value and the current wording version; otherwise the carrier JSON is exactly
  what it was before #2761;
- an answer from a cross-site navigation (``Sec-Fetch-Site: cross-site`` or
  ``none``) is dropped: a third party cannot mint a sign-up opt-in by
  publishing a link;
- both providers; the callback passes ``announcement_consent`` to
  ``ensure_user``; a tampered or stale value reaches it as ``None``; a terms
  refusal or a link round trip consumes it without creating anything.
"""

# ruff: noqa: F811 — the callback fixtures are shared with test_auth_terms_acceptance
from __future__ import annotations

import json

import pytest

from api.routes import auth as auth_routes
from config.settings import get_settings
from services.announcement_consent_service import (
    ANNOUNCEMENT_CONSENT_WORDING_VERSION as V,
)
from services.announcement_consent_service import SignupAnnouncementConsent
from tests.api.test_auth_signup_prefs import (  # noqa: F401
    KEY,
    _only_state,
    ensure_user,
    github_client,
    google_manager,
)
from tests.api.test_auth_terms_acceptance import (  # noqa: F401
    FakeRequest,
    _github_callback,
    _google_callback,
    _pending,
    _set_identity_exists,
    github_idp,
    google_idp,
    redis,
    terms_off,
    terms_on,
)


@pytest.fixture
def enabled(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "announcement_emails_enabled", True)


@pytest.fixture
def disabled(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "announcement_emails_enabled", False)


def _request(site: str | None = "same-origin") -> FakeRequest:
    request = FakeRequest()
    if site is not None:
        request.headers["sec-fetch-site"] = site
    return request


def _stored(redis, state: str) -> dict[str, object]:
    return json.loads(redis.store[KEY.format(state=state)])


def _no_carrier(redis) -> bool:
    return not any(k.startswith("oauth2_signup_prefs:") for k in redis.store)


YES = SignupAnnouncementConsent(value=True, wording_version=V)
NO = SignupAnnouncementConsent(value=False, wording_version=V)


class TestLogin:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("raw,value", [("true", True), ("false", False)])
    async def test_google_stores_the_answer_beside_the_prefs(
        self, redis, google_manager, enabled, raw, value
    ) -> None:
        response = await auth_routes.google_login(
            _request(),
            locale="ja",
            timezone="Asia/Tokyo",
            announcement_emails=raw,
            announcement_consent_version=V,
        )

        assert _stored(redis, response.state) == {
            "locale": "ja",
            "timezone": "Asia/Tokyo",
            "announcement": {"value": value, "version": V},
        }

    @pytest.mark.asyncio
    async def test_github_stores_the_answer_without_prefs(
        self, redis, github_client, enabled
    ) -> None:
        response = await auth_routes.github_login(
            _request("same-site"),
            announcement_emails="true",
            announcement_consent_version=V,
        )

        assert _stored(redis, response.state) == {
            "locale": None,
            "timezone": None,
            "announcement": {"value": True, "version": V},
        }

    @pytest.mark.asyncio
    async def test_no_fetch_metadata_header_is_accepted(
        self, redis, github_client, enabled
    ) -> None:
        response = await auth_routes.github_login(
            _request(None), announcement_emails="true", announcement_consent_version=V
        )

        assert _stored(redis, response.state)["announcement"] == {"value": True, "version": V}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("site", ["cross-site", "none"])
    @pytest.mark.parametrize("raw", ["true", "false"])
    async def test_cross_site_navigation_is_dropped(
        self, redis, google_manager, github_client, enabled, site, raw
    ) -> None:
        g = await auth_routes.google_login(
            _request(site), locale="ja", announcement_emails=raw, announcement_consent_version=V
        )
        await auth_routes.github_login(
            _request(site), announcement_emails=raw, announcement_consent_version=V
        )

        assert _stored(redis, g.state) == {"locale": "ja", "timezone": None}
        assert len([k for k in redis.store if k.startswith("oauth2_signup_prefs:")]) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "raw,version",
        [
            ("true", None),
            ("true", "2020-01"),
            ("yes", V),
            ("1", V),
            ("True", V),
            (None, V),
            ("true", "../x"),
        ],
    )
    async def test_malformed_or_stale_stores_nothing(
        self, redis, github_client, enabled, raw, version
    ) -> None:
        await auth_routes.github_login(
            _request(), announcement_emails=raw, announcement_consent_version=version
        )

        assert _no_carrier(redis)

    @pytest.mark.asyncio
    async def test_disabled_stores_nothing_and_keeps_the_old_shape(
        self, redis, google_manager, disabled
    ) -> None:
        response = await auth_routes.google_login(
            _request(),
            locale="ja",
            timezone="Asia/Tokyo",
            announcement_emails="true",
            announcement_consent_version=V,
        )

        assert _stored(redis, response.state) == {"locale": "ja", "timezone": "Asia/Tokyo"}


class TestCarrier:
    def test_take_returns_the_answer_once(self, redis, enabled) -> None:
        auth_routes._remember_signup_prefs("st1", "ja", None, announcement=YES)

        bundle = auth_routes._take_signup_bundle("st1")

        assert bundle == auth_routes.SignupPrefsBundle(locale="ja", timezone=None, announcement=YES)
        assert auth_routes._take_signup_bundle("st1") == auth_routes.SignupPrefsBundle(
            None, None, None
        )

    def test_old_tuple_reader_still_works(self, redis, enabled) -> None:
        auth_routes._remember_signup_prefs("st1", "ja", "Asia/Tokyo", announcement=NO)

        assert auth_routes._take_signup_prefs("st1") == ("ja", "Asia/Tokyo")
        assert KEY.format(state="st1") not in redis.store

    def test_turned_off_meanwhile_reads_none(self, redis, enabled, monkeypatch) -> None:
        auth_routes._remember_signup_prefs("st1", None, None, announcement=YES)
        monkeypatch.setattr(get_settings(), "announcement_emails_enabled", False)

        assert auth_routes._take_signup_bundle("st1").announcement is None

    @pytest.mark.parametrize(
        "announcement",
        [
            {"value": "true", "version": V},
            {"value": True, "version": "2020-01"},
            {"value": True},
            "yes",
            [True, V],
        ],
    )
    def test_tampered_value_reads_none(self, redis, enabled, announcement) -> None:
        redis.store[KEY.format(state="st1")] = json.dumps(
            {"locale": "ja", "timezone": None, "announcement": announcement}
        )

        bundle = auth_routes._take_signup_bundle("st1")

        assert bundle.locale == "ja"
        assert bundle.announcement is None


class TestCallback:
    @pytest.mark.asyncio
    async def test_google_passes_the_answer(
        self, redis, terms_off, google_idp, ensure_user, enabled
    ) -> None:
        _pending(redis, "st1", terms=None)
        auth_routes._remember_signup_prefs("st1", "ja", None, announcement=YES)

        await _google_callback("st1")

        kwargs = ensure_user.await_args.kwargs
        assert kwargs["announcement_consent"] == YES
        assert kwargs["locale"] == "ja"
        assert KEY.format(state="st1") not in redis.store

    @pytest.mark.asyncio
    async def test_github_passes_the_answer(
        self, redis, terms_off, github_idp, ensure_user, enabled
    ) -> None:
        _pending(redis, "st1", terms=None)
        auth_routes._remember_signup_prefs("st1", None, None, announcement=NO)

        await _github_callback("st1")

        assert ensure_user.await_args.kwargs["announcement_consent"] == NO

    @pytest.mark.asyncio
    async def test_without_an_answer_ensure_user_gets_none(
        self, redis, terms_off, github_idp, ensure_user, enabled
    ) -> None:
        _pending(redis, "st1", terms=None)

        await _github_callback("st1")

        assert ensure_user.await_args.kwargs["announcement_consent"] is None

    @pytest.mark.asyncio
    async def test_a_terms_refusal_consumes_it_and_creates_nothing(
        self, redis, terms_on, github_idp, ensure_user, enabled, monkeypatch
    ) -> None:
        _set_identity_exists(monkeypatch, False)
        _pending(redis, "st1", terms=None)
        auth_routes._remember_signup_prefs("st1", None, None, announcement=YES)

        response = await _github_callback("st1")

        assert "error=terms_required" in response.headers["location"]
        ensure_user.assert_not_awaited()
        assert redis.store == {}

    @pytest.mark.asyncio
    async def test_a_link_round_trip_consumes_it_and_creates_nothing(
        self, redis, terms_off, google_idp, ensure_user, enabled
    ) -> None:
        _pending(redis, "st1", terms=None)
        redis.store["oauth2_state_intent:st1"] = "link"
        redis.store["oauth2_state_user:st1"] = "owner"
        auth_routes._remember_signup_prefs("st1", None, None, announcement=YES)

        response = await _google_callback("st1")

        assert "/profile?error=link_failed" in response.headers["location"]
        ensure_user.assert_not_awaited()
        assert KEY.format(state="st1") not in redis.store
