"""SYSTEM_ADMIN_MODE and its service settings (#2767)."""

import secrets

import pytest
from pydantic import ValidationError

from config.settings import Settings


def test_mode_defaults_to_session(monkeypatch):
    monkeypatch.delenv("SYSTEM_ADMIN_MODE", raising=False)
    assert Settings(_env_file=None).system_admin_mode == "session"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("session", "session"),
        ("off", "off"),
        ("service", "service"),
        (" off\n", "off"),
        ("\tservice ", "service"),
    ],
)
def test_known_modes_are_accepted_after_trimming(monkeypatch, raw, expected):
    monkeypatch.setenv("SYSTEM_ADMIN_MODE", raw)
    assert Settings(_env_file=None).system_admin_mode == expected


@pytest.mark.parametrize("raw", ["", "   ", "Off", "SESSION", "none", "disabled", "true"])
def test_empty_unknown_or_miscased_mode_refuses_to_start(monkeypatch, raw):
    # Empty must not quietly mean "session": a deployment that writes
    # SYSTEM_ADMIN_MODE= meant to set something.
    monkeypatch.setenv("SYSTEM_ADMIN_MODE", raw)
    with pytest.raises(ValidationError, match="system_admin_mode"):
        Settings(_env_file=None)


# --- service credential (#2767, gate1 security changes 1 and 10) ---------------

_SERVICE_ENVS = (
    "SYSTEM_ADMIN_MODE",
    "SYSTEM_ADMIN_SERVICE_TOKEN",
    "SYSTEM_ADMIN_SERVICE_TOKEN_PREVIOUS",
    "SYSTEM_ADMIN_SERVICE_ALLOWED_CIDRS",
    "BILLING_SERVICE_TOKEN",
    "WORKER_SERVICE_TOKEN",
    "ENVIRONMENT",
)


@pytest.fixture
def clean_env(monkeypatch):
    for env in _SERVICE_ENVS:
        monkeypatch.delenv(env, raising=False)
    return monkeypatch


def _production(**overrides: str) -> Settings:
    values = {
        "environment": "production",
        "api_key_secret": secrets.token_hex(32),
        "audit_hmac_key": secrets.token_hex(32),
        "audit_pseudo_salt": secrets.token_hex(16),
        "system_admin_mode": "service",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_service_settings_default_to_empty(clean_env):
    s = Settings(_env_file=None)
    assert s.system_admin_service_token == ""
    assert s.system_admin_service_token_previous == ""
    assert s.system_admin_service_allowed_cidrs == ""
    assert s.system_admin_service_allowed_networks == ()
    assert s.system_admin_service_previous_tokens == ()


def test_previous_tokens_are_split_and_the_current_one_dropped(clean_env, caplog):
    current = secrets.token_hex(32)
    old = secrets.token_hex(32)
    s = Settings(
        _env_file=None,
        system_admin_mode="service",
        system_admin_service_token=current,
        system_admin_service_token_previous=f" {old} ,,{current}",
    )
    assert s.system_admin_service_previous_tokens == (old,)
    assert "repeats SYSTEM_ADMIN_SERVICE_TOKEN" in caplog.text
    assert "SYSTEM_ADMIN_SERVICE_TOKEN_PREVIOUS holds 1" in caplog.text
    assert old not in caplog.text and current not in caplog.text


@pytest.mark.parametrize("token", ["abc def", "abc,def", "abc\tdef", " trailing ", "a\n"])
def test_credential_with_a_blank_or_comma_refuses_everywhere(clean_env, token):
    with pytest.raises(ValidationError, match="SYSTEM_ADMIN_SERVICE_TOKEN"):
        Settings(_env_file=None, system_admin_mode="service", system_admin_service_token=token)


def test_previous_entry_with_an_inner_blank_refuses(clean_env):
    with pytest.raises(ValidationError, match="must not contain blanks"):
        Settings(
            _env_file=None,
            system_admin_service_token=secrets.token_hex(32),
            system_admin_service_token_previous="abc def",
        )


def test_token_outside_service_mode_only_warns(clean_env, caplog):
    Settings(_env_file=None, system_admin_mode="off", system_admin_service_token="x" * 40)
    assert "credential is ignored on this process" in caplog.text


def test_cidrs_are_parsed_and_malformed_ranges_refused(clean_env):
    s = Settings(_env_file=None, system_admin_service_allowed_cidrs="10.0.0.0/8, 192.0.2.0/24")
    assert [str(n) for n in s.system_admin_service_allowed_networks] == [
        "10.0.0.0/8",
        "192.0.2.0/24",
    ]
    with pytest.raises(ValidationError):
        Settings(_env_file=None, system_admin_service_allowed_cidrs="10.0.0.5/8")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, system_admin_service_allowed_cidrs="not-a-range")


def test_production_accepts_strong_private_credentials(clean_env):
    s = _production(
        system_admin_service_token=secrets.token_hex(32),
        system_admin_service_token_previous=secrets.token_hex(32),
    )
    assert s.system_admin_mode == "service"


def test_production_allows_an_unset_credential(clean_env):
    # Unset answers 503 at request time (fail-closed), not a boot failure.
    assert _production().system_admin_service_token == ""


@pytest.mark.parametrize(
    "weak", ["changeme", "change-me-system-admin-token", "dev-secret-x" * 4, "a" * 31]
)
def test_production_refuses_a_weak_current_credential(clean_env, weak):
    with pytest.raises(ValidationError, match="SYSTEM_ADMIN_SERVICE_TOKEN") as info:
        _production(system_admin_service_token=weak)
    assert weak not in str(info.value)


@pytest.mark.parametrize("weak", ["changeme", "change-me-old-token", "a" * 31])
def test_production_refuses_a_weak_previous_credential(clean_env, weak):
    # A previous credential is a live credential (gate1 security change 1).
    with pytest.raises(ValidationError, match="SYSTEM_ADMIN_SERVICE_TOKEN_PREVIOUS") as info:
        _production(
            system_admin_service_token=secrets.token_hex(32),
            system_admin_service_token_previous=f"{secrets.token_hex(32)},{weak}",
        )
    assert weak not in str(info.value)


@pytest.mark.parametrize("other", ["billing_service_token", "worker_service_token"])
@pytest.mark.parametrize("slot", ["current", "previous"])
def test_production_refuses_a_credential_shared_with_another_service(clean_env, other, slot):
    shared = secrets.token_hex(32)
    fields = {other: shared}
    if slot == "current":
        fields["system_admin_service_token"] = shared
    else:
        fields["system_admin_service_token"] = secrets.token_hex(32)
        fields["system_admin_service_token_previous"] = shared
    with pytest.raises(ValidationError, match="BILLING_SERVICE_TOKEN or WORKER_SERVICE_TOKEN"):
        _production(**fields)


def test_shared_credential_is_allowed_outside_production(clean_env):
    shared = secrets.token_hex(32)
    Settings(_env_file=None, billing_service_token=shared, system_admin_service_token=shared)
