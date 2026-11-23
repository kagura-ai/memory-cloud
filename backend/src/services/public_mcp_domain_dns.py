"""DNS proof of control and routing check of a public MCP domain (#2809).

The owner of ``<hostname>`` publishes

- TXT ``_kagura-public-mcp.<hostname>`` = ``kagura-public-mcp-verification=<token>``
  (the proof of control), and
- a CNAME to ``MCP_PUBLIC_DOMAINS_ROUTING_TARGET``, or the target's addresses
  (the routing, informational only);
- or (#2979, with ``MCP_PUBLIC_DOMAINS_CNAME_PROOF_ZONE`` set) a CNAME to the
  row's own target ``<label>.<zone>``, which both routes the name (through
  the zone's wildcard) and proves control.

``DnsDomainResolver.check`` answers with one ``DomainCheck``:

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
- with a CNAME target (#2979): the CNAME part is ``ok`` when the name's chain
  (from its A/AAAA answers, the ones routing reads: no extra query) contains
  the target; ``missing`` once one A or AAAA lookup of the name settled
  (an answer, NXDOMAIN or no answer) without it; ``unavailable`` when none
  settled. ``proof`` is ``ok`` when the TXT or the CNAME part is, else
  ``unavailable`` when either is, else ``missing``; ``proof_via`` says which
  record proved it (``txt``, ``cname`` or ``both``). Without a CNAME target
  the check is exactly the TXT check and ``proof_via`` is ``None``.

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
    "ProofVia",
    "RoutingResult",
    "combine_proof",
    "domain_resolver",
]

ProofResult = Literal["ok", "missing", "unavailable"]
RoutingResult = Literal["ok", "mismatch", "unresolved"]
ProofVia = Literal["txt", "cname", "both"]
# How one A/AAAA lookup ended: an answer, a definite empty one (NXDOMAIN or
# no answer), or indeterminate (timeout, SERVFAIL, any other error).
_Settled = Literal["answer", "empty", "error"]

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
    # #2979: which record proved control; set only by a check given a CNAME
    # target, and only when the proof is ``ok``.
    proof_via: ProofVia | None = None


class DomainResolver(Protocol):
    """Checks the proof of control and the routing of a host name."""

    async def check(
        self, hostname: str, token: str, routing_target: str, *, cname_target: str | None = None
    ) -> DomainCheck: ...


def combine_proof(txt: ProofResult, cname: ProofResult) -> tuple[ProofResult, ProofVia | None]:
    """#2979: the proof of the TXT part and the CNAME part together.

    ``ok`` when either is ``ok`` (``proof_via`` names which, ``both`` for
    both); else ``unavailable`` when either is; else ``missing``.
    """
    if txt == "ok" and cname == "ok":
        return "ok", "both"
    if txt == "ok":
        return "ok", "txt"
    if cname == "ok":
        return "ok", "cname"
    if "unavailable" in (txt, cname):
        return "unavailable", None
    return "missing", None


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


def _is_definite_empty(error: Exception) -> bool:
    """Whether a lookup error is a definite "no such record" (NXDOMAIN or no answer)."""
    import dns.resolver

    return isinstance(error, dns.resolver.NXDOMAIN | dns.resolver.NoAnswer)


def _name_text(name: object) -> str:
    return str(name).rstrip(".").lower()


class DnsDomainResolver:
    """``DomainResolver`` on the configured recursive DNS resolver.

    ``resolver_factory`` builds the resolver object for one check; the
    default reads the system configuration. Tests pass a scripted fake.
    """

    def __init__(self, resolver_factory: Callable[[], _AsyncResolver] | None = None) -> None:
        self._resolver_factory = resolver_factory or _system_resolver

    async def check(
        self, hostname: str, token: str, routing_target: str, *, cname_target: str | None = None
    ) -> DomainCheck:
        from models.public_mcp_domain import CHALLENGE_LABEL, CHALLENGE_VALUE_PREFIX

        try:
            resolver = self._resolver_factory()
        except Exception:
            # No usable resolver configuration: nothing can be decided.
            return DomainCheck("unavailable", "unresolved")
        challenge = self._proof(
            resolver,
            f"{CHALLENGE_LABEL}.{hostname}",
            f"{CHALLENGE_VALUE_PREFIX}{token}",
        )
        if cname_target is None:
            proof, routing = await asyncio.gather(
                challenge, self._routing(resolver, hostname, routing_target)
            )
            return DomainCheck(proof, routing)
        # #2979: the name's A/AAAA answers serve both the routing and the
        # CNAME proof; the target is compared as the chain's names are (lower
        # case, no trailing dot), never normalised away.
        target = normalize_hostname(routing_target)
        wanted = cname_target.rstrip(".").lower()
        txt, (name_chain, name_addresses, settled), target_found = await asyncio.gather(
            challenge,
            self._addresses_settled(resolver, hostname),
            self._target_addresses(resolver, target),
        )
        routing = self._routing_result(target, name_chain, name_addresses, target_found)
        if wanted in name_chain:
            cname: ProofResult = "ok"
        elif "answer" in settled or "empty" in settled:
            cname = "missing"
        else:
            cname = "unavailable"
        proof, via = combine_proof(txt, cname)
        return DomainCheck(proof, routing, via)

    @staticmethod
    async def _resolve(resolver: _AsyncResolver, qname: str, rdtype: str) -> Any:
        return await resolver.resolve(qname, rdtype, lifetime=QUERY_LIFETIME_SECONDS, search=False)

    async def _proof(self, resolver: _AsyncResolver, qname: str, expected: str) -> ProofResult:
        try:
            answer = await self._resolve(resolver, qname, "TXT")
        except Exception as error:
            return "missing" if _is_definite_empty(error) else "unavailable"
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
        return self._routing_result(target, name_chain, name_addresses, target_addresses)

    @staticmethod
    def _routing_result(
        target: str | None,
        name_chain: set[str],
        name_addresses: set[_IpAddress],
        target_addresses: set[_IpAddress],
    ) -> RoutingResult:
        if target is None:
            return "unresolved"
        if target in name_chain or name_addresses & target_addresses:
            return "ok"
        if name_addresses and target_addresses:
            return "mismatch"
        return "unresolved"

    async def _target_addresses(
        self, resolver: _AsyncResolver, target: str | None
    ) -> set[_IpAddress]:
        """The routing target's A/AAAA addresses; no query without a target."""
        if target is None:
            return set()
        _, addresses = await self._addresses(resolver, target)
        return addresses

    async def _addresses(
        self, resolver: _AsyncResolver, name: str
    ) -> tuple[set[str], set[_IpAddress]]:
        """The names of ``name``'s CNAME chain and its A/AAAA addresses."""
        chain, addresses, _ = await self._addresses_settled(resolver, name)
        return chain, addresses

    async def _addresses_settled(
        self, resolver: _AsyncResolver, name: str
    ) -> tuple[set[str], set[_IpAddress], set[_Settled]]:
        """``_addresses`` and how each of the two lookups ended."""
        results = await asyncio.gather(
            self._address_records(resolver, name, "A"),
            self._address_records(resolver, name, "AAAA"),
        )
        chain: set[str] = set()
        addresses: set[_IpAddress] = set()
        settled: set[_Settled] = set()
        for names, found, outcome in results:
            chain |= names
            addresses |= found
            settled.add(outcome)
        return chain, addresses, settled

    async def _address_records(
        self, resolver: _AsyncResolver, name: str, rdtype: str
    ) -> tuple[set[str], set[_IpAddress], _Settled]:
        try:
            answer = await self._resolve(resolver, name, rdtype)
        except Exception as error:
            return set(), set(), "empty" if _is_definite_empty(error) else "error"
        try:
            chain = {_name_text(answer.canonical_name)}
            for rrset in answer.chaining_result.cnames:
                chain.update(_name_text(rdata.target) for rdata in rrset)
            addresses = {ipaddress.ip_address(rdata.address) for rdata in answer}
        except Exception:
            return set(), set(), "error"
        return chain, addresses, "answer"


def domain_resolver() -> DomainResolver:
    """The resolver the domain service uses; tests replace this function."""
    return DnsDomainResolver()
