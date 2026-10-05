"""What an OAuth sign-in proves for an identity link (#1818).

#1803 asks both accounts of a link to have signed in within ten minutes, but an
OAuth round trip goes through without a password while the browser still has a
session with the provider. The window then proved "this browser can complete a
sign-in", not "the person authenticated".

- A sign-in started with ``link_proof=1`` asks Google for ``auth_time``, and
  the callback records it from the verified ID token as the account's proof.
  A missing or unverifiable ``auth_time`` proves nothing (fail closed).
- Ordinary sign-ins send the same request to Google as before and prove
  nothing; GitHub never reports an authentication time, so it proves nothing.
- ``IDENTITY_LINK_ALLOW_OAUTH_SIGNIN_PROOF`` brings back the #1803 rule.
- A password sign-in proves the account now.
"""

# ruff: noqa: F811 — the callback fixtures are shared with test_linked_provider_session
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest

from api.routes import auth as auth_routes
from auth.oauth2 import OAuth2Manager
from config.settings import get_settings
from tests.api.test_linked_provider_session import (  # noqa: F401
    GITHUB_SUB,
    GOOGLE_SUB,
    OWNER_ID,
    FakeRequest,
    github_idp,
    google_idp,
    linked_owner,
    manager,
    real_manager,
    signed_in_path,
)
from utils.datetime import utcnow

PROOF_KEY = "oauth2_link_proof:{state}"


@pytest.fixture
def strict(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "identity_link_allow_oauth_signin_proof", False)


@pytest.fixture
def opted_out(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "identity_link_allow_oauth_signin_proof", True)


def test_the_default_is_strict() -> None:
    field = type(get_settings()).model_fields["identity_link_allow_oauth_signin_proof"]
    assert field.default is False


# --- The request to Google ----------------------------------------------------


@pytest.fixture
def oauth(monkeypatch) -> OAuth2Manager:
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid.apps.googleusercontent.com")
    return OAuth2Manager.__new__(OAuth2Manager)


def _setup_web(m: OAuth2Manager) -> OAuth2Manager:
    m.provider = "google"
    return m


class TestAuthorizationUrl:
    @pytest.mark.parametrize("select_account", [False, True])
    def test_an_ordinary_sign_in_sends_the_same_request(self, oauth, select_account) -> None:
        """Byte-for-byte what it was before #1818, chooser or not."""
        m = _setup_web(oauth)
        url = m.get_authorization_url_web("http://cb", "st", select_account=select_account)

        prompt = "select_account%20consent" if select_account else "consent"
        assert url.endswith(f"&state=st&access_type=offline&prompt={prompt}")
        assert "claims" not in url
        assert "max_age" not in url

    def test_a_link_proof_asks_for_auth_time_and_keeps_the_prompt(self, oauth) -> None:
        m = _setup_web(oauth)
        url = m.get_authorization_url_web(
            "http://cb", "st", select_account=True, request_auth_time=True
        )

        query = parse_qs(urlparse(url).query)
        assert json.loads(query["claims"][0]) == {"id_token": {"auth_time": {"essential": True}}}
        # Google has no prompt=login and ignores max_age: never sent.
        assert query["prompt"] == ["select_account consent"]
        assert "max_age" not in query


class TestLoginRoute:
    @pytest.fixture
    def idp(self, monkeypatch) -> MagicMock:
        m = MagicMock()
        m.get_authorization_url_web.return_value = "https://idp.example.test/auth"
        monkeypatch.setattr(auth_routes, "_oauth2_manager", m)
        monkeypatch.setenv("GOOGLE_REDIRECT_URI", "http://localhost:8080/cb")
        return m

    @pytest.mark.asyncio
    async def test_link_proof_is_bound_to_the_state(self, manager, idp) -> None:
        response = await auth_routes.google_login(FakeRequest(), link_proof=True)

        assert manager._redis.store[PROOF_KEY.format(state=response.state)] == "1"
        assert idp.get_authorization_url_web.call_args.kwargs["request_auth_time"] is True

    @pytest.mark.asyncio
    async def test_an_ordinary_login_binds_nothing_and_asks_for_nothing(self, manager, idp) -> None:
        response = await auth_routes.google_login(FakeRequest())

        assert PROOF_KEY.format(state=response.state) not in manager._redis.store
        assert idp.get_authorization_url_web.call_args.kwargs["request_auth_time"] is False


