"""Tests for OAuth2DeviceCode model and generate_user_code utility (Issue #536)."""

import sys
from datetime import timedelta
from pathlib import Path

_BACKEND_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

import pytest  # noqa: E402, F401

from models.auth import OAuth2DeviceCode, generate_user_code  # noqa: E402
from utils.datetime import utcnow  # noqa: E402


class TestGenerateUserCode:
    def test_default_length_is_8(self):
        code = generate_user_code()
        assert len(code) == 8

    def test_custom_length(self):
        code = generate_user_code(length=12)
        assert len(code) == 12

    def test_only_uppercase_alphanumeric(self):
        for _ in range(100):
            code = generate_user_code()
            assert all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" for c in code)

    def test_codes_are_random(self):
        codes = {generate_user_code() for _ in range(20)}
        assert len(codes) > 1


class TestOAuth2DeviceCode:
    def test_get_client_id(self):
        device = OAuth2DeviceCode(client_id="oauth_test123")
        assert device.get_client_id() == "oauth_test123"

    def test_get_user_code(self):
        device = OAuth2DeviceCode(user_code="ABCD1234")
        assert device.get_user_code() == "ABCD1234"

    def test_get_scope(self):
        device = OAuth2DeviceCode(scope="memory:read memory:write")
        assert device.get_scope() == "memory:read memory:write"

    def test_get_scope_none(self):
        device = OAuth2DeviceCode(scope=None)
        assert device.get_scope() is None

    def test_is_expired_false_when_future(self):
        future = utcnow() + timedelta(seconds=600)
        device = OAuth2DeviceCode(expires_at=future)
        assert device.is_expired() is False

    def test_is_expired_true_when_past(self):
        past = utcnow() - timedelta(seconds=1)
        device = OAuth2DeviceCode(expires_at=past)
        assert device.is_expired() is True
