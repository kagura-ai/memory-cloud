"""ENVIRONMENT=production refuses development-default secrets (#2225).

The four server secrets ship with public placeholder values: the ``Settings``
defaults, and a different ``change-me-...`` family in ``.env.example`` /
``terraform/single-server/.env.prod.example``. Under production every one of
them is refused at ``Settings()`` construction, which runs at import time of
the API, alembic and the CLIs. The error names the variables, never the value.
"""

import secrets

import pytest
from pydantic import ValidationError

from config.settings import Settings

_FIELDS: dict[str, str] = {
    "api_key_secret": "API_KEY_SECRET",
    "jwt_secret": "JWT_SECRET",
    "audit_hmac_key": "AUDIT_HMAC_KEY",
    "audit_pseudo_salt": "AUDIT_PSEUDO_SALT",
}

# Each field × each placeholder family that ships in the repo.
_PLACEHOLDERS: dict[str, tuple[str, ...]] = {
    "api_key_secret": (
        "dev-secret-change-in-production",  # settings.py default
        "change-me-api-key-secret",  # .env.example / .env.prod.example / setup_env
    ),
    "jwt_secret": (
        "dev-secret-change-in-production",
        "change-me-jwt-secret",
    ),
    "audit_hmac_key": (
        "dev-audit-hmac-change-in-production",
        "change-me-audit-hmac-key",
    ),
    "audit_pseudo_salt": (
        "kagura-erasure-v1",  # settings.py default and .env.example
        "change-me-audit-pseudo-salt",  # .env.prod.example
    ),
}

_CASES = [(field, value) for field, values in _PLACEHOLDERS.items() for value in values]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    # A developer shell that sourced .env.local must not decide the outcome.
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    for env in _FIELDS.values():
        monkeypatch.delenv(env, raising=False)


def _private_secrets() -> dict[str, str]:
    return {field: secrets.token_hex(32) for field in _FIELDS}


@pytest.mark.parametrize(("field", "placeholder"), _CASES)
def test_production_refuses_each_shipped_placeholder(field, placeholder):
    values = _private_secrets() | {field: placeholder}
    with pytest.raises(ValidationError, match=_FIELDS[field]) as exc:
        Settings(_env_file=None, environment="production", **values)
    # Never echo the value: only the variable name reaches the log.
    assert placeholder not in str(exc.value)
    # Only the offending variable is named.
    for other_field, other_env in _FIELDS.items():
        if other_field != field:
            assert other_env not in str(exc.value)


@pytest.mark.parametrize("field", list(_FIELDS))
@pytest.mark.parametrize(
    "placeholder", ["", "   ", "change-me", "change-me-please-1234567890abcdef"]
)
def test_production_refuses_empty_and_change_me_prefixed_values(field, placeholder):
    values = _private_secrets() | {field: placeholder}
    with pytest.raises(ValidationError, match=_FIELDS[field]):
        Settings(_env_file=None, environment="production", **values)


def test_production_names_every_refused_variable_in_one_error():
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None, environment="production")
    message = str(exc.value)
    for env in _FIELDS.values():
        assert env in message
    assert "openssl rand -hex 32" in message
    for values in _PLACEHOLDERS.values():
        for value in values:
            assert value not in message


@pytest.mark.parametrize("field", ["api_key_secret", "jwt_secret", "audit_hmac_key"])
def test_production_refuses_a_key_shorter_than_32_characters(field):
    short = "x" * 31
    values = _private_secrets() | {field: short}
    with pytest.raises(ValidationError, match=_FIELDS[field]) as exc:
        Settings(_env_file=None, environment="production", **values)
    assert short not in str(exc.value)


@pytest.mark.parametrize("field", ["api_key_secret", "jwt_secret", "audit_hmac_key"])
def test_production_accepts_a_key_of_exactly_32_characters(field):
    values = _private_secrets() | {field: secrets.token_hex(16)}
    assert len(values[field]) == 32
    Settings(_env_file=None, environment="production", **values)


def test_production_accepts_a_short_private_salt():
    # Deliberate (#2225 gate1): a length rule on the salt would force a
    # rotation that breaks cross-row correlation of pseudonymized rows.
    values = _private_secrets() | {"audit_pseudo_salt": "s" * 31}
    settings = Settings(_env_file=None, environment="production", **values)
    assert settings.audit_pseudo_salt == "s" * 31


@pytest.mark.parametrize("raw", [" Production ", "PRODUCTION", "production\n"])
def test_environment_is_normalized_before_the_check(raw):
    with pytest.raises(ValidationError, match="API_KEY_SECRET"):
        Settings(_env_file=None, environment=raw)


def test_environment_from_the_process_env_is_checked(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("AUDIT_HMAC_KEY", "change-me-audit-hmac-key")
    values = {k: v for k, v in _private_secrets().items() if k != "audit_hmac_key"}
    with pytest.raises(ValidationError, match="AUDIT_HMAC_KEY"):
        Settings(_env_file=None, **values)


def test_production_boots_with_private_secrets():
    values = _private_secrets()
    settings = Settings(_env_file=None, environment="production", **values)
    assert settings.environment == "production"
    assert settings.api_key_secret == values["api_key_secret"]


def test_unset_environment_keeps_the_development_defaults():
    settings = Settings(_env_file=None)
    assert settings.environment == "development"
    assert settings.api_key_secret == "dev-secret-change-in-production"
    assert settings.audit_pseudo_salt == "kagura-erasure-v1"


@pytest.mark.parametrize("environment", ["development", "test", "staging"])
def test_non_production_environments_keep_the_defaults(environment):
    settings = Settings(_env_file=None, environment=environment)
    assert settings.audit_hmac_key == "dev-audit-hmac-change-in-production"
