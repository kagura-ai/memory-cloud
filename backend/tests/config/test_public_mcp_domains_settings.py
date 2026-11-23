"""Settings of verified custom domains for public MCP endpoints (#2809).

Domain reporting and serving are off by default. Switching on needs a routing target; every
host-name entry must normalise, or the process refuses to start. A refusal
names the variable, never its value.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from config.settings import Settings

_ENV_KEYS = [
    "MCP_PUBLIC_DOMAINS_TLS_REPORTER_TOKEN",
    "MCP_PUBLIC_DOMAINS_ENABLED",
    "MCP_PUBLIC_DOMAINS_AUTHORITY",
    "MCP_PUBLIC_DOMAINS_ROUTING_TARGET",
    "MCP_PUBLIC_DOMAINS_APP_HOSTS",
    "MCP_PUBLIC_DOMAINS_RESERVED_SUFFIXES",
    "MCP_PUBLIC_DOMAINS_REASSIGN_COOLDOWN_HOURS",
    "MCP_PUBLIC_DOMAINS_MAX_PER_WORKSPACE",
    "MCP_PUBLIC_DOMAINS_CNAME_PROOF_ZONE",
    "MCP_PUBLIC_ENDPOINTS_ENABLED",
    "FRONTEND_URL",
    "OIDC_ISSUER",
    "KMC_MCP_URL",
    "API_URL",
]


@pytest.fixture
def clean_env(monkeypatch):
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def test_defaults(clean_env) -> None:
    s = Settings(_env_file=None)
    assert s.mcp_public_domains_enabled is False
    assert s.mcp_public_domains_tls_reporter_token == ""
    assert s.mcp_public_domains_authority == "entitlement"
    assert s.mcp_public_domains_routing_target == ""
    assert s.mcp_public_domains_app_hosts == ""
    assert s.mcp_public_domains_reserved_suffixes == ""
    assert s.mcp_public_domains_reassign_cooldown_hours == 168
    assert s.mcp_public_domains_max_per_workspace == 3
    assert s.mcp_public_domains_cname_proof_zone == ""


def test_domain_settings_inventory() -> None:
    names = {n for n in Settings.model_fields if n.startswith("mcp_public_domains_")}
    assert names == {
        "mcp_public_domains_tls_reporter_token",
        "mcp_public_domains_enabled",
        "mcp_public_domains_authority",
        "mcp_public_domains_routing_target",
        "mcp_public_domains_app_hosts",
        "mcp_public_domains_reserved_suffixes",
        "mcp_public_domains_reassign_cooldown_hours",
        "mcp_public_domains_max_per_workspace",
        "mcp_public_domains_cname_proof_zone",
    }


def test_switch_on_needs_a_routing_target(clean_env) -> None:
    clean_env.setenv("MCP_PUBLIC_DOMAINS_ENABLED", "true")
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)
    assert "MCP_PUBLIC_DOMAINS_ROUTING_TARGET" in str(excinfo.value)


def test_switch_on_with_a_routing_target(clean_env) -> None:
    clean_env.setenv("MCP_PUBLIC_DOMAINS_ENABLED", "true")
    clean_env.setenv("MCP_PUBLIC_DOMAINS_ROUTING_TARGET", "Domains.Example.com.")
    s = Settings(_env_file=None)
    assert s.mcp_public_domains_enabled is True
    assert s.mcp_public_domains_routing_target == "domains.example.com"


def test_switch_on_without_public_endpoints_is_allowed(clean_env) -> None:
    clean_env.setenv("MCP_PUBLIC_DOMAINS_ENABLED", "true")
    clean_env.setenv("MCP_PUBLIC_DOMAINS_ROUTING_TARGET", "domains.example.com")
    clean_env.setenv("MCP_PUBLIC_ENDPOINTS_ENABLED", "false")
    s = Settings(_env_file=None)
    assert s.mcp_public_domains_enabled is True
    assert s.mcp_public_endpoints_enabled is False


@pytest.mark.parametrize("value", ["entitlement", "operator", " Operator "])
def test_authority_values(clean_env, value: str) -> None:
    clean_env.setenv("MCP_PUBLIC_DOMAINS_AUTHORITY", value)
    assert Settings(_env_file=None).mcp_public_domains_authority == value.strip().lower()


@pytest.mark.parametrize("value", ["", "none", "billing"])
def test_authority_refused(clean_env, value: str) -> None:
    clean_env.setenv("MCP_PUBLIC_DOMAINS_AUTHORITY", value)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    "value",
    [
        "localhost",
        "target.internal",
        "nas.home.arpa",
        "x.onion",
        "x.ip6.arpa",
        "1.2.3.4",
        "target.example.com:443",
        "*.example.com",
        "https://target.example.com",
    ],
)
def test_routing_target_refused(clean_env, value: str) -> None:
    secret_marker = value
    clean_env.setenv("MCP_PUBLIC_DOMAINS_ROUTING_TARGET", value)
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)
    message = str(excinfo.value)
    assert "MCP_PUBLIC_DOMAINS_ROUTING_TARGET" in message
    assert secret_marker not in message


def test_app_hosts_normalised(clean_env) -> None:
    clean_env.setenv(
        "MCP_PUBLIC_DOMAINS_APP_HOSTS", " App.Example.com , .Internal-Zone.Example.org., "
    )
    s = Settings(_env_file=None)
    assert s.mcp_public_domains_app_hosts == "app.example.com,.internal-zone.example.org"


@pytest.mark.parametrize(
    "value",
    ["app.example.com,bad_host.example.com", "..example.com", "localhost", "1.2.3.4", "*.x.org"],
)
def test_app_hosts_bad_entry_refuses_start(clean_env, value: str) -> None:
    clean_env.setenv("MCP_PUBLIC_DOMAINS_APP_HOSTS", value)
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)
    assert "MCP_PUBLIC_DOMAINS_APP_HOSTS" in str(excinfo.value)


def test_reserved_suffixes_normalised(clean_env) -> None:
    clean_env.setenv("MCP_PUBLIC_DOMAINS_RESERVED_SUFFIXES", "Example.com, blocked.example.org.")
    s = Settings(_env_file=None)
    assert s.mcp_public_domains_reserved_suffixes == "example.com,blocked.example.org"


@pytest.mark.parametrize("value", ["example.com,bad_name.org", ".example.com", "localhost"])
def test_reserved_suffixes_bad_entry_refuses_start(clean_env, value: str) -> None:
    clean_env.setenv("MCP_PUBLIC_DOMAINS_RESERVED_SUFFIXES", value)
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)
    assert "MCP_PUBLIC_DOMAINS_RESERVED_SUFFIXES" in str(excinfo.value)


@pytest.mark.parametrize(
    ("value", "ok"), [("0", True), ("2160", True), ("-1", False), ("2161", False)]
)
def test_cooldown_bounds(clean_env, value: str, ok: bool) -> None:
    clean_env.setenv("MCP_PUBLIC_DOMAINS_REASSIGN_COOLDOWN_HOURS", value)
    if ok:
        assert Settings(_env_file=None).mcp_public_domains_reassign_cooldown_hours == int(value)
    else:
        with pytest.raises(ValidationError):
            Settings(_env_file=None)


@pytest.mark.parametrize(("value", "ok"), [("1", True), ("50", True), ("0", False), ("51", False)])
def test_max_per_workspace_bounds(clean_env, value: str, ok: bool) -> None:
    clean_env.setenv("MCP_PUBLIC_DOMAINS_MAX_PER_WORKSPACE", value)
    if ok:
        assert Settings(_env_file=None).mcp_public_domains_max_per_workspace == int(value)
    else:
        with pytest.raises(ValidationError):
            Settings(_env_file=None)


# --- #2979: the CNAME proof zone ---------------------------------------------


@pytest.mark.parametrize("value", ["", "   "])
def test_cname_proof_zone_empty_is_off(clean_env, value: str) -> None:
    clean_env.setenv("MCP_PUBLIC_DOMAINS_CNAME_PROOF_ZONE", value)
    assert Settings(_env_file=None).mcp_public_domains_cname_proof_zone == ""


def test_cname_proof_zone_normalised(clean_env) -> None:
    clean_env.setenv("MCP_PUBLIC_DOMAINS_CNAME_PROOF_ZONE", " Proof.Example.NET. ")
    assert Settings(_env_file=None).mcp_public_domains_cname_proof_zone == "proof.example.net"


def test_cname_proof_zone_of_200_characters_is_allowed(clean_env) -> None:
    zone = f"{'a' * 63}.{'b' * 63}.{'c' * 63}.{'d' * 8}"
    assert len(zone) == 200
    clean_env.setenv("MCP_PUBLIC_DOMAINS_CNAME_PROOF_ZONE", zone)
    assert Settings(_env_file=None).mcp_public_domains_cname_proof_zone == zone


@pytest.mark.parametrize(
    "value",
    [
        "localhost",
        "proof.internal",
        "nas.home.arpa",
        "x.onion",
        "1.2.3.4",
        "proof.example.com:443",
        "*.proof.example.com",
        "https://proof.example.com",
        f"{'a' * 63}.{'b' * 63}.{'c' * 63}.{'d' * 9}",  # 201 characters: no room for the label
    ],
)
def test_cname_proof_zone_refused(clean_env, value: str) -> None:
    clean_env.setenv("MCP_PUBLIC_DOMAINS_CNAME_PROOF_ZONE", value)
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)
    message = str(excinfo.value)
    assert "MCP_PUBLIC_DOMAINS_CNAME_PROOF_ZONE" in message
    secret_marker = value
    assert secret_marker not in message


@pytest.mark.parametrize(
    ("env", "app", "zone"),
    [
        ("FRONTEND_URL", "https://app.example.com", "app.example.com"),
        ("FRONTEND_URL", "https://app.example.com", "proof.app.example.com"),
        ("API_URL", "https://api.example.com", "proof.api.example.com"),
        ("OIDC_ISSUER", "https://id.example.com", "id.example.com"),
        ("KMC_MCP_URL", "https://mcp.example.com/mcp", "x.mcp.example.com"),
        ("MCP_PUBLIC_DOMAINS_APP_HOSTS", ".apps.example.com", "proof.apps.example.com"),
        ("MCP_PUBLIC_DOMAINS_APP_HOSTS", "edge.example.com", "edge.example.com"),
    ],
)
def test_cname_proof_zone_inside_an_application_host_refused(
    clean_env, env: str, app: str, zone: str
) -> None:
    clean_env.setenv(env, app)
    clean_env.setenv("MCP_PUBLIC_DOMAINS_CNAME_PROOF_ZONE", zone)
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)
    message = str(excinfo.value)
    assert "MCP_PUBLIC_DOMAINS_CNAME_PROOF_ZONE" in message
    secret_marker = zone
    assert secret_marker not in message


def test_cname_proof_zone_beside_the_application_hosts_is_allowed(clean_env) -> None:
    clean_env.setenv("FRONTEND_URL", "https://app.example.com")
    clean_env.setenv("MCP_PUBLIC_DOMAINS_APP_HOSTS", ".apps.example.com")
    clean_env.setenv("MCP_PUBLIC_DOMAINS_CNAME_PROOF_ZONE", "proof.example.com")
    assert Settings(_env_file=None).mcp_public_domains_cname_proof_zone == "proof.example.com"


def test_the_zone_bound_is_the_label_constant_not_a_literal() -> None:
    """Fix round 1: the validator reads the bound that leaves room for the label."""
    import inspect

    from config import settings as settings_module
    from models.public_mcp_domain import CNAME_LABEL_HEX_CHARS, CNAME_PROOF_ZONE_MAX_LENGTH
    from utils.public_hostname import MAX_HOSTNAME_LENGTH

    assert CNAME_PROOF_ZONE_MAX_LENGTH + 1 + CNAME_LABEL_HEX_CHARS <= MAX_HOSTNAME_LENGTH
    source = inspect.getsource(Settings._check_public_domains_cname_proof_zone)
    assert "CNAME_PROOF_ZONE_MAX_LENGTH" in source
    assert "200" not in source.split('"""')[-1]
    assert settings_module.CNAME_PROOF_ZONE_MAX_LENGTH == CNAME_PROOF_ZONE_MAX_LENGTH