def test_the_intent_is_single_use(manager) -> None:
    auth_routes._remember_link_proof_intent("st9")

    assert auth_routes._take_link_proof_intent("st9") is True
    assert auth_routes._take_link_proof_intent("st9") is False


# --- Reading auth_time from the ID token --------------------------------------


class TestVerifiedAuthTime:
    @staticmethod
    def _verify(claims=None, error: Exception | None = None):
        mock = MagicMock(return_value=claims, side_effect=error)
        return patch("google.oauth2.id_token.verify_oauth2_token", mock), mock

    def test_returns_the_verified_auth_time(self, oauth) -> None:
        when = int((utcnow() - timedelta(minutes=2)).replace(tzinfo=UTC).timestamp())
        p, verify = self._verify({"sub": "108", "auth_time": when})
        with p:
            got = oauth.verified_auth_time(SimpleNamespace(id_token="jwt"), "108")

        assert got == datetime.fromtimestamp(when, UTC).replace(tzinfo=None)
        # Verified against our client id: Google's signature, aud, iss, exp.
        assert verify.call_args.args[0] == "jwt"
        assert verify.call_args.kwargs["audience"] == "cid.apps.googleusercontent.com"

    @pytest.mark.parametrize(
        "claims",
        [
            {"sub": "108"},  # Session age claims not enabled for the app
            {"sub": "108", "auth_time": "1700000000"},
            {"sub": "108", "auth_time": True},
            {"sub": "999", "auth_time": 1700000000},  # another identity
        ],
    )
    def test_fails_closed_on_a_missing_or_foreign_claim(self, oauth, claims) -> None:
        p, _ = self._verify(claims)
        with p:
            assert oauth.verified_auth_time(SimpleNamespace(id_token="jwt"), "108") is None

    def test_fails_closed_when_the_token_does_not_verify(self, oauth) -> None:
        p, _ = self._verify(error=ValueError("Token used too late"))
        with p:
            assert oauth.verified_auth_time(SimpleNamespace(id_token="jwt"), "108") is None

    def test_fails_closed_without_an_id_token(self, oauth) -> None:
        p, verify = self._verify({"sub": "108", "auth_time": 1})
        with p:
            assert oauth.verified_auth_time(SimpleNamespace(id_token=None), "108") is None
        verify.assert_not_called()

    def test_a_time_slightly_ahead_of_ours_reads_as_now(self, oauth) -> None:
        ahead = int((utcnow() + timedelta(seconds=5)).replace(tzinfo=UTC).timestamp())
        p, _ = self._verify({"sub": "108", "auth_time": ahead})
        with p:
            got = oauth.verified_auth_time(SimpleNamespace(id_token="jwt"), "108")

        assert got is not None and got <= utcnow()

    def test_a_time_well_ahead_of_ours_proves_nothing(self, oauth) -> None:
        """Clock skew must not stretch into a fresh proof (fail closed)."""
        ahead = int((utcnow() + timedelta(minutes=20)).replace(tzinfo=UTC).timestamp())
        p, _ = self._verify({"sub": "108", "auth_time": ahead})
        with p:
            assert oauth.verified_auth_time(SimpleNamespace(id_token="jwt"), "108") is None


# --- What the callbacks record --------------------------------------------------


async def _callback(provider: str):
    handler = auth_routes.google_callback if provider == "google" else auth_routes.github_callback
    return await handler(FakeRequest(), code="c", state="st1", error=None, error_description=None)


def _proof(manager: MagicMock):
    return manager.create_session.call_args.kwargs["proven_at"]


