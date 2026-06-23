"""Comprehensive coverage tests for ``auth.oauth2.OAuth2Manager``.

The manager wraps Google OAuth2: an InstalledAppFlow desktop login, a Web flow
(authorization URL + code exchange + userinfo), and Fernet-encrypted credential
storage on disk. None of these should touch the network in a unit test, so we:

  * redirect ``get_config_dir()`` to a per-test ``tmp_path`` via
    ``XDG_CONFIG_HOME`` so every manager gets its own throwaway config dir and
    its own freshly generated Fernet key;
  * monkeypatch ``google_auth_oauthlib`` Flow classes, ``google.auth`` Request,
    and ``requests`` so the I/O boundaries are stubbed.

We exercise the encrypt/decrypt round-trip, expiry (de)serialization, automatic
refresh, and every reachable error branch.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet
from google.oauth2.credentials import Credentials

from auth.config import AuthConfig
from auth.exceptions import (
    InvalidCredentialsError,
    NotAuthenticatedError,
    TokenRefreshError,
)
from auth.oauth2 import OAuth2Manager

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``get_config_dir()`` at an isolated tmp dir for this test.

    ``get_config_dir()`` prefers ``/app/config`` if ``/app`` exists, else falls
    back to ``XDG_CONFIG_HOME/kagura``. To stay deterministic across hosts we
    also stub ``os.path.exists`` to report that ``/app`` is absent so the XDG
    branch is always taken.
    """
    import os as _os

    real_exists = _os.path.exists

    def fake_exists(path: str) -> bool:
        if path == "/app":
            return False
        return real_exists(path)

    monkeypatch.setattr("config.paths.os.path.exists", fake_exists)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return tmp_path / "kagura"


@pytest.fixture
def manager(config_dir: Path) -> OAuth2Manager:
    """A manager bound to the isolated config dir (generates its own key)."""
    return OAuth2Manager(provider="google")


class _FakeCredentials:
    """Minimal stand-in for google Credentials used by save/refresh paths.

    ``_save_credentials`` only reads attributes, so a duck type suffices for the
    save side. (Loading goes through the real Credentials class.)
    """

    def __init__(
        self,
        token: str | None = "access-token",
        refresh_token: str | None = "refresh-token",
        token_uri: str = "https://oauth2.googleapis.com/token",
        client_id: str = "client-id",
        client_secret: str = "client-secret",
        scopes: list[str] | None = None,
        expiry: datetime | None = None,
        expired: bool = False,
    ) -> None:
        self.token = token
        self.refresh_token = refresh_token
        self.token_uri = token_uri
        self.client_id = client_id
        self.client_secret = client_secret
        self.scopes = scopes or ["openid"]
        self.expiry = expiry
        self.expired = expired
        self.refresh_called_with: Any = None

    def refresh(self, request: Any) -> None:
        self.refresh_called_with = request
        self.token = "refreshed-token"
        self.expired = False


def _valid_creds_data() -> dict[str, Any]:
    """A credentials dict acceptable to Credentials.from_authorized_user_info."""
    return {
        "token": "access-token",
        "refresh_token": "refresh-token",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "client-id",
        "client_secret": "client-secret",
        "scopes": ["openid"],
    }


# ---------------------------------------------------------------------------
# __init__ / encryption setup
# ---------------------------------------------------------------------------


class TestInitAndEncryptionSetup:
    """Construction wires paths, default config and a Fernet cipher."""

    def test_init_defaults(self, manager: OAuth2Manager, config_dir: Path) -> None:
        """Default provider, a generated AuthConfig and file paths are set."""
        assert manager.provider == "google"
        assert isinstance(manager.config, AuthConfig)
        assert manager.creds_file == config_dir / "credentials.json.enc"
        assert manager.key_file == config_dir / ".key"
        # No explicit secrets path → defaults under config dir.
        assert manager.client_secrets_file == config_dir / "client_secrets.json"

    def test_key_file_generated_on_first_use(self, config_dir: Path) -> None:
        """A new key file is generated and the cipher round-trips data."""
        mgr = OAuth2Manager(provider="google")
        assert mgr.key_file.exists()
        token = mgr.cipher.encrypt(b"secret")
        assert mgr.cipher.decrypt(token) == b"secret"

    def test_existing_key_is_reused(self, config_dir: Path) -> None:
        """A second manager reuses the already-written key (same cipher)."""
        first = OAuth2Manager(provider="google")
        blob = first.cipher.encrypt(b"shared")
        second = OAuth2Manager(provider="google")
        # Same key on disk → second cipher can decrypt the first's ciphertext.
        assert second.cipher.decrypt(blob) == b"shared"

    def test_custom_config_secrets_path(self, config_dir: Path) -> None:
        """An explicit client_secrets_path on the config is honored."""
        custom = config_dir / "my_secrets.json"
        cfg = AuthConfig(provider="google", client_secrets_path=custom)
        mgr = OAuth2Manager(provider="google", config=cfg)
        assert mgr.client_secrets_file == custom


