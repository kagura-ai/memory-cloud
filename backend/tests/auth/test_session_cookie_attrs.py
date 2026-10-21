"""``Secure`` on browser cookies follows the normalized ENVIRONMENT (#2239).

``browser_cookie_attrs`` reads ``Settings.is_production``, the same check the
production secret guard uses, so ``Production`` or `` production `` cannot
pass the guard as production and still ship session cookies without
``Secure``.
"""

from __future__ import annotations

import pytest

from auth.session import browser_cookie_attrs
from config.settings import get_settings


@pytest.mark.parametrize(
    "environment", ["production", "Production ", " PRODUCTION", "production\n"]
)
def test_secure_in_production_whatever_the_spelling(monkeypatch, environment) -> None:
    monkeypatch.setattr(get_settings(), "environment", environment)
    assert browser_cookie_attrs()["secure"] is True


@pytest.mark.parametrize("environment", ["development", "staging", "test", ""])
def test_not_secure_outside_production(monkeypatch, environment) -> None:
    monkeypatch.setattr(get_settings(), "environment", environment)
    assert browser_cookie_attrs()["secure"] is False


def test_the_process_env_alone_does_not_decide(monkeypatch) -> None:
    # The cached settings are the source; a later env change does not flip it.
    monkeypatch.setattr(get_settings(), "environment", "development")
    monkeypatch.setenv("ENVIRONMENT", "production")
    assert browser_cookie_attrs()["secure"] is False


def test_shared_attributes() -> None:
    attrs = browser_cookie_attrs()
    assert attrs["httponly"] is True
    assert attrs["samesite"] == "lax"
    assert attrs["path"] == "/"
