"""DNS proof of control and routing check of a public MCP domain (#2809).

The owner of ``<hostname>`` publishes

- TXT ``_kagura-public-mcp.<hostname>`` = ``kagura-public-mcp-verification=<token>``
  (the proof of control), and
- a CNAME to ``MCP_PUBLIC_DOMAINS_ROUTING_TARGET``, or the target's addresses
  (the routing, informational only).

``DnsDomainResolver.check`` answers both with one ``DomainCheck``:

- ``proof``: ``ok`` when one of the first 20 TXT values at the challenge name
  (a CNAME there is followed by the resolver) equals the expected value
  exactly, a value being the concatenation of its character-strings;
  ``missing`` on NXDOMAIN, no answer, or no equal value; ``unavailable`` on a
  timeout, SERVFAIL / no nameservers, a missing resolver configuration or
  any other error.
- ``routing``: ``ok`` when the name's CNAME chain contains the target or the
  A/AAAA sets of the name and the target intersect; ``mismatch`` when both
  resolve and neither holds; else ``unresolved``. Routing never decides the
  proof or a state.

DNS only, through the recursive resolver of the container's system
configuration: no connection to an address read from DNS, no HTTP, no TLS,
no query to an authoritative server. No application cache: a new resolver,
without a cache, is built for every check, and each query has a lifetime of
``QUERY_LIFETIME_SECONDS``. The queries of one check run concurrently.

The overall time bound belongs to the caller (``lookup`` in
``services.public_mcp_domains``), so that it also holds for a replaced
resolver. Nothing here logs a host name, a token or an answer.

At import time this module needs only the standard library and
``utils.public_hostname``; ``dns`` (dnspython) is imported by the resolver on
first use, so the application loads where the library is absent.
"""

from __future__ import annotations

import asyncio
import ipaddress
import itertools
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from utils.public_hostname import normalize_hostname

__all__ = [
    "MAX_TXT_VALUES",
    "QUERY_LIFETIME_SECONDS",
    "DnsDomainResolver",
    "DomainCheck",
    "DomainResolver",
    "ProofResult",
    "RoutingResult",
    "domain_resolver",
]

ProofResult = Literal["ok", "missing", "unavailable"]
RoutingResult = Literal["ok", "mismatch", "unresolved"]

# Lifetime of one DNS query (all retries and nameservers included).
QUERY_LIFETIME_SECONDS = 3.0
# TXT values read at the challenge name; later values are ignored.
MAX_TXT_VALUES = 20

_IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


@dataclass(frozen=True, slots=True)
class DomainCheck:
    """Result of one check of a host name."""

    proof: ProofResult
    routing: RoutingResult


class DomainResolver(Protocol):
    """Checks the proof of control and the routing of a host name."""

    async def check(self, hostname: str, token: str, routing_target: str) -> DomainCheck: ...


class _AsyncResolver(Protocol):
    """The part of ``dns.asyncresolver.Resolver`` the adapter uses."""

    async def resolve(self, qname: str, rdtype: str, *, lifetime: float, search: bool) -> Any: ...


def _system_resolver() -> _AsyncResolver:
    """A recursive resolver from the system configuration, without a cache."""
    import dns.asyncresolver

    resolver = dns.asyncresolver.Resolver()
    resolver.cache = None
    resolver.lifetime = QUERY_LIFETIME_SECONDS
    return resolver


def _name_text(name: object) -> str:
    return str(name).rstrip(".").lower()


class DnsDomainResolver:
    """``DomainResolver`` on the configured recursive DNS resolver.

    ``resolver_factory`` builds the resolver object for one check; the
    default reads the system configuration. Tests pass a scripted fake.
    """

    def __init__(self, resolver_factory: Callable[[], _AsyncResolver] | None = None) -> None:
        self._resolver_factory = resolver_factory or _system_resolver

    async def check(self, hostname: str, token: str, routing_target: str) -> DomainCheck:
        from models.public_mcp_domain import CHALLENGE_LABEL, CHALLENGE_VALUE_PREFIX

        try:
            resolver = self._resolver_factory()
        except Exception:
            # No usable resolver configuration: nothing can be decided.
            return DomainCheck("unavailable", "unresolved")
        proof, routing = await asyncio.gather(
            self._proof(
                resolver,
                f"{CHALLENGE_LABEL}.{hostname}",
                f"{CHALLENGE_VALUE_PREFIX}{token}",
            ),
            self._routing(resolver, hostname, routing_target),
        )
        return DomainCheck(proof, routing)

    @staticmethod
    async def _resolve(resolver: _AsyncResolver, qname: str, rdtype: str) -> Any:
        return await resolver.resolve(qname, rdtype, lifetime=QUERY_LIFETIME_SECONDS, search=False)

    async def _proof(self, resolver: _AsyncResolver, qname: str, expected: str) -> ProofResult:
        import dns.resolver

        try:
            answer = await self._resolve(resolver, qname, "TXT")
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
            return "missing"
        except Exception:
            return "unavailable"
        wanted = expected.encode("ascii")
        try:
            for rdata in itertools.islice(answer, MAX_TXT_VALUES):
                if b"".join(getattr(rdata, "strings", ())) == wanted:
                    return "ok"
        except Exception:
            return "unavailable"
        return "missing"

    async def _routing(
        self, resolver: _AsyncResolver, hostname: str, routing_target: str
    ) -> RoutingResult:
        target = normalize_hostname(routing_target)
        if target is None:
            return "unresolved"
        (name_chain, name_addresses), (_, target_addresses) = await asyncio.gather(
            self._addresses(resolver, hostname),
            self._addresses(resolver, target),
        )
        if target in name_chain or name_addresses & target_addresses:
            return "ok"
        if name_addresses and target_addresses:
            return "mismatch"
        return "unresolved"

    async def _addresses(
        self, resolver: _AsyncResolver, name: str
    ) -> tuple[set[str], set[_IpAddress]]:
        """The names of ``name``'s CNAME chain and its A/AAAA addresses."""
        results = await asyncio.gather(
            self._address_records(resolver, name, "A"),
            self._address_records(resolver, name, "AAAA"),
        )
        chain: set[str] = set()
        addresses: set[_IpAddress] = set()
        for names, found in results:
            chain |= names
            addresses |= found
        return chain, addresses

    async def _address_records(
        self, resolver: _AsyncResolver, name: str, rdtype: str
    ) -> tuple[set[str], set[_IpAddress]]:
        try:
            answer = await self._resolve(resolver, name, rdtype)
            chain = {_name_text(answer.canonical_name)}
            for rrset in answer.chaining_result.cnames:
                chain.update(_name_text(rdata.target) for rdata in rrset)
            addresses = {ipaddress.ip_address(rdata.address) for rdata in answer}
        except Exception:
            return set(), set()
        return chain, addresses


def domain_resolver() -> DomainResolver:
    """The resolver the domain service uses; tests replace this function."""
    return DnsDomainResolver()