# ---------------------------------------------------------------------------
# is_authenticated / logout
# ---------------------------------------------------------------------------


class TestAuthStateAndLogout:
    """Authentication-state checks and credential removal."""

    def test_is_authenticated_false_when_no_file(self, manager: OAuth2Manager) -> None:
        """No creds file → not authenticated."""
        assert manager.is_authenticated() is False

    def test_is_authenticated_true_when_file_present(self, manager: OAuth2Manager) -> None:
        """A present creds file → authenticated."""
        manager.creds_file.write_bytes(b"anything")
        assert manager.is_authenticated() is True

    def test_logout_removes_credentials(self, manager: OAuth2Manager) -> None:
        """Logout deletes the creds file when authenticated."""
        manager.creds_file.write_bytes(b"anything")
        manager.logout()
        assert not manager.creds_file.exists()

    def test_logout_raises_when_not_authenticated(self, manager: OAuth2Manager) -> None:
        """Logout without credentials raises NotAuthenticatedError."""
        with pytest.raises(NotAuthenticatedError):
            manager.logout()


# ---------------------------------------------------------------------------
# _save_credentials / _load_credentials round-trip
# ---------------------------------------------------------------------------


class TestCredentialPersistence:
    """Fernet encrypt/decrypt round-trip and expiry serialization."""

    def test_save_writes_encrypted_file(self, manager: OAuth2Manager) -> None:
        """Saved bytes are ciphertext (not plaintext JSON) but decrypt back."""
        creds = _FakeCredentials()
        manager._save_credentials(creds)
        raw = manager.creds_file.read_bytes()
        assert b"access-token" not in raw  # encrypted at rest
        decrypted = json.loads(manager.cipher.decrypt(raw))
        assert decrypted["token"] == "access-token"
        assert decrypted["refresh_token"] == "refresh-token"
        assert decrypted["expiry"] is None  # no expiry set

    def test_save_serializes_aware_expiry(self, manager: OAuth2Manager) -> None:
        """An aware expiry is stored as an ISO string with timezone."""
        expiry = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)
        manager._save_credentials(_FakeCredentials(expiry=expiry))
        decrypted = json.loads(manager.cipher.decrypt(manager.creds_file.read_bytes()))
        assert decrypted["expiry"] == expiry.isoformat()

    def test_save_attaches_utc_to_naive_expiry(self, manager: OAuth2Manager) -> None:
        """A naive expiry is assumed UTC before serialization."""
        naive = datetime(2030, 6, 7, 8, 9, 10)  # noqa: DTZ001 - intentional naive
        manager._save_credentials(_FakeCredentials(expiry=naive))
        decrypted = json.loads(manager.cipher.decrypt(manager.creds_file.read_bytes()))
        assert decrypted["expiry"] == naive.replace(tzinfo=UTC).isoformat()

    def test_load_round_trips_credentials(self, manager: OAuth2Manager) -> None:
        """A saved-then-loaded credential reconstructs the core fields."""
        manager._save_credentials(_FakeCredentials())
        loaded = manager._load_credentials()
        assert isinstance(loaded, Credentials)
        assert loaded.token == "access-token"
        assert loaded.refresh_token == "refresh-token"
        assert loaded.client_id == "client-id"

    def test_load_restores_aware_expiry_as_naive_utc(self, manager: OAuth2Manager) -> None:
        """Expiry round-trips back to a naive-UTC datetime (Google convention)."""
        expiry = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)
        manager._save_credentials(_FakeCredentials(expiry=expiry))
        loaded = manager._load_credentials()
        assert loaded.expiry is not None
        # Google auth uses naive UTC datetimes internally.
        assert loaded.expiry.tzinfo is None
        assert loaded.expiry == expiry.replace(tzinfo=None)

    def test_load_handles_offset_expiry_conversion(self, manager: OAuth2Manager) -> None:
        """A non-UTC offset expiry is converted to UTC then made naive."""
        from datetime import timezone as _tz

        # Build a +09:00 (JST) aware datetime explicitly.
        jst = datetime(2030, 1, 2, 12, 0, 0, tzinfo=_tz(timedelta(hours=9)))
        manager._save_credentials(_FakeCredentials(expiry=jst))
        loaded = manager._load_credentials()
        assert loaded.expiry is not None
        assert loaded.expiry.tzinfo is None
        # 12:00 +09:00 == 03:00 UTC
        assert loaded.expiry == datetime(2030, 1, 2, 3, 0, 0)  # noqa: DTZ001

    def test_load_raises_on_corrupt_ciphertext(self, manager: OAuth2Manager) -> None:
        """Garbage that isn't valid Fernet ciphertext raises InvalidCredentials."""
        manager.creds_file.write_bytes(b"not-a-valid-fernet-token")
        with pytest.raises(InvalidCredentialsError, match="Failed to decrypt"):
            manager._load_credentials()

    def test_load_raises_when_decrypted_with_wrong_key(self, manager: OAuth2Manager) -> None:
        """Ciphertext from a foreign key cannot be decrypted → error branch."""
        foreign = Fernet(Fernet.generate_key())
        manager.creds_file.write_bytes(foreign.encrypt(json.dumps(_valid_creds_data()).encode()))
        with pytest.raises(InvalidCredentialsError):
            manager._load_credentials()


