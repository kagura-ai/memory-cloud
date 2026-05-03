"""Tests for DeviceAuthorizationGrant (RFC 8628 grant implementation, Issue #536)."""

import sys
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock

_BACKEND_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

import pytest  # noqa: E402, F401

from auth.oauth2_server import (  # noqa: E402
    DeviceAuthorizationGrant,
    OAuth2AuthorizationServer,
)
from models.auth import OAuth2DeviceCode  # noqa: E402
from utils.datetime import utcnow  # noqa: E402


def _make_device(**overrides):
    kwargs = {
        "device_code": "test-device-code-abc123",
        "user_code": "ABCD1234",
        "client_id": "oauth_test123",
        "user_id": None,
        "scope": "memory:read",
        "expires_at": utcnow() + timedelta(seconds=600),
        "last_polled_at": None,
        "denied_at": None,
        "authorized_at": None,
    }
    kwargs.update(overrides)
    return OAuth2DeviceCode(**kwargs)


def _make_grant():
    grant = DeviceAuthorizationGrant.__new__(DeviceAuthorizationGrant)
    grant.server = MagicMock()
    grant.server.db_session = MagicMock()
    return grant


class TestDeviceAuthorizationGrant:
    def test_query_device_credential_found(self):
        grant = _make_grant()
        device = _make_device()
        grant.server.db_session.query().filter_by().first.return_value = device

        result = grant.query_device_credential("test-device-code-abc123")
        assert result is device

    def test_query_device_credential_not_found(self):
        grant = _make_grant()
        grant.server.db_session.query().filter_by().first.return_value = None

        result = grant.query_device_credential("nonexistent")
        assert result is None

    def test_query_user_grant_authorized(self):
        grant = _make_grant()
        device = _make_device(
            authorized_at=utcnow() - timedelta(seconds=30),
            user_id="test_user_123",
        )
        grant.server.db_session.query().filter_by().first.return_value = device

        user, approved = grant.query_user_grant("ABCD1234")
        assert user.get_user_id() == "test_user_123"
        assert approved is True

    def test_query_user_grant_denied(self):
        grant = _make_grant()
        device = _make_device(denied_at=utcnow() - timedelta(seconds=30))
        grant.server.db_session.query().filter_by().first.return_value = device

        _user, approved = grant.query_user_grant("ABCD1234")
        assert approved is False
        # _user is a _DeniedUser stub created by the grant for Authlib

    def test_query_user_grant_pending(self):
        grant = _make_grant()
        device = _make_device()
        grant.server.db_session.query().filter_by().first.return_value = device

        result = grant.query_user_grant("ABCD1234")
        assert result is None

    def test_query_user_grant_not_found(self):
        grant = _make_grant()
        grant.server.db_session.query().filter_by().first.return_value = None

        result = grant.query_user_grant("NONEXIST")
        assert result is None

    def test_should_slow_down_first_poll(self):
        grant = _make_grant()
        device = _make_device()

        result = grant.should_slow_down(device)
        assert result is False
        assert device.last_polled_at is not None

    def test_should_slow_down_rapid_poll(self):
        grant = _make_grant()
        device = _make_device(last_polled_at=utcnow() - timedelta(seconds=2))

        result = grant.should_slow_down(device)
        assert result is True

    def test_should_slow_down_after_interval(self):
        grant = _make_grant()
        device = _make_device(last_polled_at=utcnow() - timedelta(seconds=6))

        result = grant.should_slow_down(device)
        assert result is False

    def test_token_endpoint_auth_methods(self):
        assert "none" in DeviceAuthorizationGrant.TOKEN_ENDPOINT_AUTH_METHODS
        assert "client_secret_post" in DeviceAuthorizationGrant.TOKEN_ENDPOINT_AUTH_METHODS
        assert "client_secret_basic" in DeviceAuthorizationGrant.TOKEN_ENDPOINT_AUTH_METHODS


class TestDeviceGrantRegistration:
    def test_device_grant_registered_in_server(self):
        """Verify DeviceAuthorizationGrant is registered in _register_grants."""
        wrapper = OAuth2AuthorizationServer.__new__(OAuth2AuthorizationServer)
        wrapper.session = MagicMock()
        wrapper.server = MagicMock()

        wrapper._register_grants()

        registered_classes = [call.args[0] for call in wrapper.server.register_grant.call_args_list]
        assert DeviceAuthorizationGrant in registered_classes
