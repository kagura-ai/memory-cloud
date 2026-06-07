"""Centralized OAuth provider endpoint resolution + production guard (Issue #937).

Production talks to the real Google/GitHub OAuth endpoints. The account-linking
E2E harness, however, needs the *server-side* token/userinfo exchange pointed at
a mock IdP — and because that exchange runs inside the backend (a browser-level
Playwright mock cannot intercept a server-to-server HTTP call), the endpoint URLs
must be injectable at the backend layer.

That injectability is security-sensitive: an attacker able to set these in
production could redirect token exchange to a host they control and harvest
codes/tokens. Two invariants keep the seam safe:

  1. **Inert by default.** Overrides are honored only when
     ``OAUTH_ENDPOINT_OVERRIDE_ENABLED`` is truthy *and* the environment is not
     production. Resolution in production always returns the real defaults.
  2. **Fail-closed boot guard.** :func:`assert_oauth_endpoints_safe`, called from
     the app lifespan, refuses to start the process if *any* override mechanism
     (the flag or a stray ``OAUTH_*_URL``) is active while
     ``ENVIRONMENT=production``.

All values are read live from ``os.environ`` on each call (the lookups are
trivial), so tests can ``monkeypatch.setenv`` without fighting a cached
``Settings`` singleton.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

from utils.logger import get_logger

logger = get_logger(__name__)

# --- Real provider defaults (the only URLs used unless overrides are active) ---
GOOGLE_AUTH_URL_DEFAULT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL_DEFAULT = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL_DEFAULT = "https://www.googleapis.com/oauth2/v3/userinfo"
GITHUB_AUTH_URL_DEFAULT = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL_DEFAULT = "https://github.com/login/oauth/access_token"
GITHUB_USER_URL_DEFAULT = "https://api.github.com/user"
GITHUB_EMAILS_URL_DEFAULT = "https://api.github.com/user/emails"

# Master switch — overrides are inert unless this is truthy.
OVERRIDE_FLAG_ENV = "OAUTH_ENDPOINT_OVERRIDE_ENABLED"

# Logical endpoint key -> (env var name, real default).
_ENDPOINTS: dict[str, tuple[str, str]] = {
    "google_auth_url": ("OAUTH_GOOGLE_AUTH_URL", GOOGLE_AUTH_URL_DEFAULT),
    "google_token_url": ("OAUTH_GOOGLE_TOKEN_URL", GOOGLE_TOKEN_URL_DEFAULT),
    "google_userinfo_url": ("OAUTH_GOOGLE_USERINFO_URL", GOOGLE_USERINFO_URL_DEFAULT),
    "github_auth_url": ("OAUTH_GITHUB_AUTH_URL", GITHUB_AUTH_URL_DEFAULT),
    "github_token_url": ("OAUTH_GITHUB_TOKEN_URL", GITHUB_TOKEN_URL_DEFAULT),
    "github_user_url": ("OAUTH_GITHUB_USER_URL", GITHUB_USER_URL_DEFAULT),
    "github_emails_url": ("OAUTH_GITHUB_EMAILS_URL", GITHUB_EMAILS_URL_DEFAULT),
}

_TRUTHY = {"1", "true", "yes", "on"}


def _is_production() -> bool:
    # Mirrors how pydantic-settings binds Settings.environment from ENVIRONMENT.
    return os.environ.get("ENVIRONMENT", "development").strip().lower() == "production"


def _overrides_enabled() -> bool:
    return os.environ.get(OVERRIDE_FLAG_ENV, "").strip().lower() in _TRUTHY


def _any_override_url_set() -> bool:
    return any(os.environ.get(env_name) for env_name, _ in _ENDPOINTS.values())


def _valid_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _resolve(key: str) -> str:
    """Resolve one logical endpoint, honoring an override only when it is safe."""
    env_name, default = _ENDPOINTS[key]

    # Production NEVER honors an override, regardless of the flag — defense in
    # depth behind the boot guard (which should already have refused to start).
    if _is_production() or not _overrides_enabled():
        return default

    raw = os.environ.get(env_name)
    if not raw:
        return default
    if not _valid_http_url(raw):
        logger.warning(
            "oauth_endpoint_override_invalid_scheme",
            endpoint=key,
            env=env_name,
        )
        return default
    return raw


def google_auth_url() -> str:
    return _resolve("google_auth_url")


def google_token_url() -> str:
    return _resolve("google_token_url")


def google_userinfo_url() -> str:
    return _resolve("google_userinfo_url")


def github_auth_url() -> str:
    return _resolve("github_auth_url")


def github_token_url() -> str:
    return _resolve("github_token_url")


def github_user_url() -> str:
    return _resolve("github_user_url")


def github_emails_url() -> str:
    return _resolve("github_emails_url")


def assert_oauth_endpoints_safe() -> None:
    """Fail-closed boot guard: refuse to start with active overrides in production.

    Called from the app lifespan. Raises :class:`RuntimeError` if the override
    flag is enabled or any ``OAUTH_*_URL`` is set while ``ENVIRONMENT=production``,
    so a misconfiguration that would redirect token exchange to a non-Google /
    non-GitHub host crashes the process loudly instead of silently shipping.
    """
    if not _is_production():
        return
    if _overrides_enabled() or _any_override_url_set():
        raise RuntimeError(
            "OAuth endpoint override is active in production "
            f"(ENVIRONMENT=production, {OVERRIDE_FLAG_ENV} or an OAUTH_*_URL is "
            "set). These overrides exist only for E2E testing and must never be "
            "set in production — refusing to start. Unset them and restart."
        )
