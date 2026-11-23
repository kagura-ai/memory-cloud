"""Which host names the application keeps for itself (#2809).

``mcp_server.public.domains`` derives the application's own hosts and the
reserved names from the settings on every call (memoised only on the tuple of
source values), so a setting changed in place is seen by the next call.
"""

from __future__ import annotations

import pytest
import structlog

from config.settings import get_settings
from mcp_server.public import domains
from mcp_server.public.domains import (
    application_hosts,
    challenge_record,
    claimable_hostname,
    classify_request_host,
    is_reserved,
    log_application_hosts,
    public_domains_enabled,
)
from models.public_mcp_domain import CHALLENGE_LABEL, CHALLENGE_VALUE_PREFIX

# What ``configured`` makes ``application_hosts().names()`` return: exact
# names sorted, then zones with a leading dot. The billing, CORS, origin and
# routing hosts are reserved but are not application hosts.
EXPECTED_APPLICATION_HOST_NAMES = [
    "api-internal.example.org",
    "api.example.com",
    "app.example.com",
    "id.example.com",
    "mcp.example.com",
    ".ops.example.org",
]


@pytest.fixture
def configured(monkeypatch):
    """A deployment reached under app.example.com with a few extra names."""
    s = get_settings()
    monkeypatch.setattr(s, "frontend_url", "https://app.example.com")
    monkeypatch.setenv("API_URL", "https://api.example.com:8443/")
    monkeypatch.setattr(s, "oidc_issuer", "https://id.example.com/oidc")
    monkeypatch.setattr(s, "kmc_mcp_url", "https://mcp.example.com/mcp")
    monkeypatch.setattr(s, "payment_public_base_url", "https://billing.example.net")
    monkeypatch.setattr(s, "cors_origins", "https://console.example.net, http://localhost:3000")
    monkeypatch.setattr(s, "mcp_allowed_origins", "https://tools.example.net")
    monkeypatch.setattr(s, "mcp_public_domains_routing_target", "domains.example.net")
    monkeypatch.setattr(
        s, "mcp_public_domains_app_hosts", "api-internal.example.org,.ops.example.org"
    )
    monkeypatch.setattr(s, "mcp_public_domains_reserved_suffixes", "blocked.example.org")
    return monkeypatch


def test_switch_reads_the_setting(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "mcp_public_domains_enabled", False)
    assert public_domains_enabled() is False
    monkeypatch.setattr(get_settings(), "mcp_public_domains_enabled", True)
    assert public_domains_enabled() is True


def test_application_hosts(configured) -> None:
    hosts = application_hosts()
    for name in (
        "app.example.com",
        "api.example.com",
        "id.example.com",
        "mcp.example.com",
        "api-internal.example.org",
        "ops.example.org",
        "deep.ops.example.org",
    ):
        assert hosts.matches(name), name
    for name in (
        "sub.app.example.com",  # exact entries match only themselves
        "example.com",
        "billing.example.net",  # reserved, not an application host
        "console.example.net",
        "x.api-internal.example.org",
        "localhost",
    ):
        assert not hosts.matches(name), name
    # The whole list, compared as a value (localhost is not in it).
    assert hosts.names() == EXPECTED_APPLICATION_HOST_NAMES


def test_internal_shaped_hosts_are_dropped(monkeypatch) -> None:
    s = get_settings()
    monkeypatch.setattr(s, "frontend_url", "http://localhost:3000")
    monkeypatch.setenv("API_URL", "http://127.0.0.1:8080")
    monkeypatch.setattr(s, "oidc_issuer", "")
    monkeypatch.setattr(s, "kmc_mcp_url", "http://api.internal:8080/mcp")
    monkeypatch.setattr(s, "mcp_public_domains_app_hosts", "")
    assert application_hosts().names() == []


def test_reserved_names(configured) -> None:
    for name in (
        "app.example.com",
        "x.app.example.com",  # under an application host
        "x.api-internal.example.org",
        "ops.example.org",
        "billing.example.net",
        "pay.billing.example.net",
        "console.example.net",
        "tools.example.net",
        "domains.example.net",
        "a.domains.example.net",
        "blocked.example.org",
        "x.blocked.example.org",
        "x.onion",
        "x.ip6.arpa",
        "nas.home.arpa",
        "x.internal",
    ):
        assert is_reserved(name), name
    for name in ("mcp.customer.example.org", "example.net", "notblocked.example.org"):
        assert not is_reserved(name), name


