"""Config categories and SYSTEM_ADMIN_MODE (#2767)."""

from __future__ import annotations

import pytest

from api.routes.config import get_config_categories, get_sensitive_keys, get_visible_categories
from config.settings import get_settings

ADMIN = {"user_id": "1", "email": "admin@example.com", "role": "admin"}
USER = {"user_id": "2", "email": "user@example.com", "role": "user"}


@pytest.fixture
def mode(monkeypatch):
    def _set(value: str) -> None:
        monkeypatch.setattr(get_settings(), "system_admin_mode", value)

    return _set


@pytest.mark.parametrize(
    ("value", "listed"), [("session", True), ("off", False), ("service", False)]
)
def test_hosted_category_needs_an_effective_session_admin(mode, value, listed):
    mode(value)
    assert ("hosted" in get_visible_categories(ADMIN)) is listed
    assert "hosted" not in get_visible_categories(USER)


def test_no_category_lists_a_secret_unless_it_is_masked():
    # SYSTEM_ADMIN_SERVICE_TOKEN* (and any other credential) must never be
    # served by GET /api/v1/config unmasked.
    sensitive = get_sensitive_keys()
    leaking = [
        key
        for keys in get_config_categories().values()
        for key in keys
        if ("TOKEN" in key or "SECRET" in key) and key not in sensitive
    ]
    assert leaking == []
    listed = {key for keys in get_config_categories().values() for key in keys}
    assert not {key for key in listed if key.startswith("SYSTEM_ADMIN_SERVICE")}
