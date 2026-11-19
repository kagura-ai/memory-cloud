"""The edge certificate reporter's credential (#2941), validated like a service credential.

``MCP_PUBLIC_DOMAINS_TLS_REPORTER_TOKEN`` is empty by default (reports are then
refused and none is shown). A value with blanks could never be presented as
the bearer, so it refuses start-up everywhere. Under ENVIRONMENT=production a
set value must be private, at least 32 characters, and its own: not the
system-admin (current or previous), billing or worker service credential. A
refusal names the variable, never its value.
"""

import secrets

import pytest
from pydantic import ValidationError

from config.settings import Settings

_ENVS = (
    "MCP_PUBLIC_DOMAINS_TLS_REPORTER_TOKEN",
    "SYSTEM_ADMIN_MODE",
    "SYSTEM_ADMIN_SERVICE_TOKEN",
    "SYSTEM_ADMIN_SERVICE_TOKEN_PREVIOUS",
    "BILLING_SERVICE_TOKEN",
    "WORKER_SERVICE_TOKEN",
    "ENVIRONMENT",
)
VARIABLE = "MCP_PUBLIC_DOMAINS_TLS_REPORTER_TOKEN"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for env in _ENVS:
        monkeypatch.delenv(env, raising=False)


def _production(**overrides: str) -> Settings:
    values = {
        "environment": "production",
        "api_key_secret": secrets.token_hex(32),
        "audit_hmac_key": secrets.token_hex(32),
        "audit_pseudo_salt": secrets.token_hex(16),
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_the_credential_is_empty_by_default_and_never_in_the_repr():
    assert Settings(_env_file=None).mcp_public_domains_tls_reporter_token == ""
    token = secrets.token_hex(32)
    settings = Settings(_env_file=None, mcp_public_domains_tls_reporter_token=token)
    assert token not in repr(settings)


@pytest.mark.parametrize(
    "blank", [" ", "   ", f" {'a' * 40}", f"{'a' * 40}\n", "a" * 20 + " " + "a" * 20]
)
def test_a_credential_with_blanks_refuses_to_start_everywhere(blank):
    with pytest.raises(ValidationError, match=VARIABLE) as info:
        Settings(_env_file=None, mcp_public_domains_tls_reporter_token=blank)
    assert "a" * 20 not in str(info.value)


def test_production_accepts_a_strong_dedicated_credential_and_an_unset_one():
    assert _production().mcp_public_domains_tls_reporter_token == ""
    token = secrets.token_hex(32)
    assert (
        _production(
            mcp_public_domains_tls_reporter_token=token
        ).mcp_public_domains_tls_reporter_token
        == token
    )


@pytest.mark.parametrize(
    "weak", ["changeme", "change-me-tls-reporter", "dev-secret-x" * 4, "a" * 31]
)
def test_production_refuses_a_weak_credential(weak):
    with pytest.raises(ValidationError, match=VARIABLE) as info:
        _production(mcp_public_domains_tls_reporter_token=weak)
    assert weak not in str(info.value)


def test_a_short_credential_is_allowed_outside_production():
    settings = Settings(_env_file=None, mcp_public_domains_tls_reporter_token="short")
    assert settings.mcp_public_domains_tls_reporter_token == "short"


@pytest.mark.parametrize(
    "other",
    [
        "system_admin_service_token",
        "system_admin_service_token_previous",
        "billing_service_token",
        "worker_service_token",
    ],
)
def test_production_refuses_a_credential_shared_with_another_service(other):
    shared = secrets.token_hex(32)
    fields = {"mcp_public_domains_tls_reporter_token": shared, other: shared}
    if other == "system_admin_service_token_previous":
        fields["system_admin_service_token"] = secrets.token_hex(32)
    with pytest.raises(ValidationError, match=VARIABLE) as info:
        _production(**fields)
    assert shared not in str(info.value)


def test_production_refuses_one_of_several_previous_system_admin_credentials():
    shared = secrets.token_hex(32)
    with pytest.raises(ValidationError, match=VARIABLE):
        _production(
            mcp_public_domains_tls_reporter_token=shared,
            system_admin_service_token=secrets.token_hex(32),
            system_admin_service_token_previous=f"{secrets.token_hex(32)}, {shared}",
        )