def test_claimable_hostname(configured) -> None:
    assert claimable_hostname("MCP.Customer.Example.org.") == "mcp.customer.example.org"
    assert claimable_hostname("x.blocked.example.org") is None
    assert claimable_hostname("bad_name.example.org") is None
    assert claimable_hostname("x.onion") is None
    assert claimable_hostname("app.example.com") is None


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("app.example.com", "application"),
        ("deep.ops.example.org", "application"),
        ("testserver", "internal"),
        ("api-blue", "internal"),
        ("127.0.0.1", "internal"),
        ("[::1]", "internal"),
        ("", "internal"),
        ("x.internal", "internal"),
        ("nas.home.arpa", "internal"),
        ("mcp.customer.example.org", "foreign"),
        ("x.1.0.0.2.ip6.arpa", "foreign"),
        ("4.3.2.1.in-addr.arpa", "foreign"),
        ("x.onion", "foreign"),
        ("blocked.example.org", "foreign"),
        ("sub.app.example.com", "foreign"),
    ],
)
def test_classify_request_host(configured, host: str, expected: str) -> None:
    assert classify_request_host(host) == expected


def test_zone_entry_covers_every_name_under_it(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "mcp_public_domains_app_hosts", ".svc.example.org")
    assert classify_request_host("api.svc.example.org") == "application"


def test_a_setting_changed_in_place_is_seen_by_the_next_call(configured) -> None:
    s = get_settings()
    assert classify_request_host("new.example.org") == "foreign"
    assert claimable_hostname("new.example.org") == "new.example.org"
    configured.setattr(s, "mcp_public_domains_app_hosts", "new.example.org")
    assert classify_request_host("new.example.org") == "application"
    assert claimable_hostname("new.example.org") is None
    configured.setattr(s, "mcp_public_domains_app_hosts", "")
    configured.setattr(s, "mcp_public_domains_reserved_suffixes", "example.org")
    assert classify_request_host("new.example.org") == "foreign"
    assert claimable_hostname("new.example.org") is None
    configured.setattr(s, "mcp_public_domains_reserved_suffixes", "")
    assert claimable_hostname("new.example.org") == "new.example.org"
    configured.setattr(s, "frontend_url", "https://new.example.org")
    assert classify_request_host("new.example.org") == "application"
    configured.setenv("API_URL", "https://later.example.org")
    assert classify_request_host("later.example.org") == "application"


def test_the_memo_is_keyed_on_values_not_on_the_settings_object() -> None:
    cached = domains._host_sets
    assert hasattr(cached, "cache_info")
    assert cached.cache_parameters()["maxsize"] == 8


def test_challenge_record() -> None:
    name, value = challenge_record("mcp.customer.example.org", "ab" * 32)
    assert CHALLENGE_LABEL == "_kagura-public-mcp"
    assert CHALLENGE_VALUE_PREFIX == "kagura-public-mcp-verification="
    assert name == "_kagura-public-mcp.mcp.customer.example.org"
    assert value == "kagura-public-mcp-verification=" + "ab" * 32


def test_startup_line_lists_the_application_hosts(configured) -> None:
    configured.setattr(get_settings(), "mcp_public_domains_enabled", True)
    with structlog.testing.capture_logs() as logs:
        log_application_hosts()
    lines = [e for e in logs if e["event"] == "public_mcp_domains_application_hosts"]
    assert len(lines) == 1
    assert lines[0]["log_level"] == "info"
    assert lines[0]["hosts"] == EXPECTED_APPLICATION_HOST_NAMES


def test_startup_line_warns_when_empty(monkeypatch) -> None:
    s = get_settings()
    monkeypatch.setattr(s, "mcp_public_domains_enabled", True)
    monkeypatch.setattr(s, "frontend_url", "http://localhost:3000")
    monkeypatch.setenv("API_URL", "")
    monkeypatch.setattr(s, "oidc_issuer", "")
    monkeypatch.setattr(s, "kmc_mcp_url", "http://localhost:8080/mcp")
    monkeypatch.setattr(s, "mcp_public_domains_app_hosts", "")
    with structlog.testing.capture_logs() as logs:
        log_application_hosts()
    lines = [e for e in logs if e["event"] == "public_mcp_domains_application_hosts"]
    assert len(lines) == 1
    assert lines[0]["log_level"] == "warning"
    assert lines[0]["hosts"] == []


