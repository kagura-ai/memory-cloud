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


def _make_grant_with_request(client_id="kagura-cli"):
    """Grant with a mock request/client/GRANT_TYPE attached — for generate_token tests."""
    grant = _make_grant()
    grant.request = MagicMock()
    grant.request.client.client_id = client_id
    grant.GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
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


class TestDeviceAuthorizationGrantTokenGeneration:
    """Without ``generate_token`` Authlib's ``BaseGrant.generate_token`` falls
    through to ``server.generate_token`` (intentionally unset) and raises
    ``RuntimeError("No configured token generator")`` on every approved
    device_code poll.
    """

    def test_generate_token_returns_oauth2_token_dict(self):
        grant = _make_grant_with_request()
        token = grant.generate_token(scope="memory:read")

        assert token["token_type"] == "Bearer"
        assert "access_token" in token and len(token["access_token"]) > 0
        assert "refresh_token" in token and len(token["refresh_token"]) > 0
        assert token["scope"] == "memory:read"

    def test_generate_token_uses_class_token_expires_in_when_none(self):
        grant = _make_grant_with_request()
        token = grant.generate_token(scope="memory:read", expires_in=None)
        assert token["expires_in"] == DeviceAuthorizationGrant.TOKEN_EXPIRES_IN
        assert token["expires_in"] == 3600

    def test_generate_token_respects_explicit_expires_in(self):
        grant = _make_grant_with_request()
        token = grant.generate_token(scope="memory:read", expires_in=42)
        assert token["expires_in"] == 42

    def test_generate_token_uses_class_grant_type_when_none(self):
        # GRANT_TYPE fallback path: no exception means _generate_token_with_expiry
        # accepted the device_code grant_type from the class attribute.
        grant = _make_grant_with_request()
        token = grant.generate_token(scope="memory:read")
        assert token is not None
        assert "access_token" in token

    # ----- Identity injection (#640) -----
    # The next three tests pin the response-body identity contract that the SDK
    # (kagura-memory-python-sdk device_flow.py:382-385) reads. They are the
    # mirror of the user=None tests above for the user-populated branch.

    @staticmethod
    def _stub_identity_lookups(grant, user_row, workspace):
        """Wire ``server.db_session.query(...)`` to return distinct chains for
        ``User`` and ``Workspace`` lookups. ``Workspace`` uses a longer chain
        (``filter_by(...).filter(...).first()``) because the production code
        filters soft-deleted workspaces — keep this mirrored against
        ``oauth2_server.py`` if that chain ever changes shape.
        """
        from models.auth import User

        user_chain = MagicMock()
        user_chain.filter_by.return_value.first.return_value = user_row
        ws_chain = MagicMock()
        ws_chain.filter_by.return_value.filter.return_value.first.return_value = workspace
        grant.server.db_session.query.side_effect = lambda model: (
            user_chain if model is User else ws_chain
        )

    def test_generate_token_injects_identity_when_user_and_workspace_present(self):
        from uuid import uuid4

        grant = _make_grant_with_request()
        workspace_uuid = uuid4()
        user_row = MagicMock(email="alice@example.com", current_workspace_id=workspace_uuid)
        # ``MagicMock(name=...)`` sets the mock's repr name, not an attribute —
        # assign workspace.name after construction so production reads it as data.
        workspace = MagicMock(id=workspace_uuid)
        workspace.name = "alice-workspace"
        self._stub_identity_lookups(grant, user_row, workspace)

        user = MagicMock(user_id="oauth-sub-alice")
        token = grant.generate_token(user=user, scope="memory:read")

        assert token["user_email"] == "alice@example.com"
        assert token["workspace_id"] == str(workspace_uuid)
        assert token["workspace_name"] == "alice-workspace"

    def test_generate_token_skips_soft_deleted_workspace(self):
        # Regression guard for the soft-delete filter (added after Copilot
        # review loop 1). When the Workspace query returns None — which is
        # what the ``.filter(Workspace.deleted_at.is_(None))`` clause produces
        # for a soft-deleted row — the token must NOT carry stale identity.
        from uuid import uuid4

        grant = _make_grant_with_request()
        user_row = MagicMock(email="bob@example.com", current_workspace_id=uuid4())
        # workspace=None simulates the soft-delete filter excluding the row.
        self._stub_identity_lookups(grant, user_row, None)

        user = MagicMock(user_id="oauth-sub-bob")
        token = grant.generate_token(user=user, scope="memory:read")

        assert token["user_email"] == "bob@example.com"
        assert "workspace_id" not in token
        assert "workspace_name" not in token

    def test_generate_token_skips_workspace_when_user_has_no_current_workspace(self):
        grant = _make_grant_with_request()
        user_row = MagicMock(email="carol@example.com", current_workspace_id=None)
        # workspace stub is unused because user_row.current_workspace_id is
        # falsy and the production code short-circuits before querying.
        self._stub_identity_lookups(grant, user_row, workspace=None)

        user = MagicMock(user_id="oauth-sub-carol")
        token = grant.generate_token(user=user, scope="memory:read")

        assert token["user_email"] == "carol@example.com"
        assert "workspace_id" not in token
        assert "workspace_name" not in token
