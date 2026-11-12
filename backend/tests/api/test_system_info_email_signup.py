"""``features.email_signup`` / ``features.open_registration`` on GET /system/info (#1734)."""

from __future__ import annotations

from tests.api.test_system_info import info_with  # noqa: F401  (fixture)


def test_email_signup_and_open_registration_default_off(info_with) -> None:  # noqa: F811
    features = info_with(allow_registration=False, enable_email_signup=False).json()["features"]
    assert features["email_signup"] is False
    assert features["open_registration"] is False


def test_flags_follow_the_settings(info_with) -> None:  # noqa: F811
    features = info_with(allow_registration=True, enable_email_signup=True).json()["features"]
    assert features["email_signup"] is True
    assert features["open_registration"] is True