class TestGoogleCallback:
    @pytest.mark.asyncio
    async def test_a_link_proof_records_the_provider_authentication_time(
        self, manager, signed_in_path, google_idp, strict
    ) -> None:
        auth_time = utcnow() - timedelta(hours=3)
        auth_routes._oauth2_manager.verified_auth_time.return_value = auth_time
        manager._redis.store[PROOF_KEY.format(state="st1")] = "1"

        await _callback("google")

        # The provider's time, not the sign-in's: an hours-old Google session
        # does not become a fresh proof by completing a round trip now.
        assert _proof(manager) == auth_time
        auth_routes._oauth2_manager.verified_auth_time.assert_called_once()
        assert auth_routes._oauth2_manager.verified_auth_time.call_args.args[1] == GOOGLE_SUB
        assert PROOF_KEY.format(state="st1") not in manager._redis.store

    @pytest.mark.asyncio
    async def test_a_missing_auth_time_proves_nothing(
        self, manager, signed_in_path, google_idp, strict
    ) -> None:
        auth_routes._oauth2_manager.verified_auth_time.return_value = None
        manager._redis.store[PROOF_KEY.format(state="st1")] = "1"

        response = await _callback("google")

        assert response.status_code == 303  # the sign-in itself still succeeds
        assert _proof(manager) is None

    @pytest.mark.asyncio
    async def test_an_ordinary_sign_in_proves_nothing(
        self, manager, signed_in_path, google_idp, strict
    ) -> None:
        await _callback("google")

        assert _proof(manager) is None
        auth_routes._oauth2_manager.verified_auth_time.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_opt_out_counts_the_sign_in(
        self, manager, signed_in_path, google_idp, opted_out
    ) -> None:
        before = utcnow()
        await _callback("google")

        assert before <= _proof(manager) <= utcnow()

    @pytest.mark.asyncio
    async def test_the_opt_out_does_not_let_an_old_auth_time_weaken_a_proof(
        self, manager, signed_in_path, google_idp, opted_out
    ) -> None:
        """With the opt-out, "Confirm with Google" is never worse than a sign-in."""
        auth_routes._oauth2_manager.verified_auth_time.return_value = utcnow() - timedelta(hours=3)
        manager._redis.store[PROOF_KEY.format(state="st1")] = "1"
        before = utcnow()

        await _callback("google")

        assert before <= _proof(manager) <= utcnow()

    # --- #1833: telling the page when the proof did not stand ---------------

    @staticmethod
    def _result(response) -> list[str] | None:
        return parse_qs(urlparse(response.headers["location"]).query).get("link_proof")

    @pytest.mark.asyncio
    async def test_a_missing_auth_time_tells_the_page_it_proved_nothing(
        self, manager, signed_in_path, google_idp, strict
    ) -> None:
        auth_routes._oauth2_manager.verified_auth_time.return_value = None
        manager._redis.store[PROOF_KEY.format(state="st1")] = "1"
        manager._redis.store["oauth2_return_to:st1"] = "http://localhost:3000/profile"

        response = await _callback("google")

        location = urlparse(response.headers["location"])
        assert location.path == "/profile"
        assert self._result(response) == ["unproved"]

    @pytest.mark.asyncio
    async def test_a_stale_auth_time_tells_the_page_to_sign_in_to_google_again(
        self, manager, signed_in_path, google_idp, strict
    ) -> None:
        auth_routes._oauth2_manager.verified_auth_time.return_value = utcnow() - timedelta(hours=3)
        manager._redis.store[PROOF_KEY.format(state="st1")] = "1"
        manager._redis.store["oauth2_return_to:st1"] = "http://localhost:3000/profile?tab=x"

        response = await _callback("google")

        query = parse_qs(urlparse(response.headers["location"]).query)
        assert query["link_proof"] == ["stale"]
        assert query["tab"] == ["x"]  # merged, not a second "?"

    @pytest.mark.asyncio
    async def test_a_fresh_auth_time_reports_nothing(
        self, manager, signed_in_path, google_idp, strict
    ) -> None:
        auth_routes._oauth2_manager.verified_auth_time.return_value = utcnow() - timedelta(
            minutes=1
        )
        manager._redis.store[PROOF_KEY.format(state="st1")] = "1"
        manager._redis.store["oauth2_return_to:st1"] = "http://localhost:3000/profile"

        response = await _callback("google")

        assert self._result(response) is None

    @pytest.mark.asyncio
    async def test_a_proof_about_to_expire_counts_as_stale(
        self, manager, signed_in_path, google_idp, strict
    ) -> None:
        """Seconds of headroom are not enough to come back and link."""
        auth_routes._oauth2_manager.verified_auth_time.return_value = utcnow() - timedelta(
            minutes=9, seconds=30
        )
        manager._redis.store[PROOF_KEY.format(state="st1")] = "1"
        manager._redis.store["oauth2_return_to:st1"] = "http://localhost:3000/profile"

        response = await _callback("google")

        assert self._result(response) == ["stale"]

    @pytest.mark.asyncio
    async def test_a_result_already_on_return_to_is_replaced_not_stacked(
        self, manager, signed_in_path, google_idp, strict
    ) -> None:
        auth_routes._oauth2_manager.verified_auth_time.return_value = None
        manager._redis.store[PROOF_KEY.format(state="st1")] = "1"
        manager._redis.store["oauth2_return_to:st1"] = (
            "http://localhost:3000/profile?link_proof=stale"
        )

        response = await _callback("google")

        assert self._result(response) == ["unproved"]

    @pytest.mark.asyncio
    async def test_the_result_never_rides_on_the_dashboard_fallback(
        self, manager, signed_in_path, google_idp, strict
    ) -> None:
        """No return_to, or one that fails validation: nobody reads the result."""
        auth_routes._oauth2_manager.verified_auth_time.return_value = None
        manager._redis.store[PROOF_KEY.format(state="st1")] = "1"

        response = await _callback("google")
        assert self._result(response) is None

        manager._redis.store["oauth2_state:st1"] = "pending"
        manager._redis.store[PROOF_KEY.format(state="st1")] = "1"
        manager._redis.store["oauth2_return_to:st1"] = "https://evil.example/profile"
        response = await _callback("google")
        assert self._result(response) is None
        assert "evil.example" not in response.headers["location"]

    @pytest.mark.asyncio
    async def test_an_ordinary_sign_in_reports_nothing(
        self, manager, signed_in_path, google_idp, strict
    ) -> None:
        manager._redis.store["oauth2_return_to:st1"] = "http://localhost:3000/profile"

        response = await _callback("google")

        assert self._result(response) is None

    @pytest.mark.asyncio
    async def test_the_opt_out_reports_nothing(
        self, manager, signed_in_path, google_idp, opted_out
    ) -> None:
        auth_routes._oauth2_manager.verified_auth_time.return_value = None
        manager._redis.store[PROOF_KEY.format(state="st1")] = "1"
        manager._redis.store["oauth2_return_to:st1"] = "http://localhost:3000/profile"

        response = await _callback("google")

        assert self._result(response) is None

    @pytest.mark.asyncio
    async def test_add_account_carries_the_proof(
        self, manager, signed_in_path, google_idp, strict, monkeypatch
    ) -> None:
        auth_time = utcnow() - timedelta(minutes=1)
        auth_routes._oauth2_manager.verified_auth_time.return_value = auth_time
        manager._redis.store[PROOF_KEY.format(state="st1")] = "1"
        monkeypatch.setattr(
            auth_routes, "_take_add_account_intent", lambda _s, _r: ("add", "sess-A")
        )
        manager.add_account.return_value = True

        await _callback("google")

        assert manager.add_account.call_args.kwargs["proven_at"] == auth_time
        manager.create_session.assert_not_called()


