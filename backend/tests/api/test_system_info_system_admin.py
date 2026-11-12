"""GET /api/v1/system/info exposes features.system_admin (#2767)."""

from __future__ import annotations

import pytest

from tests.api.test_system_info import info_with  # noqa: F401 - fixture


@pytest.mark.parametrize(
    ("value", "expected"), [("session", True), ("off", False), ("service", False)]
)
def test_system_admin_flag_follows_the_mode(info_with, value, expected):  # noqa: F811
    assert info_with(system_admin_mode=value).json()["features"]["system_admin"] is expected


def test_system_admin_flag_defaults_on(info_with):  # noqa: F811
    assert info_with().json()["features"]["system_admin"] is True
