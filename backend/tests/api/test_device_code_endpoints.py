"""Integration tests for Device Authorization Grant endpoints (Issue #536).

Tests the three RFC 8628 endpoints:
- POST /api/v1/oauth/device/authorize
- POST /api/v1/oauth/device/verify
- POST /api/v1/oauth/device/confirm

Plus the per-client-address request limits on ``device/authorize`` and
``device/verify`` (#1656), and the form-encoded and JSON request bodies
``device/authorize`` accepts (#1671).
"""

import sys
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

_BACKEND_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from api.main import app  # noqa: E402
from models.auth import OAuth2Client, OAuth2DeviceCode  # noqa: E402
from utils.datetime import utcnow  # noqa: E402
from utils.exceptions import RedisError  # noqa: E402


@pytest.fixture(autouse=True)
def _under_rate_limit():
    """Keep the per-IP counter off Redis: every call is the first in its window."""
    with patch("api.routes.oauth.increment_counter", AsyncMock(return_value=1)) as counter:
        yield counter


def _device_settings(**overrides):
    values = {
        "oauth_device_code_expires_in": 600,
        "oauth_device_polling_interval": 5,
        "oauth_device_authorize_rate_limit_per_minute": 10,
        "oauth_device_verify_rate_limit_per_minute": 30,
        "frontend_url": "https://memory.example.test",
    }
    values.update(overrides)
    return MagicMock(**values)


@pytest.fixture
def test_oauth_client():
    return OAuth2Client(
        client_id="oauth_test_dev_536",
        client_secret_hash="sha256_test_secret_hash_placeholder",
        client_name="Test CLI Client",
        grant_types=[
            "authorization_code",
            "refresh_token",
            "urn:ietf:params:oauth:grant-type:device_code",
        ],
        scope="memory:read memory:write offline_access",
        redirect_uris=["http://localhost:60801/callback"],
        token_endpoint_auth_method="client_secret_post",
        provider="claude",
    )


@pytest.fixture
def test_device_code(test_oauth_client):
    return OAuth2DeviceCode(
        device_code="full-device-code-for-testing-abc123",
        user_code="TST12345",
        client_id=test_oauth_client.client_id,
        scope="memory:read memory:write",
        expires_at=utcnow() + timedelta(seconds=600),
    )


class TestDeviceAuthorizeEndpoint:
    def test_authorize_success(self, test_oauth_client):
        with patch("api.routes.oauth.get_sync_session") as mock_session_fn:
            mock_db = MagicMock()
            mock_db.query().filter_by().first.return_value = test_oauth_client
            mock_db.add.return_value = None
            mock_db.commit.return_value = None
            mock_db.close.return_value = None
            mock_session_fn.return_value = mock_db

            with patch("api.routes.oauth.get_settings") as mock_settings:
                mock_settings.return_value = _device_settings()

                client = TestClient(app)
                resp = client.post(
                    "/api/v1/oauth/device/authorize",
                    json={"client_id": "oauth_test_dev_536", "scope": "memory:read"},
                )

            assert resp.status_code == 200
            data = resp.json()
            assert "device_code" in data
            assert len(data["user_code"]) == 8
            assert data["verification_uri"] == "https://memory.example.test/device"
            assert data["expires_in"] == 600
            assert data["interval"] == 5

    def test_authorize_unknown_client(self):
        with patch("api.routes.oauth.get_sync_session") as mock_session_fn:
            mock_db = MagicMock()
            mock_db.query().filter_by().first.return_value = None
            mock_db.close.return_value = None
            mock_session_fn.return_value = mock_db

            client = TestClient(app)
            resp = client.post(
                "/api/v1/oauth/device/authorize",
                json={"client_id": "unknown_client", "scope": "memory:read"},
            )

        # RFC 6749 §5.2 error shape (#1671), not the first-party envelope.
        assert resp.status_code == 400
        assert resp.json() == {"error": "invalid_client", "error_description": "Unknown client_id"}

    def test_authorize_scope_intersection(self, test_oauth_client):
        with patch("api.routes.oauth.get_sync_session") as mock_session_fn:
            mock_db = MagicMock()
            mock_db.query().filter_by().first.return_value = test_oauth_client
            mock_db.commit.return_value = None
            mock_db.close.return_value = None
            mock_session_fn.return_value = mock_db

            with patch("api.routes.oauth.get_settings") as mock_settings:
                mock_settings.return_value = _device_settings()

                client = TestClient(app)
                resp = client.post(
                    "/api/v1/oauth/device/authorize",
                    json={
                        "client_id": "oauth_test_dev_536",
                        "scope": "memory:read memory:admin",
                    },
                )

            assert resp.status_code == 200
            # memory:admin not in client's allowed scope
            assert "device_code" in resp.json()


