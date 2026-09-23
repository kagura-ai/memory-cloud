"""Device-flow abuse limits and expired-code retention settings (#1656)."""

import pytest
from pydantic import ValidationError

from config.settings import Settings

_ENV_NAMES = (
    "OAUTH_DEVICE_AUTHORIZE_RATE_LIMIT_PER_MINUTE",
    "OAUTH_DEVICE_VERIFY_RATE_LIMIT_PER_MINUTE",
    "OAUTH_DEVICE_CODE_RETENTION_SECONDS",
)


def test_defaults(monkeypatch):
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    s = Settings(_env_file=None)
    assert s.oauth_device_authorize_rate_limit_per_minute == 10
    assert s.oauth_device_verify_rate_limit_per_minute == 30
    assert s.oauth_device_code_retention_seconds == 3600


def test_operators_can_override(monkeypatch):
    monkeypatch.setenv("OAUTH_DEVICE_AUTHORIZE_RATE_LIMIT_PER_MINUTE", "3")
    monkeypatch.setenv("OAUTH_DEVICE_VERIFY_RATE_LIMIT_PER_MINUTE", "12")
    monkeypatch.setenv("OAUTH_DEVICE_CODE_RETENTION_SECONDS", "0")
    s = Settings(_env_file=None)
    assert s.oauth_device_authorize_rate_limit_per_minute == 3
    assert s.oauth_device_verify_rate_limit_per_minute == 12
    assert s.oauth_device_code_retention_seconds == 0


@pytest.mark.parametrize(
    "name",
    [
        "OAUTH_DEVICE_AUTHORIZE_RATE_LIMIT_PER_MINUTE",
        "OAUTH_DEVICE_VERIFY_RATE_LIMIT_PER_MINUTE",
    ],
)
@pytest.mark.parametrize("raw", ["0", "-1"])
def test_rate_limits_must_be_positive(monkeypatch, name, raw):
    monkeypatch.setenv(name, raw)
    with pytest.raises(ValidationError, match=name.lower()):
        Settings(_env_file=None)


def test_retention_cannot_be_negative(monkeypatch):
    monkeypatch.setenv("OAUTH_DEVICE_CODE_RETENTION_SECONDS", "-1")
    with pytest.raises(ValidationError, match="oauth_device_code_retention_seconds"):
        Settings(_env_file=None)
