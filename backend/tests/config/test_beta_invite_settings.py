"""Closed-beta invite settings (#1581).

The feature grants account creation, so the defaults have to reproduce today's
behaviour exactly: off, and with a quota that an operator typo cannot turn
negative.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from config.settings import Settings

_ENV_KEYS = ["ENABLE_BETA_INVITES", "BETA_INVITE_QUOTA_PER_USER"]


@pytest.fixture
def clean_env(monkeypatch):
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def test_defaults_are_off_with_a_quota_of_four(clean_env):
    s = Settings(_env_file=None)
    assert s.enable_beta_invites is False
    assert s.beta_invite_quota_per_user == 4


def test_env_vars_are_read(clean_env):
    clean_env.setenv("ENABLE_BETA_INVITES", "true")
    clean_env.setenv("BETA_INVITE_QUOTA_PER_USER", "2")
    s = Settings(_env_file=None)
    assert s.enable_beta_invites is True
    assert s.beta_invite_quota_per_user == 2


def test_zero_quota_is_allowed(clean_env):
    """0 = only system admins can mint, without switching redemption off."""
    clean_env.setenv("BETA_INVITE_QUOTA_PER_USER", "0")
    assert Settings(_env_file=None).beta_invite_quota_per_user == 0


def test_negative_quota_is_refused_at_load(clean_env):
    clean_env.setenv("BETA_INVITE_QUOTA_PER_USER", "-1")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