class TestGitHubCallback:
    @pytest.mark.asyncio
    async def test_github_proves_nothing_by_default(
        self, manager, signed_in_path, github_idp, strict
    ) -> None:
        await _callback("github")

        assert _proof(manager) is None

    @pytest.mark.asyncio
    async def test_github_counts_with_the_opt_out(
        self, manager, signed_in_path, github_idp, opted_out
    ) -> None:
        before = utcnow()
        await _callback("github")

        assert before <= _proof(manager) <= utcnow()


class TestARecentlyAttachedProvider:
    """#1875: attaching a sign-in provider needs only a live session, so a
    sign-in through one attached inside the link window proves nothing."""

    RECENT = timedelta(minutes=3)

    @pytest.mark.asyncio
    async def test_a_fresh_google_auth_time_through_it_proves_nothing(
        self, manager, signed_in_path, google_idp, strict
    ) -> None:
        signed_in_path.owning.return_value = linked_owner(attached=self.RECENT)
        auth_routes._oauth2_manager.verified_auth_time.return_value = utcnow() - timedelta(
            minutes=1
        )
        manager._redis.store[PROOF_KEY.format(state="st1")] = "1"
        manager._redis.store["oauth2_return_to:st1"] = "http://localhost:3000/profile"

        response = await _callback("google")

        assert response.status_code == 303  # the sign-in itself still succeeds
        assert _proof(manager) is None
        query = parse_qs(urlparse(response.headers["location"]).query)
        assert query["link_proof"] == ["recent_provider"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("provider", ["google", "github"])
    async def test_the_opt_out_does_not_count_it_either(
        self, manager, signed_in_path, request, opted_out, provider
    ) -> None:
        request.getfixturevalue(f"{provider}_idp")
        signed_in_path.owning.return_value = linked_owner(attached=self.RECENT)

        await _callback(provider)

        assert _proof(manager) is None

    @pytest.mark.asyncio
    async def test_the_opt_out_still_tells_the_page(
        self, manager, signed_in_path, google_idp, opted_out
    ) -> None:
        signed_in_path.owning.return_value = linked_owner(attached=self.RECENT)
        manager._redis.store[PROOF_KEY.format(state="st1")] = "1"
        manager._redis.store["oauth2_return_to:st1"] = "http://localhost:3000/profile"

        response = await _callback("google")

        query = parse_qs(urlparse(response.headers["location"]).query)
        assert query["link_proof"] == ["recent_provider"]

    @pytest.mark.asyncio
    async def test_an_ordinary_sign_in_through_it_reports_nothing(
        self, manager, signed_in_path, google_idp, strict
    ) -> None:
        signed_in_path.owning.return_value = linked_owner(attached=self.RECENT)
        manager._redis.store["oauth2_return_to:st1"] = "http://localhost:3000/profile"

        response = await _callback("google")

        assert "link_proof" not in response.headers["location"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("provider", ["google", "github"])
    async def test_once_the_window_has_passed_it_proves_the_account(
        self, manager, signed_in_path, request, opted_out, provider
    ) -> None:
        request.getfixturevalue(f"{provider}_idp")
        signed_in_path.owning.return_value = linked_owner(attached=timedelta(minutes=11))
        before = utcnow()

        await _callback(provider)

        assert before <= _proof(manager) <= utcnow()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("provider", "sub"), [("google", GOOGLE_SUB), ("github", GITHUB_SUB)])
    async def test_a_first_sign_in_proves_the_new_account_at_once(
        self, manager, signed_in_path, request, opted_out, provider, sub
    ) -> None:
        """A first sign-in creates the account and its provider row together,
        seconds before the proof."""
        request.getfixturevalue(f"{provider}_idp")
        signed_in_path.owning.return_value = auth_routes.SessionOwner(
            sub, "own@example.test", "Own", None, utcnow(), utcnow()
        )
        before = utcnow()

        await _callback(provider)

        assert before <= _proof(manager) <= utcnow()

    @staticmethod
    def _owner(user_id: str, *, linked: timedelta | None, created: timedelta | None):
        now = utcnow()
        return auth_routes.SessionOwner(
            user_id,
            "o@example.test",
            None,
            None,
            None if linked is None else now - linked,
            None if created is None else now - created,
        )

    @pytest.mark.parametrize(
        ("linked", "expected"),
        [
            (timedelta(minutes=9, seconds=59), False),
            (timedelta(minutes=10, seconds=1), True),
            (timedelta(minutes=-5), False),  # a time in the future
            (None, False),  # a linked row with no readable time
        ],
    )
    def test_a_linked_provider_counts_once_the_window_has_passed(self, linked, expected) -> None:
        owner = self._owner(OWNER_ID, linked=linked, created=timedelta(days=365))

        assert auth_routes._identity_can_prove(owner, GOOGLE_SUB) is expected

    @pytest.mark.parametrize(
        ("linked", "created", "expected"),
        [
            # Created together a moment ago: the first sign-in.
            (timedelta(seconds=2), timedelta(seconds=2), True),
            (timedelta(minutes=9), timedelta(minutes=9), True),
            # The same sub attached again to an old account (it unlinked its
            # original identity), or another provider's sub that equals the id.
            (timedelta(minutes=3), timedelta(days=365), False),
            (timedelta(minutes=3), timedelta(minutes=11), False),
            (timedelta(minutes=3), None, False),
            # Old either way.
            (timedelta(days=30), timedelta(days=365), True),
        ],
    )
    def test_the_sub_alone_does_not_exempt_a_recent_row(self, linked, created, expected) -> None:
        owner = self._owner(GOOGLE_SUB, linked=linked, created=created)

        assert auth_routes._identity_can_prove(owner, GOOGLE_SUB) is expected

    def test_an_identity_with_no_link_row_was_never_attached(self) -> None:
        """Found by the ``users`` row keyed by the sub, or not found at all."""
        assert auth_routes._identity_can_prove(
            self._owner(GOOGLE_SUB, linked=None, created=timedelta(days=365)), GOOGLE_SUB
        )
        assert auth_routes._identity_can_prove(
            auth_routes.SessionOwner(GOOGLE_SUB, "idp@example.test"), GOOGLE_SUB
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("attached", "linked"), [(RECENT, False), (timedelta(days=1), True)])
    async def test_the_link_route_refuses_the_account_it_was_attached_to(
        self, real_manager, signed_in_path, google_idp, strict, monkeypatch, attached, linked
    ) -> None:
        """The regression, end to end over a real session: OWNER attaches a
        Google identity from a session that proved nothing, signs in through
        it with a fresh ``auth_time``, and asks to be linked to an account
        that did prove its password. 403 — unless the provider is an old one."""
        from api.routes import me_account
        from utils.exceptions import IdentityLinkSignInRequiredError

        signed_in_path.owning.return_value = linked_owner(attached=attached)
        auth_routes._oauth2_manager.verified_auth_time.return_value = utcnow() - timedelta(
            minutes=1
        )
        real_manager._redis.setex(PROOF_KEY.format(state="st1"), 300, "1")
        victim = {"sub": "local:admin", "user_id": "local:admin", "email": "admin@local"}
        sid = real_manager.create_session(victim, proven_at=utcnow())
        monkeypatch.setattr(auth_routes, "_take_add_account_intent", lambda _s, _r: ("add", sid))

        await _callback("google")

        window = me_account.IDENTITY_LINK_SIGN_IN_WINDOW
        assert real_manager.session_holds_user(sid, OWNER_ID)
        assert real_manager.proven_within(sid, OWNER_ID, window) is linked
        request = MagicMock()
        request.cookies = {me_account.auth_module.SESSION_COOKIE_NAME: sid}
        request.client = None
        request.headers = {}
        service = MagicMock()
        service.link = AsyncMock(return_value=frozenset({OWNER_ID, "local:admin"}))
        with (
            patch.object(me_account, "IdentityLinkService", return_value=service),
            patch.object(me_account, "schedule_security_notification"),
        ):

            async def link():
                return await me_account.link_identity(
                    me_account.IdentityLinkTarget(user_id=OWNER_ID),
                    request,
                    MagicMock(),
                    {"user_id": "local:admin"},
                    AsyncMock(),
                )

            if linked:
                assert (await link()).status == "ok"
            else:
                with pytest.raises(IdentityLinkSignInRequiredError) as refused:
                    await link()
                assert refused.value.status_code == 403
                service.link.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_password_sign_in_proves_the_account_now(monkeypatch, strict) -> None:
    m = MagicMock()
    m.delete_user_sessions.return_value = 0
    m.create_session.return_value = "sess-1"
    monkeypatch.setattr(auth_routes, "_session_manager", m)

    async def _fake_db():
        db = MagicMock()
        db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=lambda: None))
        yield db

    monkeypatch.setattr(auth_routes, "get_db", _fake_db)
    workspaces = MagicMock()
    workspaces.return_value.ensure_personal_workspace = AsyncMock()
    monkeypatch.setattr(auth_routes, "WorkspaceService", workspaces)

    before = utcnow()
    await auth_routes._create_session_and_workspace("local:admin", "a@local", None, "admin")

    assert before <= _proof(m) <= utcnow()


def test_the_certificate_fetch_has_a_short_timeout() -> None:
    """A slow Google must not hold a worker for the library's 120 s default."""
    import auth.oauth2 as oauth2_module

    with patch.object(oauth2_module, "Request") as request_cls:
        oauth2_module._cert_request("https://certs", method="GET")

    request_cls.return_value.assert_called_once_with(
        "https://certs", method="GET", timeout=oauth2_module._CERT_FETCH_TIMEOUT_SECONDS
    )
