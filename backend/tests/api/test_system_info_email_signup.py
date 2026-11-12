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


_RESEND = {
    "email_provider": "resend",
    "resend_api_key": "re_test",
    "resend_from_email": "noreply@example.com",
    "resend_dpa_accepted_at": "2026-01-01T00:00:00Z",
}


def test_provider_link_confirmation_needs_the_switch_and_a_mail_backend(info_with) -> None:  # noqa: F811
    """#2786: per deployment; false while mail goes to the log."""
    on = info_with(enable_provider_link_confirmation=True, email_provider="logging")
    assert on.json()["features"]["provider_link_confirmation"] is False
    resend = info_with(enable_provider_link_confirmation=True, **_RESEND)
    assert resend.json()["features"]["provider_link_confirmation"] is True
    off = info_with(enable_provider_link_confirmation=False, **_RESEND)
    assert off.json()["features"]["provider_link_confirmation"] is False
