"""Google connected apps settings (#2773): exact redirect URIs, verification tokens."""

import pytest
from pydantic import ValidationError

from config.settings import Settings

REDIRECT_ENV = "GOOGLE_CONNECTED_APPS_REDIRECT_URIS"
TOKENS_ENV = "GOOGLE_CONNECTED_APPS_VERIFICATION_TOKENS"
APP_URI = "https://oauth-redirect.googleusercontent.com/r/example-app"
SANDBOX_URI = "https://oauth-redirect-sandbox.googleusercontent.com/r/example-app"
TEST_URI = "https://oauth-redirect-test.googleusercontent.com/a/example-app/cb"


def test_defaults_are_empty(monkeypatch):
    monkeypatch.delenv(REDIRECT_ENV, raising=False)
    monkeypatch.delenv(TOKENS_ENV, raising=False)
    s = Settings(_env_file=None)
    assert s.google_connected_apps_redirect_uri_list == ()
    assert s.google_connected_apps_verification_token_list == ()


def test_redirect_uris_are_parsed_in_order(monkeypatch):
    monkeypatch.setenv(REDIRECT_ENV, f" {APP_URI} ,{SANDBOX_URI},, {TEST_URI}")
    s = Settings(_env_file=None)
    assert s.google_connected_apps_redirect_uri_list == (APP_URI, SANDBOX_URI, TEST_URI)


@pytest.mark.parametrize(
    "raw",
    [
        # Wildcards and prefixes: never a pattern.
        "https://oauth-redirect.googleusercontent.com/r/*",
        "https://oauth-redirect.googleusercontent.com/a/*",
        "https://oauth-redirect.googleusercontent.com/r/",
        "https://oauth-redirect.googleusercontent.com/r",
        "https://oauth-redirect.googleusercontent.com/A",
        "https://oauth-redirect.googleusercontent.com/r/app/",
        "https://oauth-redirect.googleusercontent.com/",
        "https://oauth-redirect.googleusercontent.com",
        "https://oauth-redirect.googleusercontent.com//app",
        # Scheme.
        "http://oauth-redirect.googleusercontent.com/r/app",
        "HTTPS://oauth-redirect.googleusercontent.com/r/app",
        # Host, written exactly: no other host, userinfo, port, case, trailing dot.
        "https://example.com/r/app",
        "https://evil.oauth-redirect.googleusercontent.com/r/app",
        "https://googleusercontent.com/r/app",
        "https://user@oauth-redirect.googleusercontent.com/r/app",
        "https://oauth-redirect.googleusercontent.com:443/r/app",
        "https://OAUTH-REDIRECT.googleusercontent.com/r/app",
        "https://oauth-redirect.googleusercontent.com./r/app",
        # Query, fragment, params, whitespace.
        "https://oauth-redirect.googleusercontent.com/r/app?x=1",
        "https://oauth-redirect.googleusercontent.com/r/app?",
        "https://oauth-redirect.googleusercontent.com/r/app#x",
        "https://oauth-redirect.googleusercontent.com/r/app;x",
        "https://oauth-redirect.googleusercontent.com/r/a pp",
        # Dot segments, raw or encoded.
        "https://oauth-redirect.googleusercontent.com/r/../a/app",
        "https://oauth-redirect.googleusercontent.com/r/%2E%2E/app",
        # The gemini provider's binding namespace (#2222), as written or encoded.
        "https://oauth-redirect.googleusercontent.com/r/user_bound_custom-mcp-abc",
        "https://oauth-redirect.googleusercontent.com/r/user_bound_custom%2Dmcp-abc",
        "https://oauth-redirect.googleusercontent.com/R/USER_BOUND_CUSTOM-MCP-abc",
        # Listed twice.
        f"{APP_URI},{APP_URI}",
    ],
)
def test_bad_redirect_uris_refuse_startup(monkeypatch, raw):
    monkeypatch.setenv(REDIRECT_ENV, raw)
    with pytest.raises(ValidationError, match="google_connected_apps_redirect_uris"):
        Settings(_env_file=None)


def test_error_names_the_entry_by_position_not_value(monkeypatch):
    secretish = "https://example.com/r/some-app-value"
    monkeypatch.setenv(REDIRECT_ENV, f"{APP_URI},{secretish}")
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)
    assert "entry 2" in str(excinfo.value)
    assert "some-app-value" not in str(excinfo.value)


def test_verification_tokens_are_parsed(monkeypatch):
    monkeypatch.setenv(TOKENS_ENV, " token-one , token-two,")
    s = Settings(_env_file=None)
    assert s.google_connected_apps_verification_token_list == ("token-one", "token-two")


@pytest.mark.parametrize("raw", ["token one", "tok\x07en", "dup,dup"])
def test_bad_verification_tokens_refuse_startup(monkeypatch, raw):
    monkeypatch.setenv(TOKENS_ENV, raw)
    with pytest.raises(ValidationError, match="google_connected_apps_verification_tokens"):
        Settings(_env_file=None)
