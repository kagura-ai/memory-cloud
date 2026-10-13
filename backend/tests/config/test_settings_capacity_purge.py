"""#1969: both purge stages are off by default and read from the environment."""

from __future__ import annotations

import pytest

from config.settings import Settings

_ENV_KEYS = ["CAPACITY_PURGE_NOTICE_ENABLED", "CAPACITY_PURGE_ENABLED"]


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def test_capacity_purge_flags_default_off(clean_env: pytest.MonkeyPatch) -> None:
    s = Settings(_env_file=None)
    assert s.capacity_purge_notice_enabled is False
    assert s.capacity_purge_enabled is False


def test_capacity_purge_flags_read_env(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("CAPACITY_PURGE_NOTICE_ENABLED", "true")
    clean_env.setenv("CAPACITY_PURGE_ENABLED", "true")
    s = Settings(_env_file=None)
    assert s.capacity_purge_notice_enabled is True
    assert s.capacity_purge_enabled is True
