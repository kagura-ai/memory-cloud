"""SYSTEM_ADMIN_MODE and its service settings (#2767)."""

import pytest
from pydantic import ValidationError

from config.settings import Settings


def test_mode_defaults_to_session(monkeypatch):
    monkeypatch.delenv("SYSTEM_ADMIN_MODE", raising=False)
    assert Settings(_env_file=None).system_admin_mode == "session"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("session", "session"),
        ("off", "off"),
        ("service", "service"),
        (" off\n", "off"),
        ("\tservice ", "service"),
    ],
)
def test_known_modes_are_accepted_after_trimming(monkeypatch, raw, expected):
    monkeypatch.setenv("SYSTEM_ADMIN_MODE", raw)
    assert Settings(_env_file=None).system_admin_mode == expected


@pytest.mark.parametrize("raw", ["", "   ", "Off", "SESSION", "none", "disabled", "true"])
def test_empty_unknown_or_miscased_mode_refuses_to_start(monkeypatch, raw):
    # Empty must not quietly mean "session": a deployment that writes
    # SYSTEM_ADMIN_MODE= meant to set something.
    monkeypatch.setenv("SYSTEM_ADMIN_MODE", raw)
    with pytest.raises(ValidationError, match="system_admin_mode"):
        Settings(_env_file=None)
