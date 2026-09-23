"""Server-side terms-of-service acceptance on sign-in (Issue #1665).

``GET /auth/{google,github}/login?accepted_terms=<version>`` stores
``oauth2_accepted_terms:{state}`` beside the CSRF state; the callback reads and
deletes it after the state check. Pinned here:

- the value is bound to one state, consumed once, and inert while
  ``TERMS_VERSION`` is empty;
- a NEW identity without the current version is refused before the signup gate
  (nothing is created, the flow's keys are dropped) — both providers;
- an EXISTING identity with a missing or stale version signs in normally;
- the current version is recorded after ``ensure_user`` (``login``, or ``join``
  when a beta invite rode the same flow);
- password login records on success, and an MFA login carries the value to
  ``/mfa/verify``;
- with ``TERMS_VERSION`` empty nothing changes: no refusal, no lookup, no write.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.responses import RedirectResponse

from api.routes import auth as auth_routes
from config.settings import get_settings
from utils.hashing import sha256_hex

VERSION = "2026-09"
KEY = "oauth2_accepted_terms:{state}"
INVITE_KEY = "oauth2_beta_invite:{state}"
INVITE = "Zk3v_9Qw-" + "a" * 34


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
def terms_on(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "terms_version", VERSION)


@pytest.fixture
def terms_off(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "terms_version", "")


class TestStateBinding:
    def test_bound_to_its_state_with_the_state_lifetime(self, redis, terms_on) -> None:
        auth_routes._remember_accepted_terms("st1", VERSION)

        assert redis.store == {KEY.format(state="st1"): VERSION}
        assert redis.ttls[KEY.format(state="st1")] == 300

    def test_consumed_once(self, redis, terms_on) -> None:
        auth_routes._remember_accepted_terms("st1", VERSION)

        assert auth_routes._take_accepted_terms("st1") == VERSION
        assert redis.store == {}
        assert auth_routes._take_accepted_terms("st1") is None

    def test_not_readable_from_another_state(self, redis, terms_on) -> None:
        auth_routes._remember_accepted_terms("st1", VERSION)

        assert auth_routes._take_accepted_terms("other-state") is None
        assert KEY.format(state="st1") in redis.store

    @pytest.mark.parametrize(
        "value", [None, "", "x" * 65, "has space", "semi;colon", "../etc", "ünï"]
    )
    def test_malformed_value_is_ignored(self, redis, terms_on, value) -> None:
        auth_routes._remember_accepted_terms("st1", value)
        assert redis.store == {}

    def test_inert_while_terms_version_is_empty(self, redis, terms_off) -> None:
        auth_routes._remember_accepted_terms("st1", VERSION)
        assert redis.store == {}

    def test_turned_off_between_login_and_callback(self, redis, monkeypatch) -> None:
        redis.store[KEY.format(state="st1")] = VERSION
        monkeypatch.setattr(get_settings(), "terms_version", "")

        assert auth_routes._take_accepted_terms("st1") is None
        assert redis.store == {}

    def test_tampered_value_is_dropped(self, redis, terms_on) -> None:
        redis.store[KEY.format(state="st1")] = "<script>"
        assert auth_routes._take_accepted_terms("st1") is None
        assert redis.store == {}


class TestLoginRoutes:
    @pytest.mark.asyncio
    async def test_google_login_binds_accepted_terms(self, redis, terms_on, monkeypatch) -> None:
        manager = MagicMock()
        manager.get_authorization_url_web.return_value = "https://idp.example.test/auth"
        monkeypatch.setattr(auth_routes, "_oauth2_manager", manager)
        monkeypatch.setenv("GOOGLE_REDIRECT_URI", "http://localhost:8080/cb")

        response = await auth_routes.google_login(FakeRequest(), accepted_terms=VERSION)

        assert redis.store[KEY.format(state=response.state)] == VERSION

    @pytest.mark.asyncio
    async def test_github_login_binds_accepted_terms(self, redis, terms_on, monkeypatch) -> None:
        monkeypatch.setenv("GITHUB_CLIENT_ID", "client-id")

        response = await auth_routes.github_login(FakeRequest(), accepted_terms=VERSION)

        assert redis.store[KEY.format(state=response.state)] == VERSION

    @pytest.mark.asyncio
    async def test_login_without_the_param_stores_nothing(
        self, redis, terms_on, monkeypatch
    ) -> None:
        monkeypatch.setenv("GITHUB_CLIENT_ID", "client-id")

        await auth_routes.github_login(FakeRequest())

        assert not any(k.startswith("oauth2_accepted_terms:") for k in redis.store)


def _pending(redis: FakeRedis, state: str, *, terms: str | None, return_to: str | None = None):
    redis.store[f"oauth2_state:{state}"] = "pending"
    if terms is not None:
        redis.store[KEY.format(state=state)] = terms
    if return_to is not None:
        redis.store[f"oauth2_return_to:{state}"] = return_to


@pytest.fixture
def github_idp(monkeypatch) -> None:
    monkeypatch.setattr(auth_routes, "_oauth2_manager", MagicMock())
    monkeypatch.setattr(auth_routes, "_github_exchange_code", AsyncMock(return_value="at"))
    monkeypatch.setattr(
        auth_routes,
        "_github_get_user_info",
        AsyncMock(
            return_value={
                "sub": "583231",
                "email": "n@example.test",
                "email_verified": True,
                "login": "octo",
            }
        ),
    )


@pytest.fixture
def google_idp(monkeypatch) -> None:
    manager = MagicMock()
    manager.get_user_info_web.return_value = {
        "sub": "108",
        "email": "n@example.test",
        "email_verified": True,
    }
    monkeypatch.setattr(auth_routes, "_oauth2_manager", manager)
    monkeypatch.setenv("GOOGLE_REDIRECT_URI", "http://localhost:8080/cb")


@pytest.fixture
def gate(monkeypatch) -> AsyncMock:
    """The signup gate, stubbed to block — reaching it means 'not refused by terms'."""
    blocked = RedirectResponse("http://localhost:3000/signup-blocked", status_code=303)
    stub = AsyncMock(return_value=blocked)
    monkeypatch.setattr(auth_routes, "check_signup_access", stub)
    monkeypatch.setattr(auth_routes, "_maybe_link_redirect", AsyncMock(return_value=None))
    return stub


def _set_identity_exists(monkeypatch, exists: bool) -> AsyncMock:
    lookup = AsyncMock(return_value=exists)
    monkeypatch.setattr(auth_routes, "_identity_exists", lookup)
    return lookup


async def _github_callback(state: str):
    return await auth_routes.github_callback(
        FakeRequest(), code="c", state=state, error=None, error_description=None
    )


async def _google_callback(state: str):
    return await auth_routes.google_callback(
        FakeRequest(), code="c", state=state, error=None, error_description=None
    )


class TestNewUserRefusal:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("presented", [None, "2025-01"])
    async def test_github_new_identity_without_the_current_version_is_refused(
        self, redis, terms_on, github_idp, gate, monkeypatch, presented
    ) -> None:
        _set_identity_exists(monkeypatch, False)
        ensure_user = AsyncMock()
        monkeypatch.setattr(
            auth_routes, "get_role_manager", lambda: SimpleNamespace(ensure_user=ensure_user)
        )
        _pending(redis, "st1", terms=presented, return_to="http://localhost:3000/device?c=1")
        redis.store["oauth2_add_to_session:st1"] = "sess"

        response = await _github_callback("st1")

        location = urlparse(response.headers["location"])
        query = parse_qs(location.query)
        assert location.path == "/login"
        assert query["error"] == ["terms_required"]
        assert query["provider"] == ["github"]
        # The flow's destination survives, so agreeing resumes it.
        assert query["return_to"] == ["http://localhost:3000/device?c=1"]
        # Nothing was created, the signup gate never ran (no invite spent) and
        # nothing bound to the state lingers.
        gate.assert_not_awaited()
        ensure_user.assert_not_awaited()
        assert redis.store == {}

    @pytest.mark.asyncio
    async def test_google_new_identity_without_terms_is_refused(
        self, redis, terms_on, google_idp, gate, monkeypatch
    ) -> None:
        _set_identity_exists(monkeypatch, False)
        _pending(redis, "st1", terms=None)

        response = await _google_callback("st1")

        query = parse_qs(urlparse(response.headers["location"]).query)
        assert query["error"] == ["terms_required"]
        assert query["provider"] == ["google"]
        assert "return_to" not in query
        gate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_unsafe_return_to_is_not_reflected(
        self, redis, terms_on, github_idp, gate, monkeypatch
    ) -> None:
        _set_identity_exists(monkeypatch, False)
        _pending(redis, "st1", terms=None, return_to="https://evil.example/x")

        response = await _github_callback("st1")

        assert "evil.example" not in response.headers["location"]

    @pytest.mark.asyncio
    async def test_new_identity_with_the_current_version_reaches_the_gate(
        self, redis, terms_on, github_idp, gate, monkeypatch
    ) -> None:
        lookup = _set_identity_exists(monkeypatch, False)
        _pending(redis, "st1", terms=VERSION)

        response = await _github_callback("st1")

        assert response.headers["location"] == "http://localhost:3000/signup-blocked"
        gate.assert_awaited_once()
        lookup.assert_not_awaited()
        assert KEY.format(state="st1") not in redis.store

    @pytest.mark.asyncio
    async def test_invalid_csrf_state_never_reads_the_acceptance(
        self, redis, terms_on, github_idp, gate
    ) -> None:
        redis.store[KEY.format(state="forged")] = VERSION  # no oauth2_state:forged

        await _github_callback("forged")

        gate.assert_not_awaited()
        assert KEY.format(state="forged") in redis.store


class TestExistingUser:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("presented", [None, "2025-01"])
    async def test_existing_identity_with_missing_or_stale_terms_signs_in(
        self, redis, terms_on, github_idp, gate, monkeypatch, presented
    ) -> None:
        _set_identity_exists(monkeypatch, True)
        _pending(redis, "st1", terms=presented)

        await _github_callback("st1")

        gate.assert_awaited_once()


class TestDisabled:
    @pytest.mark.asyncio
    async def test_no_refusal_and_no_lookup_while_terms_version_is_empty(
        self, redis, terms_off, github_idp, gate, monkeypatch
    ) -> None:
        lookup = _set_identity_exists(monkeypatch, False)
        _pending(redis, "st1", terms=None)

        await _github_callback("st1")

        gate.assert_awaited_once()
        lookup.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_record_is_a_no_op(self, terms_off, monkeypatch) -> None:
        service = MagicMock()
        monkeypatch.setattr(auth_routes, "TermsService", service)

        await auth_routes._record_terms_acceptance(
            user_id="u1",
            email="u@example.test",
            accepted_terms=VERSION,
            source="login",
            request=None,
        )

        service.assert_not_called()


class TestRecordingOnSignIn:
    """Drive the callback through ``ensure_user``; the refresh short-circuit is
    stubbed to stop right after the recording step."""

    @pytest.fixture
    def through_ensure_user(self, monkeypatch) -> AsyncMock:
        monkeypatch.setattr(auth_routes, "check_signup_access", AsyncMock(return_value=None))
        monkeypatch.setattr(auth_routes, "_maybe_link_redirect", AsyncMock(return_value=None))
        role = MagicMock(value="user")
        monkeypatch.setattr(
            auth_routes,
            "get_role_manager",
            lambda: SimpleNamespace(ensure_user=AsyncMock(return_value=role)),
        )
        monkeypatch.setattr(
            auth_routes,
            "_maybe_refresh_redirect",
            AsyncMock(return_value=RedirectResponse("http://localhost:3000/stop", 303)),
        )
        record = AsyncMock()
        monkeypatch.setattr(auth_routes, "_record_terms_acceptance", record)
        _set_identity_exists(monkeypatch, False)
        return record

    @pytest.mark.asyncio
    async def test_google_records_login(
        self, redis, terms_on, google_idp, through_ensure_user
    ) -> None:
        _pending(redis, "st1", terms=VERSION)

        await _google_callback("st1")

        kwargs = through_ensure_user.await_args.kwargs
        assert kwargs["user_id"] == "108"
        assert kwargs["accepted_terms"] == VERSION
        assert kwargs["source"] == "login"

    @pytest.mark.asyncio
    async def test_github_records_join_when_an_invite_rode_the_flow(
        self, redis, terms_on, github_idp, through_ensure_user, monkeypatch
    ) -> None:
        monkeypatch.setattr(get_settings(), "enable_beta_invites", True)
        _pending(redis, "st1", terms=VERSION)
        redis.store[INVITE_KEY.format(state="st1")] = sha256_hex(INVITE)

        await _github_callback("st1")

        kwargs = through_ensure_user.await_args.kwargs
        assert kwargs["user_id"] == "583231"
        assert kwargs["source"] == "join"

    @pytest.mark.asyncio
    async def test_record_helper_writes_only_the_current_version(
        self, terms_on, monkeypatch
    ) -> None:
        record = AsyncMock()
        monkeypatch.setattr(
            auth_routes, "TermsService", MagicMock(return_value=MagicMock(record=record))
        )

        async def _fake_db():
            yield MagicMock()

        monkeypatch.setattr(auth_routes, "get_db", _fake_db)

        for stale in (None, "2025-01"):
            await auth_routes._record_terms_acceptance(
                user_id="u1",
                email="u@example.test",
                accepted_terms=stale,
                source="login",
                request=None,
            )
        record.assert_not_awaited()

        await auth_routes._record_terms_acceptance(
            user_id="u1",
            email="u@example.test",
            accepted_terms=VERSION,
            source="login",
            request=FakeRequest(),
        )
        kwargs = record.await_args.kwargs
        assert kwargs["version"] == VERSION
        assert kwargs["source"] == "login"
        assert kwargs["ip_address"] == "203.0.113.7"

    @pytest.mark.asyncio
    async def test_a_failed_write_does_not_fail_the_sign_in(self, terms_on, monkeypatch) -> None:
        monkeypatch.setattr(
            auth_routes,
            "TermsService",
            MagicMock(return_value=MagicMock(record=AsyncMock(side_effect=RuntimeError("db")))),
        )

        async def _fake_db():
            yield MagicMock()

        monkeypatch.setattr(auth_routes, "get_db", _fake_db)

        await auth_routes._record_terms_acceptance(
            user_id="u1",
            email="u@example.test",
            accepted_terms=VERSION,
            source="login",
            request=None,
        )


class TestPasswordLogin:
    @pytest.fixture
    def password_user(self, monkeypatch):
        user = SimpleNamespace(
            user_id="admin-1",
            email="admin@example.test",
            name="Admin",
            role="admin",
            password_hash="hash",
            totp_enabled=False,
            totp_secret=None,
        )
        db = MagicMock()
        db.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=user))
        )

        async def _fake_db():
            yield db

        monkeypatch.setattr(auth_routes, "get_db", _fake_db)
        monkeypatch.setattr(auth_routes, "verify_password", lambda *_: True)
        monkeypatch.setattr(
            auth_routes, "_create_session_and_workspace", AsyncMock(return_value="sess-1")
        )
        record = AsyncMock()
        monkeypatch.setattr(auth_routes, "_record_terms_acceptance", record)
        return SimpleNamespace(user=user, record=record)

    @pytest.mark.asyncio
    async def test_records_on_success(self, redis, terms_on, password_user) -> None:
        body = auth_routes.PasswordLoginRequest(
            login_id="admin", password="pw", accepted_terms=VERSION
        )

        await auth_routes.password_login(body, FakeRequest(), return_to=None)

        kwargs = password_user.record.await_args.kwargs
        assert kwargs["user_id"] == "admin-1"
        assert kwargs["accepted_terms"] == VERSION
        assert kwargs["source"] == "password"

    @pytest.mark.asyncio
    async def test_mfa_carries_the_acceptance_to_verify(
        self, redis, terms_on, password_user, monkeypatch
    ) -> None:
        password_user.user.totp_enabled = True
        password_user.user.totp_secret = "enc"
        body = auth_routes.PasswordLoginRequest(
            login_id="admin", password="pw", accepted_terms=VERSION
        )

        pending = await auth_routes.password_login(body, FakeRequest(), return_to=None)

        # Nothing is recorded before the second factor.
        password_user.record.assert_not_awaited()
        token = pending.mfa_session_token
        assert redis.store[f"mfa_pending_terms:{token}"] == VERSION

        monkeypatch.setattr(
            auth_routes, "get_encryptor", lambda: MagicMock(decrypt=lambda _: "secret")
        )
        monkeypatch.setattr(auth_routes, "verify_totp", lambda *_: True)

        await auth_routes.mfa_verify(
            auth_routes.MfaVerifyRequest(mfa_session_token=token, totp_code="123456"),
            FakeRequest(),
            return_to=None,
        )

        kwargs = password_user.record.await_args.kwargs
        assert kwargs["accepted_terms"] == VERSION
        assert kwargs["source"] == "password"
        assert redis.store == {}

    @pytest.mark.asyncio
    async def test_failed_mfa_records_nothing_and_drops_the_acceptance(
        self, redis, terms_on, password_user, monkeypatch
    ) -> None:
        password_user.user.totp_enabled = True
        password_user.user.totp_secret = "enc"
        body = auth_routes.PasswordLoginRequest(
            login_id="admin", password="pw", accepted_terms=VERSION
        )
        pending = await auth_routes.password_login(body, FakeRequest(), return_to=None)
        monkeypatch.setattr(
            auth_routes, "get_encryptor", lambda: MagicMock(decrypt=lambda _: "secret")
        )
        monkeypatch.setattr(auth_routes, "verify_totp", lambda *_: False)

        with pytest.raises(auth_routes.AuthenticationError):
            await auth_routes.mfa_verify(
                auth_routes.MfaVerifyRequest(
                    mfa_session_token=pending.mfa_session_token, totp_code="000000"
                ),
                FakeRequest(),
                return_to=None,
            )

        password_user.record.assert_not_awaited()
        assert redis.store == {}

    @pytest.mark.asyncio
    async def test_mfa_binding_is_inert_while_disabled(self, redis, terms_off) -> None:
        auth_routes._remember_mfa_accepted_terms("tok", VERSION)
        assert redis.store == {}

    def test_accepted_terms_is_optional_and_bounded(self) -> None:
        assert auth_routes.PasswordLoginRequest(login_id="a", password="b").accepted_terms is None
        with pytest.raises(ValueError):
            auth_routes.PasswordLoginRequest(login_id="a", password="b", accepted_terms="x" * 65)


class TestAuthMe:
    @pytest.mark.asyncio
    async def test_reports_terms_acceptance_required(self, terms_on, monkeypatch) -> None:
        db_user = SimpleNamespace(
            timezone="UTC",
            locale="en",
            auth_method="oauth",
            auth_provider="google",
            is_initial_admin=False,
        )
        db = MagicMock()
        db.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=db_user))
        )
        required = AsyncMock(return_value=True)
        monkeypatch.setattr(
            auth_routes,
            "TermsService",
            MagicMock(return_value=MagicMock(acceptance_required=required)),
        )

        result = await auth_routes.get_current_user_info(
            user={"user_id": "u1", "email": "u@example.test"}, db=db
        )

        assert result["user"]["terms_acceptance_required"] is True
        required.assert_awaited_once_with("u1")
