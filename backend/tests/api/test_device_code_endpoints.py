"""Integration tests for Device Authorization Grant endpoints (Issue #536).

Tests the three RFC 8628 endpoints:
- POST /api/v1/oauth/device/authorize
- POST /api/v1/oauth/device/verify
- POST /api/v1/oauth/device/confirm
"""

import sys
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

_BACKEND_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from api.main import app  # noqa: E402
from models.auth import OAuth2Client, OAuth2DeviceCode  # noqa: E402
from utils.datetime import utcnow  # noqa: E402


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
                mock_settings.return_value = MagicMock(
                    oauth_device_code_expires_in=600,
                    oauth_device_polling_interval=5,
                    frontend_url="https://memory.kagura-ai.com",
                )

                client = TestClient(app)
                resp = client.post(
                    "/api/v1/oauth/device/authorize",
                    json={"client_id": "oauth_test_dev_536", "scope": "memory:read"},
                )

            assert resp.status_code == 200
            data = resp.json()
            assert "device_code" in data
            assert len(data["user_code"]) == 8
            assert data["verification_uri"] == "https://memory.kagura-ai.com/device"
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

        assert resp.status_code == 400
        assert "Unknown client_id" in resp.json()["detail"]

    def test_authorize_scope_intersection(self, test_oauth_client):
        with patch("api.routes.oauth.get_sync_session") as mock_session_fn:
            mock_db = MagicMock()
            mock_db.query().filter_by().first.return_value = test_oauth_client
            mock_db.commit.return_value = None
            mock_db.close.return_value = None
            mock_session_fn.return_value = mock_db

            with patch("api.routes.oauth.get_settings") as mock_settings:
                mock_settings.return_value = MagicMock(
                    oauth_device_code_expires_in=600,
                    oauth_device_polling_interval=5,
                    frontend_url="https://memory.kagura-ai.com",
                )

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
        assert "expired" in resp.json()["detail"].lower()

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