# ---------------------------------------------------------------------------
# get_credentials / get_token (refresh paths)
# ---------------------------------------------------------------------------


class TestGetCredentialsAndToken:
    """Credential retrieval, automatic refresh and access-token extraction."""

    def test_get_credentials_raises_when_not_authenticated(self, manager: OAuth2Manager) -> None:
        """No creds file → NotAuthenticatedError before any load."""
        with pytest.raises(NotAuthenticatedError):
            manager.get_credentials()

    def test_get_credentials_no_refresh_when_valid(self, manager: OAuth2Manager) -> None:
        """A non-expired credential is returned without calling refresh."""
        # A far-future expiry guarantees google's ``.expired`` is False, so the
        # refresh branch is skipped and the loaded token is returned as-is.
        future = datetime.now(UTC) + timedelta(days=365)
        manager._save_credentials(_FakeCredentials(expiry=future))
        creds = manager.get_credentials()
        assert creds.token == "access-token"

    def test_get_credentials_refreshes_expired(
        self, manager: OAuth2Manager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An expired credential with a refresh token triggers a refresh + save."""
        fake = _FakeCredentials(expired=True)

        # Patch _load_credentials to hand back our controllable fake.
        monkeypatch.setattr(manager, "_load_credentials", lambda: fake)
        # Avoid constructing a real google Request (no network anyway).
        monkeypatch.setattr("auth.oauth2.Request", lambda: "req-sentinel")
        manager.creds_file.write_bytes(b"present")  # is_authenticated() → True

        saved: list[Any] = []
        monkeypatch.setattr(manager, "_save_credentials", lambda c: saved.append(c))

        creds = manager.get_credentials()
        assert creds is fake
        assert fake.refresh_called_with == "req-sentinel"
        assert fake.token == "refreshed-token"
        assert saved == [fake]  # refreshed creds persisted

    def test_get_credentials_refresh_failure_raises(
        self, manager: OAuth2Manager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failing refresh is wrapped in TokenRefreshError."""

        class _Boom(_FakeCredentials):
            def refresh(self, request: Any) -> None:
                raise RuntimeError("network down")

        fake = _Boom(expired=True)
        monkeypatch.setattr(manager, "_load_credentials", lambda: fake)
        monkeypatch.setattr("auth.oauth2.Request", lambda: "req")
        manager.creds_file.write_bytes(b"present")

        with pytest.raises(TokenRefreshError) as excinfo:
            manager.get_credentials()
        assert "network down" in str(excinfo.value)

    def test_get_credentials_no_refresh_without_refresh_token(
        self, manager: OAuth2Manager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Expired but no refresh_token → refresh branch skipped entirely."""
        fake = _FakeCredentials(expired=True, refresh_token=None)
        called: list[Any] = []
        fake.refresh = lambda req: called.append(req)  # type: ignore[assignment]
        monkeypatch.setattr(manager, "_load_credentials", lambda: fake)
        manager.creds_file.write_bytes(b"present")

        creds = manager.get_credentials()
        assert creds is fake
        assert called == []  # refresh not attempted

    def test_get_token_returns_access_token(
        self, manager: OAuth2Manager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """get_token returns the credential's token string."""
        monkeypatch.setattr(manager, "get_credentials", lambda: _FakeCredentials(token="tok-123"))
        assert manager.get_token() == "tok-123"

    def test_get_token_raises_when_no_token(
        self, manager: OAuth2Manager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A credential with an empty token → InvalidCredentialsError."""
        monkeypatch.setattr(manager, "get_credentials", lambda: _FakeCredentials(token=None))
        with pytest.raises(InvalidCredentialsError, match="No access token"):
            manager.get_token()


# ---------------------------------------------------------------------------
# login (InstalledAppFlow desktop flow)
# ---------------------------------------------------------------------------


class TestLogin:
    """Desktop InstalledAppFlow login, success and failure branches."""

    def test_login_raises_when_secrets_missing(self, manager: OAuth2Manager) -> None:
        """Missing client_secrets.json → FileNotFoundError with guidance."""
        with pytest.raises(FileNotFoundError, match="Client secrets file not found"):
            manager.login()

    def test_login_success_saves_credentials(
        self, manager: OAuth2Manager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A successful flow yields real Credentials that get saved encrypted."""
        manager.client_secrets_file.write_text("{}")  # presence is all login checks

        real_creds = Credentials(
            token="login-token",
            refresh_token="login-refresh",
            token_uri="https://oauth2.googleapis.com/token",
            client_id="cid",
            client_secret="csecret",
            scopes=["openid"],
        )

        class _FakeFlow:
            def run_local_server(self, port: int = 0) -> Credentials:
                assert port == 0
                return real_creds

        monkeypatch.setattr(
            "auth.oauth2.InstalledAppFlow.from_client_secrets_file",
            lambda path, scopes: _FakeFlow(),
        )

        manager.login()
        assert manager.is_authenticated()
        loaded = manager._load_credentials()
        assert loaded.token == "login-token"

    def test_login_rejects_non_oauth2_credentials(
        self, manager: OAuth2Manager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-Credentials result is rejected (wrapped) as InvalidCredentials."""
        manager.client_secrets_file.write_text("{}")

        class _FakeFlow:
            def run_local_server(self, port: int = 0) -> object:
                return object()  # not a Credentials instance

        monkeypatch.setattr(
            "auth.oauth2.InstalledAppFlow.from_client_secrets_file",
            lambda path, scopes: _FakeFlow(),
        )

        with pytest.raises(InvalidCredentialsError, match="Authentication failed"):
            manager.login()
        assert not manager.is_authenticated()

    def test_login_wraps_flow_exception(
        self, manager: OAuth2Manager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An exception during the flow is wrapped in InvalidCredentialsError."""
        manager.client_secrets_file.write_text("{}")

        def _boom(path: str, scopes: Any) -> Any:
            raise RuntimeError("flow exploded")

        monkeypatch.setattr("auth.oauth2.InstalledAppFlow.from_client_secrets_file", _boom)

        with pytest.raises(InvalidCredentialsError, match="flow exploded"):
            manager.login()

    def test_login_uses_config_scopes_when_set(
        self, config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Custom scopes from AuthConfig are passed to the flow factory."""
        cfg = AuthConfig(provider="google", scopes=["custom-scope"])
        mgr = OAuth2Manager(provider="google", config=cfg)
        mgr.client_secrets_file.write_text("{}")

        captured: dict[str, Any] = {}
        real_creds = Credentials(token="t", scopes=["custom-scope"])

        class _FakeFlow:
            def run_local_server(self, port: int = 0) -> Credentials:
                return real_creds

        def _factory(path: str, scopes: Any) -> Any:
            captured["scopes"] = scopes
            return _FakeFlow()

        monkeypatch.setattr("auth.oauth2.InstalledAppFlow.from_client_secrets_file", _factory)
        mgr.login()
        assert captured["scopes"] == ["custom-scope"]


# ---------------------------------------------------------------------------
# Web flow: get_authorization_url_web
# ---------------------------------------------------------------------------


class TestAuthorizationUrlWeb:
    """Web-flow authorization URL construction and validation."""

    def test_builds_url_with_all_params(
        self, manager: OAuth2Manager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The URL embeds client_id, redirect, scopes, state and consent params."""
        monkeypatch.setenv("GOOGLE_CLIENT_ID", "my-client-id")
        url = manager.get_authorization_url_web(
            redirect_uri="https://example.com/cb", state="csrf-123"
        )
        assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
        assert "client_id=my-client-id" in url
        assert "redirect_uri=https://example.com/cb" in url
        assert "state=csrf-123" in url
        assert "response_type=code" in url
        assert "access_type=offline" in url
        assert "prompt=consent" in url
        # WEB_SCOPES joined by spaces.
        assert "openid" in url
        assert "userinfo.email" in url

    def test_raises_without_client_id(
        self, manager: OAuth2Manager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A missing GOOGLE_CLIENT_ID raises ValueError."""
        monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
        with pytest.raises(ValueError, match="GOOGLE_CLIENT_ID"):
            manager.get_authorization_url_web(redirect_uri="https://x/cb", state="s")

    def test_consumes_endpoint_override(
        self, manager: OAuth2Manager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The auth URL is resolved live through the override seam."""
        monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid")
        monkeypatch.setenv("ENVIRONMENT", "development")
        monkeypatch.setenv("OAUTH_ENDPOINT_OVERRIDE_ENABLED", "true")
        monkeypatch.setenv("OAUTH_GOOGLE_AUTH_URL", "http://localhost:9999/auth")
        url = manager.get_authorization_url_web(redirect_uri="http://x/cb", state="s")
        assert url.startswith("http://localhost:9999/auth?")


# ---------------------------------------------------------------------------
# Web flow: exchange_code_web
# ---------------------------------------------------------------------------


class TestExchangeCodeWeb:
    """Web-flow authorization-code exchange, success and failure branches."""

    def test_raises_without_client_credentials(
        self, manager: OAuth2Manager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Missing client id/secret raises ValueError before any flow."""
        monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
        monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
        with pytest.raises(ValueError, match="GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET"):
            manager.exchange_code_web(code="abc", redirect_uri="https://x/cb")

    def test_raises_when_only_client_id_set(
        self, manager: OAuth2Manager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only the id without the secret still raises ValueError."""
        monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid")
        monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
        with pytest.raises(ValueError):
            manager.exchange_code_web(code="abc", redirect_uri="https://x/cb")

    def test_success_returns_flow_credentials(
        self, manager: OAuth2Manager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A successful exchange returns the flow's credentials."""
        monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid")
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "csecret")

        sentinel_creds = Credentials(token="exchanged-token")
        captured: dict[str, Any] = {}

        class _FakeFlow:
            credentials = sentinel_creds

            def fetch_token(self, code: str) -> None:
                captured["code"] = code

        def _from_client_config(
            client_config: dict[str, Any], scopes: Any, redirect_uri: str
        ) -> Any:
            captured["client_config"] = client_config
            captured["redirect_uri"] = redirect_uri
            return _FakeFlow()

        monkeypatch.setattr("auth.oauth2.Flow.from_client_config", _from_client_config)

        result = manager.exchange_code_web(code="the-code", redirect_uri="https://x/cb")
        assert result is sentinel_creds
        assert captured["code"] == "the-code"
        assert captured["redirect_uri"] == "https://x/cb"
        # client_config has the web section with id/secret wired from env.
        web = captured["client_config"]["web"]
        assert web["client_id"] == "cid"
        assert web["client_secret"] == "csecret"
        assert web["redirect_uris"] == ["https://x/cb"]

    def test_failure_wrapped_in_invalid_credentials(
        self, manager: OAuth2Manager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fetch_token failure is wrapped in InvalidCredentialsError."""
        monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid")
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "csecret")

        class _FakeFlow:
            def fetch_token(self, code: str) -> None:
                raise RuntimeError("bad code")

        monkeypatch.setattr(
            "auth.oauth2.Flow.from_client_config",
            lambda client_config, scopes, redirect_uri: _FakeFlow(),
        )

        with pytest.raises(InvalidCredentialsError, match="Code exchange failed"):
            manager.exchange_code_web(code="x", redirect_uri="https://x/cb")

    def test_token_uri_resolved_through_seam(
        self, manager: OAuth2Manager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The token_uri in client_config flows through the override resolver."""
        monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid")
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "csecret")
        monkeypatch.setenv("ENVIRONMENT", "development")
        monkeypatch.setenv("OAUTH_ENDPOINT_OVERRIDE_ENABLED", "true")
        monkeypatch.setenv("OAUTH_GOOGLE_TOKEN_URL", "http://localhost:9999/token")

        captured: dict[str, Any] = {}

        class _FakeFlow:
            credentials = Credentials(token="t")

            def fetch_token(self, code: str) -> None:
                pass

        def _from_client_config(
            client_config: dict[str, Any], scopes: Any, redirect_uri: str
        ) -> Any:
            captured["cfg"] = client_config
            return _FakeFlow()

        monkeypatch.setattr("auth.oauth2.Flow.from_client_config", _from_client_config)
        manager.exchange_code_web(code="x", redirect_uri="https://x/cb")
        assert captured["cfg"]["web"]["token_uri"] == "http://localhost:9999/token"


# ---------------------------------------------------------------------------
# Web flow: get_user_info_web
# ---------------------------------------------------------------------------


class TestGetUserInfoWeb:
    """Userinfo fetch via the requests library (mocked)."""

    def test_returns_user_info_json(
        self, manager: OAuth2Manager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A 200 response's JSON body is returned, with bearer auth header set."""
        captured: dict[str, Any] = {}

        class _FakeResponse:
            def raise_for_status(self) -> None:
                captured["raised"] = False

            def json(self) -> dict[str, Any]:
                return {"sub": "123", "email": "u@example.com", "name": "U"}

        def _fake_get(url: str, headers: dict[str, str]) -> _FakeResponse:
            captured["url"] = url
            captured["headers"] = headers
            return _FakeResponse()

        monkeypatch.setattr("requests.get", _fake_get)

        creds = Credentials(token="bearer-tok")
        info = manager.get_user_info_web(creds)
        assert info == {"sub": "123", "email": "u@example.com", "name": "U"}
        assert captured["headers"]["Authorization"] == "Bearer bearer-tok"
        assert captured["url"] == "https://www.googleapis.com/oauth2/v3/userinfo"

    def test_propagates_http_error(
        self, manager: OAuth2Manager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-2xx response (raise_for_status) propagates the error."""

        class _FakeResponse:
            def raise_for_status(self) -> None:
                raise RuntimeError("401 Unauthorized")

            def json(self) -> dict[str, Any]:  # pragma: no cover - not reached
                return {}

        monkeypatch.setattr("requests.get", lambda url, headers: _FakeResponse())

        with pytest.raises(RuntimeError, match="401 Unauthorized"):
            manager.get_user_info_web(Credentials(token="t"))

    def test_userinfo_url_resolved_through_seam(
        self, manager: OAuth2Manager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The userinfo endpoint is resolved live through the override seam."""
        monkeypatch.setenv("ENVIRONMENT", "development")
        monkeypatch.setenv("OAUTH_ENDPOINT_OVERRIDE_ENABLED", "true")
        monkeypatch.setenv("OAUTH_GOOGLE_USERINFO_URL", "http://localhost:9999/userinfo")
        captured: dict[str, Any] = {}

        class _FakeResponse:
            def raise_for_status(self) -> None:
                pass

            def json(self) -> dict[str, Any]:
                return {}

        def _fake_get(url: str, headers: dict[str, str]) -> _FakeResponse:
            captured["url"] = url
            return _FakeResponse()

        monkeypatch.setattr("requests.get", _fake_get)
        manager.get_user_info_web(Credentials(token="t"))
        assert captured["url"] == "http://localhost:9999/userinfo"
