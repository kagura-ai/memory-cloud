"""Unit tests for centralized OAuth endpoint resolution + production guard (#937).

The E2E test-IdP harness needs the *server-side* Google/GitHub token+userinfo
exchange pointed at a mock IdP. Because that exchange runs in the backend (a
browser-level Playwright mock cannot intercept it), the endpoint URLs must be
injectable — but injection is security-sensitive: an attacker who could set
these in production would redirect token exchange to a host they control.

These tests pin the two invariants that make the seam safe:
  1. Overrides are inert unless OAUTH_ENDPOINT_OVERRIDE_ENABLED is truthy.
  2. assert_oauth_endpoints_safe() refuses to boot if any override mechanism is
     active while ENVIRONMENT=production.
"""

from __future__ import annotations

import pytest

from auth import oauth_endpoints as oe

# Every override env var the module recognizes — cleared before each test so a
# stray value from the ambient environment can't leak in.
_ALL_OVERRIDE_ENV = [
    oe.OVERRIDE_FLAG_ENV,
    "OAUTH_GOOGLE_AUTH_URL",
    "OAUTH_GOOGLE_TOKEN_URL",
    "OAUTH_GOOGLE_USERINFO_URL",
    "OAUTH_GITHUB_AUTH_URL",
    "OAUTH_GITHUB_TOKEN_URL",
    "OAUTH_GITHUB_USER_URL",
    "OAUTH_GITHUB_EMAILS_URL",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start each test from a known-clean override environment."""
    for name in _ALL_OVERRIDE_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ENVIRONMENT", "development")


def test_defaults_returned_when_no_override() -> None:
    assert oe.google_auth_url() == oe.GOOGLE_AUTH_URL_DEFAULT
    assert oe.google_token_url() == oe.GOOGLE_TOKEN_URL_DEFAULT
    assert oe.google_userinfo_url() == oe.GOOGLE_USERINFO_URL_DEFAULT
    assert oe.github_auth_url() == oe.GITHUB_AUTH_URL_DEFAULT
    assert oe.github_token_url() == oe.GITHUB_TOKEN_URL_DEFAULT
    assert oe.github_user_url() == oe.GITHUB_USER_URL_DEFAULT
    assert oe.github_emails_url() == oe.GITHUB_EMAILS_URL_DEFAULT


def test_override_ignored_when_flag_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    # URL set but the master flag is NOT enabled → must be inert.
    monkeypatch.setenv("OAUTH_GITHUB_TOKEN_URL", "http://localhost:9999/token")
    assert oe.github_token_url() == oe.GITHUB_TOKEN_URL_DEFAULT


def test_override_honored_when_enabled_and_not_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(oe.OVERRIDE_FLAG_ENV, "true")
    monkeypatch.setenv("OAUTH_GITHUB_TOKEN_URL", "http://localhost:9999/token")
    monkeypatch.setenv("OAUTH_GOOGLE_USERINFO_URL", "http://localhost:9999/userinfo")
    assert oe.github_token_url() == "http://localhost:9999/token"
    assert oe.google_userinfo_url() == "http://localhost:9999/userinfo"
    # Endpoints without an explicit override still fall back to their defaults.
    assert oe.github_user_url() == oe.GITHUB_USER_URL_DEFAULT


def test_override_with_invalid_scheme_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(oe.OVERRIDE_FLAG_ENV, "true")
    monkeypatch.setenv("OAUTH_GITHUB_TOKEN_URL", "ftp://evil/x")
    assert oe.github_token_url() == oe.GITHUB_TOKEN_URL_DEFAULT


def test_guard_passes_in_production_with_no_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    # Must not raise.
    oe.assert_oauth_endpoints_safe()


def test_guard_passes_in_development_with_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(oe.OVERRIDE_FLAG_ENV, "true")
    monkeypatch.setenv("OAUTH_GITHUB_TOKEN_URL", "http://localhost:9999/token")
    # development → safe.
    oe.assert_oauth_endpoints_safe()


def test_guard_raises_in_production_when_flag_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv(oe.OVERRIDE_FLAG_ENV, "true")
    with pytest.raises(RuntimeError, match="OAuth endpoint override"):
        oe.assert_oauth_endpoints_safe()


def test_guard_raises_in_production_when_override_url_present_even_without_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Defense in depth: a stray override URL in prod blocks boot even if the
    # master flag was never set.
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("OAUTH_GOOGLE_TOKEN_URL", "http://attacker/token")
    with pytest.raises(RuntimeError, match="OAuth endpoint override"):
        oe.assert_oauth_endpoints_safe()


def test_production_resolution_ignores_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    # Even if the guard were somehow bypassed, resolution itself must never
    # honor an override in production.
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv(oe.OVERRIDE_FLAG_ENV, "true")
    monkeypatch.setenv("OAUTH_GITHUB_TOKEN_URL", "http://attacker/token")
    assert oe.github_token_url() == oe.GITHUB_TOKEN_URL_DEFAULT


# --- Wiring: the route layer must resolve URLs through the seam, not bake them in ---


def test_github_authorization_url_consumes_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """build_github_authorization_url must point at the mock IdP when overridden."""
    from api.routes.auth import build_github_authorization_url

    monkeypatch.setenv(oe.OVERRIDE_FLAG_ENV, "true")
    monkeypatch.setenv("OAUTH_GITHUB_AUTH_URL", "http://localhost:9999/login/oauth/authorize")

    url = build_github_authorization_url(
        client_id="cid", redirect_uri="http://localhost:8080/cb", state="s"
    )
    assert url.startswith("http://localhost:9999/login/oauth/authorize?")


def test_google_authorization_url_consumes_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """OAuth2Manager.get_authorization_url_web must resolve the auth URL live."""
    from auth.oauth2 import OAuth2Manager

    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv(oe.OVERRIDE_FLAG_ENV, "true")
    monkeypatch.setenv("OAUTH_GOOGLE_AUTH_URL", "http://localhost:9999/o/oauth2/v2/auth")

    url = OAuth2Manager(provider="google").get_authorization_url_web(
        redirect_uri="http://localhost:8080/cb", state="s"
    )
    assert url.startswith("http://localhost:9999/o/oauth2/v2/auth?")
