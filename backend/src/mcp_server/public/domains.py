"""Which host names belong to the application, and which may be claimed (#2809).

A verified custom domain is an alias of one public MCP endpoint
(``docs/public-mcp-domains.md``). This module decides, from the settings,
what a request's Host is:

- ``application``: a host of ``FRONTEND_URL``, ``API_URL``, ``OIDC_ISSUER``
  or ``KMC_MCP_URL``, or an entry of ``MCP_PUBLIC_DOMAINS_APP_HOSTS``
  (``name`` = that host, ``.name`` = that host and every name under it);
- ``internal``: an internal caller's shape (empty, an address literal, a
  single label, a pass-through name);
- ``foreign``: anything else (an alias candidate).

and which names may be claimed: a normalised name that is not special-use,
not equal to or under an application host, the billing origin, a CORS or MCP
browser origin, the routing target, the CNAME proof zone
(``MCP_PUBLIC_DOMAINS_CNAME_PROOF_ZONE``, #2979), or an entry of
``MCP_PUBLIC_DOMAINS_RESERVED_SUFFIXES``.

It also derives a claim's two proof records from its token: the TXT record
and, while the CNAME proof zone is set, the row's CNAME target.

The host sets are rebuilt from the setting values on every call, memoised
only on the tuple of those values: a setting changed in place is seen by the
next call. Nothing here touches the database.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from config.settings import get_settings
from models.public_mcp_domain import (
    CHALLENGE_LABEL,
    CHALLENGE_VALUE_PREFIX,
    CNAME_LABEL_HEX_CHARS,
    CNAME_LABEL_INPUT_PREFIX,
)
from utils.logger import get_logger
from utils.public_hostname import (
    is_internal_shape,
    is_reserved_special_use,
    normalize_hostname,
)

logger = get_logger(__name__)

RequestHostClass = Literal["application", "internal", "foreign"]


@dataclass(frozen=True)
class HostSet:
    """Host names matched exactly, and zones matched with every name under them."""

    exact: frozenset[str] = frozenset()
    zones: frozenset[str] = frozenset()

    def matches(self, name: str) -> bool:
        """Whether ``name`` is an exact entry, or equal to or under a zone."""
        if name in self.exact:
            return True
        return any(name == zone or name.endswith("." + zone) for zone in self.zones)

    def names(self) -> list[str]:
        """The entries for an operator: exact names, then zones with a leading dot."""
        return sorted(self.exact) + sorted(f".{zone}" for zone in self.zones)


@dataclass(frozen=True)
class _HostSets:
    application: HostSet
    reserved: HostSet


def public_domains_enabled() -> bool:
    """Whether the operator switched verified custom domains on."""
    return bool(get_settings().mcp_public_domains_enabled)


def _url_host(url: str) -> str | None:
    """The normalised host of a URL (or bare host), or ``None``."""
    url = url.strip()
    if not url:
        return None
    try:
        host = urlsplit(url if "://" in url else f"//{url}").hostname
    except ValueError:
        return None
    return normalize_hostname(host) if host else None


def _list_hosts(value: str) -> list[str]:
    hosts = (_url_host(entry) for entry in value.split(","))
    return [host for host in hosts if host is not None]


def _names(value: str) -> list[str]:
    names = (normalize_hostname(entry) for entry in value.split(",") if entry.strip())
    return [name for name in names if name is not None]


def _sources() -> tuple[str, ...]:
    s = get_settings()
    return (
        s.frontend_url,
        os.environ.get("API_URL", ""),
        s.oidc_issuer,
        s.kmc_mcp_url,
        s.payment_public_base_url,
        s.cors_origins,
        s.mcp_allowed_origins,
        s.mcp_public_domains_routing_target,
        s.mcp_public_domains_app_hosts,
        s.mcp_public_domains_reserved_suffixes,
        s.mcp_public_domains_cname_proof_zone,
    )


@lru_cache(maxsize=8)
def _host_sets(sources: tuple[str, ...]) -> _HostSets:
    """Build both host sets from the values ``_sources()`` returns (pure)."""
    (
        frontend_url,
        api_url,
        oidc_issuer,
        kmc_mcp_url,
        payment_url,
        cors_origins,
        mcp_allowed_origins,
        routing_target,
        app_hosts,
        reserved_suffixes,
        cname_proof_zone,
    ) = sources

    exact: set[str] = set()
    zones: set[str] = set()
    for url in (frontend_url, api_url, oidc_issuer, kmc_mcp_url):
        host = _url_host(url)
        if host is not None:
            exact.add(host)
    for raw in app_hosts.split(","):
        entry = raw.strip()
        if not entry:
            continue
        zone = entry.startswith(".")
        name = normalize_hostname(entry[1:] if zone else entry)
        if name is not None:
            (zones if zone else exact).add(name)
    # Internal-shaped hosts pass the guard anyway.
    application = HostSet(
        exact=frozenset(h for h in exact if not is_internal_shape(h)),
        zones=frozenset(z for z in zones if not is_internal_shape(z)),
    )

    reserved: set[str] = exact | zones
    reserved.update(_list_hosts(payment_url))
    reserved.update(_list_hosts(cors_origins))
    reserved.update(_list_hosts(mcp_allowed_origins))
    reserved.update(_names(routing_target))
    reserved.update(_names(reserved_suffixes))
    # #2979: the CNAME targets live in the zone; none of its names is a domain.
    reserved.update(_names(cname_proof_zone))
    return _HostSets(application=application, reserved=HostSet(zones=frozenset(reserved)))


def _current() -> _HostSets:
    return _host_sets(_sources())


def application_hosts() -> HostSet:
    """The host names the application itself is reached under."""
    return _current().application


def is_reserved(name: str) -> bool:
    """Whether the normalised ``name`` may never be claimed.

    Special-use; or equal to or under an application host, the billing
    origin's host, a ``CORS_ORIGINS`` / ``MCP_ALLOWED_ORIGINS`` host, the
    routing target, the CNAME proof zone, or an entry of
    ``MCP_PUBLIC_DOMAINS_RESERVED_SUFFIXES``.
    """
    return is_reserved_special_use(name) or _current().reserved.matches(name)


def claimable_hostname(raw: str) -> str | None:
    """The normalised name when it may be claimed as a custom domain, else ``None``."""
    name = normalize_hostname(raw)
    if name is None or is_reserved(name):
        return None
    return name


def classify_request_host(host: str) -> RequestHostClass:
    """Classify the name a Host header carries (``utils.public_hostname.host_from_header``)."""
    if application_hosts().matches(host):
        return "application"
    if is_internal_shape(host):
        return "internal"
    return "foreign"


def challenge_record(hostname: str, token: str) -> tuple[str, str]:
    """The TXT record (name, value) that proves control of ``hostname``."""
    return f"{CHALLENGE_LABEL}.{hostname}", f"{CHALLENGE_VALUE_PREFIX}{token}"


def cname_proof_label(token: str) -> str:
    """#2979: the row's CNAME label, derived from its token (pure).

    The first ``CNAME_LABEL_HEX_CHARS`` lower-case hex characters of
    sha256(``CNAME_LABEL_INPUT_PREFIX`` + token): a valid DNS label that
    belongs to one claim, like the TXT value. Never stored, never logged.
    """
    digest = hashlib.sha256(f"{CNAME_LABEL_INPUT_PREFIX}{token}".encode()).hexdigest()
    return digest[:CNAME_LABEL_HEX_CHARS]


def cname_proof_target(token: str) -> str | None:
    """#2979: the CNAME target ``<label>.<zone>`` that proves control of the
    row with ``token``; ``None`` (nothing derived) while the zone is empty."""
    zone = get_settings().mcp_public_domains_cname_proof_zone
    if not zone:
        return None
    return f"{cname_proof_label(token)}.{zone}"


def log_application_hosts() -> None:
    """Say at start-up which hosts are the application's (switch on only).

    A warning when the list is empty: every request then reaches the
    application under an internal shape or is treated as an alias.
    """
    if not public_domains_enabled():
        return
    hosts = application_hosts().names()
    log = logger.info if hosts else logger.warning
    log("public_mcp_domains_application_hosts", hosts=hosts)