class TestDeviceVerifyEndpoint:
    def test_verify_success(self, test_device_code, test_oauth_client):
        with patch("api.routes.oauth.get_sync_session") as mock_session_fn:
            mock_db = MagicMock()

            def query_side_effect(model):
                mock_q = MagicMock()
                if model is OAuth2DeviceCode:
                    mock_q.filter_by().first.return_value = test_device_code
                elif model is OAuth2Client:
                    mock_q.filter_by().first.return_value = test_oauth_client
                return mock_q

            mock_db.query.side_effect = query_side_effect
            mock_db.close.return_value = None
            mock_session_fn.return_value = mock_db

            client = TestClient(app)
            resp = client.post(
                "/api/v1/oauth/device/verify",
                json={"user_code": "TST12345"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["user_code"] == "TST12345"
        assert data["client_name"] == "Test CLI Client"
        assert data["is_authorized"] is False
        assert data["is_expired"] is False

    def test_verify_not_found(self):
        with patch("api.routes.oauth.get_sync_session") as mock_session_fn:
            mock_db = MagicMock()
            mock_q = MagicMock()
            mock_q.filter_by().first.return_value = None
            mock_db.query.return_value = mock_q
            mock_db.close.return_value = None
            mock_session_fn.return_value = mock_db

            client = TestClient(app)
            resp = client.post(
                "/api/v1/oauth/device/verify",
                json={"user_code": "NONEXIST"},
            )

        assert resp.status_code == 404

    def test_verify_expired(self, test_device_code, test_oauth_client):
        test_device_code.expires_at = utcnow() - timedelta(seconds=1)

        with patch("api.routes.oauth.get_sync_session") as mock_session_fn:
            mock_db = MagicMock()

            def query_side_effect(model):
                mock_q = MagicMock()
                if model is OAuth2DeviceCode:
                    mock_q.filter_by().first.return_value = test_device_code
                elif model is OAuth2Client:
                    mock_q.filter_by().first.return_value = test_oauth_client
                return mock_q

            mock_db.query.side_effect = query_side_effect
            mock_db.close.return_value = None
            mock_session_fn.return_value = mock_db

            client = TestClient(app)
            resp = client.post(
                "/api/v1/oauth/device/verify",
                json={"user_code": "TST12345"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["is_expired"] is True

    def test_verify_already_authorized(self, test_device_code, test_oauth_client):
        test_device_code.authorized_at = utcnow() - timedelta(seconds=30)
        test_device_code.user_id = "test_user_123"

        with patch("api.routes.oauth.get_sync_session") as mock_session_fn:
            mock_db = MagicMock()

            def query_side_effect(model):
                mock_q = MagicMock()
                if model is OAuth2DeviceCode:
                    mock_q.filter_by().first.return_value = test_device_code
                elif model is OAuth2Client:
                    mock_q.filter_by().first.return_value = test_oauth_client
                return mock_q

            mock_db.query.side_effect = query_side_effect
            mock_db.close.return_value = None
            mock_session_fn.return_value = mock_db

            client = TestClient(app)
            resp = client.post(
                "/api/v1/oauth/device/verify",
                json={"user_code": "TST12345"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["is_authorized"] is True


class TestDeviceConfirmEndpoint:
    def test_confirm_approve(self, test_device_code):
        with patch("api.routes.oauth._get_user_from_session") as mock_get_user:
            mock_get_user.return_value = {
                "user_id": "test_user_123",
                "email": "test@example.com",
            }

            with patch("api.routes.oauth.get_sync_session") as mock_session_fn:
                mock_db = MagicMock()
                mock_q = MagicMock()
                mock_q.filter_by().with_for_update().first.return_value = test_device_code
                mock_db.query.return_value = mock_q
                mock_db.commit.return_value = None
                mock_db.close.return_value = None
                mock_session_fn.return_value = mock_db

                client = TestClient(app)
                resp = client.post(
                    "/api/v1/oauth/device/confirm",
                    json={"user_code": "TST12345", "approve": True},
                )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "approved"
        assert test_device_code.authorized_at is not None
        assert test_device_code.user_id == "test_user_123"

    def test_confirm_deny(self, test_device_code):
        with patch("api.routes.oauth._get_user_from_session") as mock_get_user:
            mock_get_user.return_value = {
                "user_id": "test_user_123",
                "email": "test@example.com",
            }

            with patch("api.routes.oauth.get_sync_session") as mock_session_fn:
                mock_db = MagicMock()
                mock_q = MagicMock()
                mock_q.filter_by().with_for_update().first.return_value = test_device_code
                mock_db.query.return_value = mock_q
                mock_db.commit.return_value = None
                mock_db.close.return_value = None
                mock_session_fn.return_value = mock_db

                client = TestClient(app)
                resp = client.post(
                    "/api/v1/oauth/device/confirm",
                    json={"user_code": "TST12345", "approve": False},
                )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "denied"
        assert test_device_code.denied_at is not None

    def test_confirm_unauthenticated(self):
        with patch("api.routes.oauth._get_user_from_session") as mock_get_user:
            mock_get_user.return_value = None

            with patch("api.routes.oauth.get_sync_session"):
                client = TestClient(app)
                resp = client.post(
                    "/api/v1/oauth/device/confirm",
                    json={"user_code": "TST12345", "approve": True},
                )

        assert resp.status_code == 401

    def test_confirm_expired_code(self, test_device_code):
        test_device_code.expires_at = utcnow() - timedelta(seconds=1)

        with patch("api.routes.oauth._get_user_from_session") as mock_get_user:
            mock_get_user.return_value = {
                "user_id": "test_user_123",
                "email": "test@example.com",
            }

            with patch("api.routes.oauth.get_sync_session") as mock_session_fn:
                mock_db = MagicMock()
                mock_q = MagicMock()
                mock_q.filter_by().with_for_update().first.return_value = test_device_code
                mock_db.query.return_value = mock_q
                mock_db.close.return_value = None
                mock_session_fn.return_value = mock_db

                client = TestClient(app)
                resp = client.post(
                    "/api/v1/oauth/device/confirm",
                    json={"user_code": "TST12345", "approve": True},
                )

        assert resp.status_code == 404
        assert "expired" in resp.json()["message"].lower()

    def test_confirm_already_processed(self, test_device_code):
        test_device_code.authorized_at = utcnow() - timedelta(seconds=30)
        test_device_code.user_id = "test_user_123"

        with patch("api.routes.oauth._get_user_from_session") as mock_get_user:
            mock_get_user.return_value = {
                "user_id": "other_user",
                "email": "other@example.com",
            }

            with patch("api.routes.oauth.get_sync_session") as mock_session_fn:
                mock_db = MagicMock()
                mock_q = MagicMock()
                mock_q.filter_by().with_for_update().first.return_value = test_device_code
                mock_db.query.return_value = mock_q
                mock_db.close.return_value = None
                mock_session_fn.return_value = mock_db

                client = TestClient(app)
                resp = client.post(
                    "/api/v1/oauth/device/confirm",
                    json={"user_code": "TST12345", "approve": True},
                )

        assert resp.status_code == 409


class TestTokenEndpointDefenseInDepth:
    """Regression guard for Issue #638 defense-in-depth: unhandled exceptions
    in ``_run_oauth_sync`` are shaped as RFC 6749 ``server_error`` JSON instead
    of Starlette's default plain-text 500.

    Pre-fix the bug from Issue #635 surfaced as ``Content-Type: text/plain``
    body ``Internal Server Error`` (21 bytes). RFC 6749 §5.2 mandates JSON
    error responses on the token endpoint, so even genuine 500s should carry
    structured ``{error, error_description}`` for client tooling.
    """

    def test_unhandled_exception_returns_rfc6749_server_error_json(self):
        with patch(
            "api.routes.oauth._run_oauth_sync",
            side_effect=RuntimeError("simulated authlib failure"),
        ):
            client = TestClient(app)
            resp = client.post(
                "/api/v1/oauth/token/",
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "device_code": "any-device-code-since-we-mocked-the-runner",
                    "client_id": "kagura-cli",
                },
            )

        # Regression: pre-fix this returned 500 text/plain "Internal Server Error".
        assert resp.status_code == 500
        content_type = resp.headers.get("content-type", "")
        assert content_type.startswith("application/json"), (
            f"expected JSON content-type, got {content_type!r}"
        )

        body = resp.json()
        assert body == {
            "error": "server_error",
            "error_description": "internal authorization server error",
        }

        # RFC 6749 §5.1 cache directives on token error responses
        assert resp.headers.get("cache-control") == "no-store"
        assert resp.headers.get("pragma") == "no-cache"


# ============================================================================
# Per-client-address request limits (#1656)
# ============================================================================

_AUTHORIZE = "/api/v1/oauth/device/authorize"
_VERIFY = "/api/v1/oauth/device/verify"


def _fake_counter():
    """In-memory stand-in for ``increment_counter``: one count per key."""
    counts: dict[str, int] = {}

    async def increment(key: str, ttl: int | None = None) -> int:
        counts[key] = counts.get(key, 0) + 1
        return counts[key]

    return increment, counts


def _post_authorize(client: TestClient, encoding: str, fields: dict[str, str]):
    """POST ``fields`` to device/authorize as a form (RFC 8628 §3.1) or JSON body."""
    if encoding == "form":
        return client.post(_AUTHORIZE, data=fields)
    return client.post(_AUTHORIZE, json=fields)


def _verify_session(device, oauth_client):
    mock_db = MagicMock()

    def query_side_effect(model):
        mock_q = MagicMock()
        if model is OAuth2DeviceCode:
            mock_q.filter_by().first.return_value = device
        elif model is OAuth2Client:
            mock_q.filter_by().first.return_value = oauth_client
        return mock_q

    mock_db.query.side_effect = query_side_effect
    return mock_db


class TestDeviceAuthorizeRateLimit:
    def test_over_limit_returns_rfc6749_429_and_inserts_no_row(self):
        with (
            patch("api.routes.oauth.increment_counter", AsyncMock(return_value=11)),
            patch("api.routes.oauth.get_settings", return_value=_device_settings()),
            patch("api.routes.oauth.get_sync_session") as mock_session_fn,
        ):
            resp = TestClient(app).post(
                _AUTHORIZE, json={"client_id": "oauth_test_dev_536", "scope": "memory:read"}
            )

        assert resp.status_code == 429
        body = resp.json()
        assert set(body) == {"error", "error_description"}
        assert body["error"] == "invalid_request"
        assert body["error_description"]
        assert resp.headers["cache-control"] == "no-store"
        assert resp.headers["retry-after"] == "60"
        # The limit applies before the database session opens: no row is added.
        mock_session_fn.assert_not_called()

    def test_at_limit_is_served(self, test_oauth_client):
        with (
            patch("api.routes.oauth.increment_counter", AsyncMock(return_value=10)),
            patch("api.routes.oauth.get_settings", return_value=_device_settings()),
            patch("api.routes.oauth.get_sync_session") as mock_session_fn,
        ):
            mock_db = MagicMock()
            mock_db.query().filter_by().first.return_value = test_oauth_client
            mock_session_fn.return_value = mock_db
            resp = TestClient(app).post(
                _AUTHORIZE, json={"client_id": "oauth_test_dev_536", "scope": "memory:read"}
            )

        assert resp.status_code == 200
        assert mock_db.add.call_count == 1

    def test_limit_comes_from_settings(self, test_oauth_client):
        with (
            patch("api.routes.oauth.increment_counter", AsyncMock(return_value=3)),
            patch(
                "api.routes.oauth.get_settings",
                return_value=_device_settings(oauth_device_authorize_rate_limit_per_minute=2),
            ),
            patch("api.routes.oauth.get_sync_session") as mock_session_fn,
        ):
            resp = TestClient(app).post(
                _AUTHORIZE, json={"client_id": "oauth_test_dev_536", "scope": "memory:read"}
            )

        assert resp.status_code == 429
        mock_session_fn.assert_not_called()

    def test_counter_key_carries_client_address_and_one_minute_window(self, _under_rate_limit):
        with patch("api.routes.oauth.get_sync_session") as mock_session_fn:
            mock_db = MagicMock()
            mock_db.query().filter_by().first.return_value = None
            mock_session_fn.return_value = mock_db
            TestClient(app, client=("203.0.113.7", 50000)).post(
                _AUTHORIZE, json={"client_id": "unknown_client"}
            )

        _under_rate_limit.assert_awaited_once_with("device_authorize:203.0.113.7", ttl=60)

    def test_two_addresses_have_separate_budgets(self, test_oauth_client):
        increment, counts = _fake_counter()
        settings = _device_settings(oauth_device_authorize_rate_limit_per_minute=2)
        with (
            patch("api.routes.oauth.increment_counter", side_effect=increment),
            patch("api.routes.oauth.get_settings", return_value=settings),
            patch("api.routes.oauth.get_sync_session") as mock_session_fn,
        ):
            mock_db = MagicMock()
            mock_db.query().filter_by().first.return_value = test_oauth_client
            mock_session_fn.return_value = mock_db
            first = TestClient(app, client=("198.51.100.1", 50000))
            second = TestClient(app, client=("198.51.100.2", 50000))
            payload = {"client_id": "oauth_test_dev_536"}

            statuses_first = [first.post(_AUTHORIZE, json=payload).status_code for _ in range(3)]
            status_second = second.post(_AUTHORIZE, json=payload).status_code

        assert statuses_first == [200, 200, 429]
        assert status_second == 200
        assert counts == {"device_authorize:198.51.100.1": 3, "device_authorize:198.51.100.2": 1}

    def test_redis_error_fails_open_with_one_warning(self, test_oauth_client):
        with (
            patch(
                "api.routes.oauth.increment_counter",
                AsyncMock(side_effect=RedisError("Failed to increment counter")),
            ),
            patch("api.routes.oauth.get_settings", return_value=_device_settings()),
            patch("api.routes.oauth.get_sync_session") as mock_session_fn,
            patch("api.routes.oauth.logger") as mock_logger,
        ):
            mock_db = MagicMock()
            mock_db.query().filter_by().first.return_value = test_oauth_client
            mock_session_fn.return_value = mock_db
            resp = TestClient(app).post(_AUTHORIZE, json={"client_id": "oauth_test_dev_536"})

        assert resp.status_code == 200
        assert mock_logger.warning.call_count == 1
        assert mock_logger.warning.call_args.args[0] == "device_flow_rate_limit_unavailable"
        assert mock_logger.warning.call_args.kwargs["endpoint"] == "device_authorize"


class TestDeviceVerifyRateLimit:
    def test_over_limit_returns_429_with_detail_and_runs_no_lookup(self):
        with (
            patch("api.routes.oauth.increment_counter", AsyncMock(return_value=31)),
            patch("api.routes.oauth.get_settings", return_value=_device_settings()),
            patch("api.routes.oauth.get_sync_session") as mock_session_fn,
        ):
            resp = TestClient(app).post(_VERIFY, json={"user_code": "TST12345"})

        assert resp.status_code == 429
        body = resp.json()
        # First-party JSON API: the HTTPException ``detail`` travels as the
        # canonical envelope's ``message`` (see http_exception_handler).
        assert body["error"] == "HTTP-429"
        assert body["message"]
        assert resp.headers["retry-after"] == "60"
        mock_session_fn.assert_not_called()

    def test_at_limit_is_served(self, test_device_code, test_oauth_client):
        with (
            patch("api.routes.oauth.increment_counter", AsyncMock(return_value=30)),
            patch("api.routes.oauth.get_settings", return_value=_device_settings()),
            patch(
                "api.routes.oauth.get_sync_session",
                return_value=_verify_session(test_device_code, test_oauth_client),
            ),
        ):
            resp = TestClient(app).post(_VERIFY, json={"user_code": "TST12345"})

        assert resp.status_code == 200
        assert resp.json()["user_code"] == "TST12345"

    def test_unknown_code_is_counted_too(self, _under_rate_limit):
        with patch("api.routes.oauth.get_sync_session", return_value=_verify_session(None, None)):
            resp = TestClient(app, client=("203.0.113.9", 50000)).post(
                _VERIFY, json={"user_code": "NONEXIST"}
            )

        assert resp.status_code == 404
        _under_rate_limit.assert_awaited_once_with("device_verify:203.0.113.9", ttl=60)

    def test_two_addresses_have_separate_budgets(self):
        increment, counts = _fake_counter()
        settings = _device_settings(oauth_device_verify_rate_limit_per_minute=1)
        with (
            patch("api.routes.oauth.increment_counter", side_effect=increment),
            patch("api.routes.oauth.get_settings", return_value=settings),
            patch("api.routes.oauth.get_sync_session", return_value=_verify_session(None, None)),
        ):
            first = TestClient(app, client=("198.51.100.1", 50000))
            second = TestClient(app, client=("198.51.100.2", 50000))
            payload = {"user_code": "NONEXIST"}

            statuses_first = [first.post(_VERIFY, json=payload).status_code for _ in range(2)]
            status_second = second.post(_VERIFY, json=payload).status_code

        assert statuses_first == [404, 429]
        assert status_second == 404
        assert counts == {"device_verify:198.51.100.1": 2, "device_verify:198.51.100.2": 1}

    def test_redis_error_fails_open_with_one_warning(self, test_device_code, test_oauth_client):
        with (
            patch(
                "api.routes.oauth.increment_counter",
                AsyncMock(side_effect=RedisError("Failed to increment counter")),
            ),
            patch(
                "api.routes.oauth.get_sync_session",
                return_value=_verify_session(test_device_code, test_oauth_client),
            ),
            patch("api.routes.oauth.logger") as mock_logger,
        ):
            resp = TestClient(app).post(_VERIFY, json={"user_code": "TST12345"})

        assert resp.status_code == 200
        assert mock_logger.warning.call_count == 1
        assert mock_logger.warning.call_args.args[0] == "device_flow_rate_limit_unavailable"
        assert mock_logger.warning.call_args.kwargs["endpoint"] == "device_verify"


class TestDeviceSignInWithinLimits:
    """A whole device sign-in stays within both limits and still yields a token.

    Runs authorize → verify → confirm → token polling against an in-memory
    SQLite database holding the OAuth tables, with the real per-IP counter
    logic driven by an in-memory stand-in for Redis.
    """

    @pytest.fixture
    def sync_sessionmaker(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool

        from models.auth import OAuth2Token, User, Workspace

        engine = create_engine(
            "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
        )
        for model in (User, Workspace, OAuth2Client, OAuth2DeviceCode, OAuth2Token):
            model.__table__.create(engine)
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        with factory() as db:
            db.add(
                OAuth2Client(
                    client_id="device-flow-test-cli",
                    client_secret_hash="",
                    client_name="Test CLI",
                    grant_types=["urn:ietf:params:oauth:grant-type:device_code"],
                    response_types=[],
                    scope="memory:read memory:write offline_access",
                    redirect_uris=[],
                    token_endpoint_auth_method="none",
                    provider="claude",
                )
            )
            db.commit()
        yield factory
        engine.dispose()

    # The RFC 8628 §3.1 form body and the JSON body both complete a sign-in (#1671).
    @pytest.mark.parametrize("encoding", ["form", "json"])
    def test_full_sign_in_returns_token(self, sync_sessionmaker, encoding):
        increment, counts = _fake_counter()
        grant_type = "urn:ietf:params:oauth:grant-type:device_code"
        with (
            patch("api.routes.oauth.increment_counter", side_effect=increment),
            patch("api.routes.oauth.get_sync_session", side_effect=sync_sessionmaker),
            patch(
                "api.routes.oauth._get_user_from_session",
                return_value={"user_id": "device_flow_user", "email": "user@example.test"},
            ),
        ):
            # Authlib refuses the token endpoint over plain http.
            client = TestClient(app, base_url="https://testserver")
            auth = _post_authorize(
                client,
                encoding,
                {"client_id": "device-flow-test-cli", "scope": "memory:read"},
            )
            assert auth.status_code == 200, auth.text
            grant = auth.json()
            poll_form = {
                "grant_type": grant_type,
                "device_code": grant["device_code"],
                "client_id": "device-flow-test-cli",
            }

            # The CLI polls before the user has approved.
            pending = client.post("/api/v1/oauth/token", data=poll_form)
            assert pending.status_code == 400, pending.text
            assert pending.json()["error"] == "authorization_pending"

            verified = client.post(_VERIFY, json={"user_code": grant["user_code"]})
            assert verified.status_code == 200, verified.text
            confirmed = client.post(
                "/api/v1/oauth/device/confirm",
                json={"user_code": grant["user_code"], "approve": True},
            )
            assert confirmed.status_code == 200, confirmed.text

            # The CLI waits ``interval`` seconds before its next poll.
            with sync_sessionmaker() as db:
                row = db.query(OAuth2DeviceCode).filter_by(user_code=grant["user_code"]).one()
                row.last_polled_at = utcnow() - timedelta(seconds=grant["interval"] + 1)
                db.commit()

            token = client.post("/api/v1/oauth/token", data=poll_form)

        assert token.status_code == 200, token.text
        assert token.json()["access_token"]
        assert counts == {"device_authorize:testclient": 1, "device_verify:testclient": 1}


# ============================================================================
# Request encodings on device/authorize (#1671)
# ============================================================================

_FORM = "application/x-www-form-urlencoded"


def _authorize_session(oauth_client):
    """A sync session whose client lookup returns ``oauth_client``."""
    mock_db = MagicMock()
    mock_db.query().filter_by().first.return_value = oauth_client
    return mock_db


def _assert_rfc6749_error(resp, status_code: int, error: str) -> None:
    assert resp.status_code == status_code, resp.text
    body = resp.json()
    assert set(body) == {"error", "error_description"}
    assert body["error"] == error
    assert body["error_description"]
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["pragma"] == "no-cache"


class TestDeviceAuthorizeRequestEncodings:
    """device/authorize reads the RFC 8628 §3.1 form body as well as JSON."""

    @pytest.fixture(autouse=True)
    def _settings(self):
        with patch("api.routes.oauth.get_settings", return_value=_device_settings()):
            yield

    def test_form_request_returns_device_grant(self, test_oauth_client):
        mock_db = _authorize_session(test_oauth_client)
        with patch("api.routes.oauth.get_sync_session", return_value=mock_db):
            resp = TestClient(app).post(
                _AUTHORIZE, data={"client_id": "oauth_test_dev_536", "scope": "memory:read"}
            )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["device_code"]
        assert len(data["user_code"]) == 8
        assert data["verification_uri"] == "https://memory.example.test/device"
        assert data["expires_in"] == 600
        assert data["interval"] == 5
        row = mock_db.add.call_args.args[0]
        assert row.client_id == "oauth_test_dev_536"
        assert row.scope == "memory:read"

    def test_form_without_scope_gets_the_client_scope(self, test_oauth_client):
        mock_db = _authorize_session(test_oauth_client)
        with patch("api.routes.oauth.get_sync_session", return_value=mock_db):
            resp = TestClient(app).post(_AUTHORIZE, data={"client_id": "oauth_test_dev_536"})

        assert resp.status_code == 200, resp.text
        assert mock_db.add.call_args.args[0].scope == test_oauth_client.scope

    @pytest.mark.parametrize(
        "content_type",
        [
            "application/x-www-form-urlencoded; charset=UTF-8",
            "Application/X-WWW-Form-Urlencoded",
            "application/json; charset=utf-8",
            "APPLICATION/JSON",
            "application/vnd.example+json",
            None,
        ],
        ids=[
            "form-charset",
            "form-mixed-case",
            "json-charset",
            "json-upper",
            "json-suffix",
            "none",
        ],
    )
    def test_media_type_parameters_and_case_are_accepted(self, test_oauth_client, content_type):
        is_form = content_type is not None and "form" in content_type.lower()
        content = (
            b"client_id=oauth_test_dev_536&scope=memory%3Aread"
            if is_form
            else b'{"client_id": "oauth_test_dev_536", "scope": "memory:read"}'
        )
        # No Content-Type at all is read as JSON, as FastAPI's body parsing did
        # before #1671, so an existing JSON client that omits the header keeps working.
        headers = {"Content-Type": content_type} if content_type else {}
        mock_db = _authorize_session(test_oauth_client)
        with patch("api.routes.oauth.get_sync_session", return_value=mock_db):
            resp = TestClient(app).post(_AUTHORIZE, content=content, headers=headers)

        assert resp.status_code == 200, resp.text
        assert mock_db.add.call_args.args[0].scope == "memory:read"

    @pytest.mark.parametrize("encoding", ["form", "json"])
    def test_unknown_client_is_invalid_client(self, encoding):
        with patch("api.routes.oauth.get_sync_session", return_value=_authorize_session(None)):
            resp = _post_authorize(TestClient(app), encoding, {"client_id": "no_such_client_1671"})

        _assert_rfc6749_error(resp, 400, "invalid_client")
        # The rejected value is not reflected back.
        assert "no_such_client_1671" not in resp.text

    @pytest.mark.parametrize("encoding", ["form", "json"])
    def test_missing_client_id_is_invalid_request(self, encoding):
        with patch("api.routes.oauth.get_sync_session") as mock_session_fn:
            resp = _post_authorize(TestClient(app), encoding, {"scope": "memory:read"})

        _assert_rfc6749_error(resp, 400, "invalid_request")
        assert "client_id" in resp.json()["error_description"]
        mock_session_fn.assert_not_called()

    @pytest.mark.parametrize(
        "content_type,content",
        [
            ("text/plain", b'{"client_id": "oauth_test_dev_536"}'),
            ("multipart/form-data; boundary=x1671", b"client_id=oauth_test_dev_536"),
            ("application/xml", b"<client_id>oauth_test_dev_536</client_id>"),
            ("application/jsonl", b'{"client_id": "oauth_test_dev_536"}'),
        ],
        ids=["text-plain", "multipart", "xml", "json-lookalike"],
    )
    def test_unsupported_content_type_is_invalid_request(self, content_type, content):
        with patch("api.routes.oauth.get_sync_session") as mock_session_fn:
            resp = TestClient(app).post(
                _AUTHORIZE, content=content, headers={"Content-Type": content_type}
            )

        _assert_rfc6749_error(resp, 400, "invalid_request")
        assert "Content-Type" in resp.json()["error_description"]
        mock_session_fn.assert_not_called()

    @pytest.mark.parametrize(
        "content_type,content",
        [
            ("application/json", b'{"client_id": '),
            ("application/json", b'["oauth_test_dev_536"]'),
            ("application/json", b'{"client_id": 1671}'),
            ("application/json", b""),
            (_FORM, b"client_id=oauth_test_dev_536&client_id=other"),
            (_FORM, b"client_id=%FF"),
            (_FORM, b"client_id=\xff"),
        ],
        ids=[
            "json-truncated",
            "json-array",
            "json-number",
            "json-empty",
            "form-repeated-param",
            "form-bad-percent-escape",
            "form-not-utf8",
        ],
    )
    def test_malformed_body_is_invalid_request(self, content_type, content):
        with patch("api.routes.oauth.get_sync_session") as mock_session_fn:
            resp = TestClient(app).post(
                _AUTHORIZE, content=content, headers={"Content-Type": content_type}
            )

        _assert_rfc6749_error(resp, 400, "invalid_request")
        mock_session_fn.assert_not_called()

    def test_unrecognised_form_parameters_are_ignored(self, test_oauth_client):
        """RFC 6749 §3.1: unrecognised request parameters are ignored."""
        with patch(
            "api.routes.oauth.get_sync_session",
            return_value=_authorize_session(test_oauth_client),
        ):
            resp = TestClient(app).post(
                _AUTHORIZE,
                data={"client_id": "oauth_test_dev_536", "audience": "x", "resource": "y"},
            )

        assert resp.status_code == 200, resp.text

    @pytest.mark.parametrize("chunked", [False, True], ids=["content-length", "chunked"])
    def test_oversized_body_is_refused_before_it_is_parsed(self, chunked):
        body = b"client_id=oauth_test_dev_536&pad=" + b"x" * 8192
        # An iterator makes httpx send the body chunked, without Content-Length.
        content = iter([body[:4096], body[4096:]]) if chunked else body
        with patch("api.routes.oauth.get_sync_session") as mock_session_fn:
            resp = TestClient(app).post(
                _AUTHORIZE, content=content, headers={"Content-Type": _FORM}
            )

        _assert_rfc6749_error(resp, 413, "invalid_request")
        mock_session_fn.assert_not_called()

    def test_rate_limit_applies_to_form_requests(self):
        with (
            patch("api.routes.oauth.increment_counter", AsyncMock(return_value=11)),
            patch("api.routes.oauth.get_sync_session") as mock_session_fn,
        ):
            resp = TestClient(app).post(_AUTHORIZE, data={"client_id": "oauth_test_dev_536"})

        _assert_rfc6749_error(resp, 429, "invalid_request")
        assert resp.headers["retry-after"] == "60"
        mock_session_fn.assert_not_called()

    def test_form_and_json_requests_share_one_budget(self, test_oauth_client):
        increment, counts = _fake_counter()
        settings = _device_settings(oauth_device_authorize_rate_limit_per_minute=2)
        with (
            patch("api.routes.oauth.increment_counter", side_effect=increment),
            patch("api.routes.oauth.get_settings", return_value=settings),
            patch(
                "api.routes.oauth.get_sync_session",
                return_value=_authorize_session(test_oauth_client),
            ),
        ):
            client = TestClient(app, client=("198.51.100.3", 50000))
            payload = {"client_id": "oauth_test_dev_536"}
            statuses = [
                _post_authorize(client, "form", payload).status_code,
                _post_authorize(client, "json", payload).status_code,
                _post_authorize(client, "form", payload).status_code,
            ]

        assert statuses == [200, 200, 429]
        assert counts == {"device_authorize:198.51.100.3": 3}

    @pytest.mark.parametrize(
        "content_type,content",
        [("text/plain", b"client_id=x"), (_FORM, b"scope=memory%3Aread")],
        ids=["unsupported-content-type", "missing-client-id"],
    )
    def test_rejected_requests_are_counted_first(self, _under_rate_limit, content_type, content):
        """The body checks run after the limit, so they cannot be used to skip it."""
        resp = TestClient(app, client=("203.0.113.8", 50000)).post(
            _AUTHORIZE, content=content, headers={"Content-Type": content_type}
        )

        assert resp.status_code == 400
        _under_rate_limit.assert_awaited_once_with("device_authorize:203.0.113.8", ttl=60)

    def test_openapi_lists_both_request_encodings(self):
        operation = app.openapi()["paths"][_AUTHORIZE]["post"]
        request_body = operation["requestBody"]

        assert request_body["required"] is True
        assert set(request_body["content"]) == {_FORM, "application/json"}
        for media in request_body["content"].values():
            schema = media["schema"]
            assert schema["type"] == "object"
            assert schema["required"] == ["client_id"]
            assert set(schema["properties"]) == {"client_id", "scope"}
        assert {"400", "413", "429"} <= set(operation["responses"])
