"""Settings of verified custom domains for public MCP endpoints (#2809).

Seven settings, off by default. Switching on needs a routing target; every
host-name entry must normalise, or the process refuses to start. A refusal
names the variable, never its value.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from config.settings import Settings

_ENV_KEYS = [
    "MCP_PUBLIC_DOMAINS_ENABLED",
    "MCP_PUBLIC_DOMAINS_AUTHORITY",
    "MCP_PUBLIC_DOMAINS_ROUTING_TARGET",
    "MCP_PUBLIC_DOMAINS_APP_HOSTS",
    "MCP_PUBLIC_DOMAINS_RESERVED_SUFFIXES",
    "MCP_PUBLIC_DOMAINS_REASSIGN_COOLDOWN_HOURS",
    "MCP_PUBLIC_DOMAINS_MAX_PER_WORKSPACE",
    "MCP_PUBLIC_ENDPOINTS_ENABLED",
]


@pytest.fixture
def clean_env(monkeypatch):
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def test_defaults(clean_env) -> None:
    s = Settings(_env_file=None)
    assert s.mcp_public_domains_enabled is False
    assert s.mcp_public_domains_authority == "entitlement"
    assert s.mcp_public_domains_routing_target == ""
    assert s.mcp_public_domains_app_hosts == ""
    assert s.mcp_public_domains_reserved_suffixes == ""
    assert s.mcp_public_domains_reassign_cooldown_hours == 168
    assert s.mcp_public_domains_max_per_workspace == 3


def test_exactly_seven_settings() -> None:
    names = {n for n in Settings.model_fields if n.startswith("mcp_public_domains_")}
    assert names == {
        "mcp_public_domains_enabled",
        "mcp_public_domains_authority",
        "mcp_public_domains_routing_target",
        "mcp_public_domains_app_hosts",
        "mcp_public_domains_reserved_suffixes",
        "mcp_public_domains_reassign_cooldown_hours",
        "mcp_public_domains_max_per_workspace",
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
