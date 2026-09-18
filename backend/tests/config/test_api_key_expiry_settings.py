"""API_KEY_DEFAULT_EXPIRES_DAYS / API_KEY_AGENT_DEFAULT_EXPIRES_DAYS (#1537).

Operators may tighten the defaults but never reopen "never expires" as a
default — the lower bound is 1 day, matching the request schemas' range.
"""

import pytest
from pydantic import ValidationError

from config.settings import Settings


def test_defaults_are_365_and_90_days(monkeypatch):
    monkeypatch.delenv("API_KEY_DEFAULT_EXPIRES_DAYS", raising=False)
    monkeypatch.delenv("API_KEY_AGENT_DEFAULT_EXPIRES_DAYS", raising=False)
    s = Settings(_env_file=None)
    assert s.api_key_default_expires_days == 365
    assert s.api_key_agent_default_expires_days == 90


def test_operators_can_tighten(monkeypatch):
    monkeypatch.setenv("API_KEY_DEFAULT_EXPIRES_DAYS", "30")
    monkeypatch.setenv("API_KEY_AGENT_DEFAULT_EXPIRES_DAYS", "7")
    s = Settings(_env_file=None)
    assert s.api_key_default_expires_days == 30
    assert s.api_key_agent_default_expires_days == 7


@pytest.mark.parametrize(
    "name", ["API_KEY_DEFAULT_EXPIRES_DAYS", "API_KEY_AGENT_DEFAULT_EXPIRES_DAYS"]
)
@pytest.mark.parametrize("raw", ["0", "-1", "3651"])
def test_never_expires_and_out_of_range_are_not_valid_defaults(monkeypatch, name, raw):
    monkeypatch.setenv(name, raw)
    with pytest.raises(ValidationError, match=name.lower()):
        Settings(_env_file=None)