def test_no_startup_line_while_off(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "mcp_public_domains_enabled", False)
    with structlog.testing.capture_logs() as logs:
        log_application_hosts()
    assert not [e for e in logs if e["event"] == "public_mcp_domains_application_hosts"]


def test_constants() -> None:
    from models import public_mcp_domain as m

    assert m.DOMAIN_SERVICE_ID == "public-mcp-domains"
    assert m.PROOF_GRACE_HOURS == 72
    assert m.PROOF_MIN_MISSING_CHECKS == 3
    assert m.SUSPENDED_HOLD_DAYS == 30
    assert m.PENDING_TTL_DAYS == 7
    assert m.SERVE_LEASE_HOURS == 72
    assert m.RECHECK_AGE_SECONDS == 3600
    assert m.MAINTENANCE_INTERVAL_SECONDS == 900
    assert m.MAINTENANCE_BATCH == 200
    assert m.MAINTENANCE_CONCURRENCY == 4
    assert m.CHECK_TIMEOUT_SECONDS == 8
    assert m.VERIFY_LIMIT_PER_HOUR == 20
    assert m.ACTIVATIONS_PER_DAY == 6
    # The challenge name of the longest claimable host still fits in 253.
    assert m.MAX_HOSTNAME_LENGTH == 253 - len(m.CHALLENGE_LABEL) - 1 == 234


# --- #2979: the CNAME proof zone -------------------------------------------


def test_cname_proof_target_is_off_while_the_zone_is_empty(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "mcp_public_domains_cname_proof_zone", "")
    calls: list[str] = []
    monkeypatch.setattr(domains, "cname_proof_label", lambda token: calls.append(token) or "x")
    assert domains.cname_proof_target("ab" * 32) is None
    assert calls == []  # no label is derived


def test_cname_proof_target_is_a_label_of_the_token_in_the_zone(monkeypatch) -> None:
    import hashlib

    from models.public_mcp_domain import CNAME_LABEL_HEX_CHARS, CNAME_LABEL_INPUT_PREFIX

    monkeypatch.setattr(get_settings(), "mcp_public_domains_cname_proof_zone", "proof.example.net")
    token = "ab" * 32
    label = hashlib.sha256(("kagura-public-mcp-cname-v1:" + token).encode()).hexdigest()[:32]
    assert CNAME_LABEL_INPUT_PREFIX == "kagura-public-mcp-cname-v1:"
    assert CNAME_LABEL_HEX_CHARS == 32
    assert domains.cname_proof_label(token) == label
    assert domains.cname_proof_target(token) == f"{label}.proof.example.net"
    # Row-specific: another token, another target; never the token itself.
    other = domains.cname_proof_target("cd" * 32)
    assert other is not None and other != domains.cname_proof_target(token)
    assert token[:32] != label
    # Any ASCII token (tests use short ones) derives a label.
    assert len(domains.cname_proof_label("t")) == 32


def test_names_in_the_cname_proof_zone_are_reserved(configured) -> None:
    s = get_settings()
    assert claimable_hostname("x.proof.example.net") == "x.proof.example.net"
    configured.setattr(s, "mcp_public_domains_cname_proof_zone", "proof.example.net")
    for name in ("proof.example.net", "x.proof.example.net", f"{'a1' * 16}.proof.example.net"):
        assert is_reserved(name), name
        assert claimable_hostname(name) is None, name
    assert claimable_hostname("example.net") == "example.net"
    assert claimable_hostname("mcp.customer.example.org") == "mcp.customer.example.org"
    # A name in the zone is not an application host: it stays foreign.
    assert classify_request_host("x.proof.example.net") == "foreign"
    configured.setattr(s, "mcp_public_domains_cname_proof_zone", "")
    assert claimable_hostname("x.proof.example.net") == "x.proof.example.net"
