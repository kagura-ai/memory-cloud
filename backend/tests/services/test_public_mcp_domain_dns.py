"""DNS proof of control and routing check of a public MCP domain (#2809, R6).

The resolver object is a scripted fake: no network. The answers it returns
are real ``dns.resolver.Answer`` objects parsed from response text, so the
adapter is exercised against the library's own types (character-strings,
CNAME chains, address records). Every test that needs the library asks for
the ``dnsmods`` fixture (``pytest.importorskip("dns")``); the structural
tests at the end run without it and show that the module imports and never
names ``dns`` at import time.
"""

from __future__ import annotations

import ast
import inspect
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from models.public_mcp_domain import CHALLENGE_LABEL, CHALLENGE_VALUE_PREFIX
from services import public_mcp_domain_dns as adapter

HOST = "mcp.example.org"
TARGET = "edge.example.com"
TOKEN = "0f" * 32
CHALLENGE_NAME = f"{CHALLENGE_LABEL}.{HOST}"
EXPECTED = f"{CHALLENGE_VALUE_PREFIX}{TOKEN}"


@pytest.fixture(scope="module")
def dnsmods() -> SimpleNamespace:
    pytest.importorskip("dns")
    return SimpleNamespace(
        exception=pytest.importorskip("dns.exception"),
        message=pytest.importorskip("dns.message"),
        name=pytest.importorskip("dns.name"),
        rdataclass=pytest.importorskip("dns.rdataclass"),
        rdatatype=pytest.importorskip("dns.rdatatype"),
        resolver=pytest.importorskip("dns.resolver"),
        asyncresolver=pytest.importorskip("dns.asyncresolver"),
    )


def _answer(dnsmods: SimpleNamespace, qname: str, rdtype: str, *records: str) -> Any:
    """A real ``Answer`` for ``qname``/``rdtype`` whose answer section is ``records``."""
    text = "\n".join(
        [
            "id 1",
            "opcode QUERY",
            "rcode NOERROR",
            "flags QR RD RA",
            ";QUESTION",
            f"{qname}. IN {rdtype}",
            ";ANSWER",
            *records,
        ]
    )
    response = dnsmods.message.from_text(text)
    return dnsmods.resolver.Answer(
        dnsmods.name.from_text(qname),
        dnsmods.rdatatype.from_text(rdtype),
        dnsmods.rdataclass.IN,
        response,
    )


def _txt(dnsmods: SimpleNamespace, *values: str) -> Any:
    """A TXT answer at the challenge name; each value is the RDATA text."""
    return _answer(
        dnsmods,
        CHALLENGE_NAME,
        "TXT",
        *(f"{CHALLENGE_NAME}. 300 IN TXT {value}" for value in values),
    )


class FakeResolver:
    """Scripted stand-in for ``dns.asyncresolver.Resolver``.

    ``script`` maps ``(name, rdtype)`` to an ``Answer`` (returned) or an
    exception (raised). An unscripted question is NXDOMAIN.
    """

    def __init__(self, dnsmods: SimpleNamespace, script: dict[tuple[str, str], Any]) -> None:
        self._dnsmods = dnsmods
        self.script = script
        self.calls: list[tuple[str, str, float, bool]] = []

    async def resolve(self, qname: str, rdtype: str, *, lifetime: float, search: bool) -> Any:
        self.calls.append((str(qname), str(rdtype), lifetime, search))
        outcome = self.script.get((str(qname).rstrip(".").lower(), str(rdtype)))
        if outcome is None:
            raise self._dnsmods.resolver.NXDOMAIN()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


async def _check(
    dnsmods: SimpleNamespace,
    script: dict[tuple[str, str], Any],
    *,
    routing_target: str = TARGET,
) -> tuple[adapter.DomainCheck, FakeResolver]:
    fake = FakeResolver(dnsmods, script)
    resolver = adapter.DnsDomainResolver(resolver_factory=lambda: fake)
    result = await resolver.check(HOST, TOKEN, routing_target)
    return result, fake


def _routed(dnsmods: SimpleNamespace) -> dict[tuple[str, str], Any]:
    """Name and target share an address: routing ``ok``."""
    return {
        (HOST, "A"): _answer(dnsmods, HOST, "A", f"{HOST}. 300 IN A 192.0.2.10"),
        (TARGET, "A"): _answer(dnsmods, TARGET, "A", f"{TARGET}. 300 IN A 192.0.2.10"),
    }


# --- proof ---------------------------------------------------------------


async def test_exact_value_proves_control(dnsmods: SimpleNamespace) -> None:
    script = _routed(dnsmods)
    script[(CHALLENGE_NAME, "TXT")] = _txt(dnsmods, f'"{EXPECTED}"')

    result, fake = await _check(dnsmods, script)

    assert result == adapter.DomainCheck("ok", "ok")
    # The challenge name is asked for TXT once, with the per-query lifetime
    # and without the search list.
    txt_calls = [call for call in fake.calls if call[1] == "TXT"]
    assert txt_calls == [(CHALLENGE_NAME, "TXT", 3.0, False)]
    assert all(call[2] == 3.0 and call[3] is False for call in fake.calls)


async def test_value_split_into_character_strings_is_concatenated(
    dnsmods: SimpleNamespace,
) -> None:
    half = len(EXPECTED) // 2
    script = _routed(dnsmods)
    script[(CHALLENGE_NAME, "TXT")] = _txt(dnsmods, f'"{EXPECTED[:half]}" "{EXPECTED[half:]}"')

    result, _ = await _check(dnsmods, script)

    assert result.proof == "ok"


async def test_one_matching_value_among_others_is_enough(dnsmods: SimpleNamespace) -> None:
    script = _routed(dnsmods)
    script[(CHALLENGE_NAME, "TXT")] = _txt(
        dnsmods, '"v=spf1 -all"', f'"{CHALLENGE_VALUE_PREFIX}{"1" * 64}"', f'"{EXPECTED}"'
    )

    result, _ = await _check(dnsmods, script)

    assert result.proof == "ok"


@pytest.mark.parametrize(
    "value",
    [
        f'"{CHALLENGE_VALUE_PREFIX}{"1" * 64}"',  # another token
        f'"{EXPECTED.upper()}"',  # not the exact value
        f'"{EXPECTED} "',  # trailing space
        f'"{EXPECTED}0"',  # longer
        f'"{TOKEN}"',  # token without the prefix
        f'" {EXPECTED}"',  # leading space
    ],
)
async def test_any_other_value_is_missing(dnsmods: SimpleNamespace, value: str) -> None:
    script = _routed(dnsmods)
    script[(CHALLENGE_NAME, "TXT")] = _txt(dnsmods, value)

    result, _ = await _check(dnsmods, script)

    assert result.proof == "missing"


async def test_only_the_first_twenty_values_are_read(dnsmods: SimpleNamespace) -> None:
    others = [f'"filler-{index}"' for index in range(20)]
    script = _routed(dnsmods)
    script[(CHALLENGE_NAME, "TXT")] = _txt(dnsmods, *others, f'"{EXPECTED}"')

    result, _ = await _check(dnsmods, script)

    assert result.proof == "missing"


async def test_the_twentieth_value_is_still_read(dnsmods: SimpleNamespace) -> None:
    others = [f'"filler-{index}"' for index in range(19)]
    script = _routed(dnsmods)
    script[(CHALLENGE_NAME, "TXT")] = _txt(dnsmods, *others, f'"{EXPECTED}"')

    result, _ = await _check(dnsmods, script)

    assert result.proof == "ok"


async def test_values_reached_through_a_cname_at_the_challenge_name(
    dnsmods: SimpleNamespace,
) -> None:
    delegated = "proof.example.net"
    script = _routed(dnsmods)
    script[(CHALLENGE_NAME, "TXT")] = _answer(
        dnsmods,
        CHALLENGE_NAME,
        "TXT",
        f"{CHALLENGE_NAME}. 300 IN CNAME {delegated}.",
        f'{delegated}. 300 IN TXT "{EXPECTED}"',
    )

    result, _ = await _check(dnsmods, script)

    assert result.proof == "ok"


@pytest.mark.parametrize("error", ["NXDOMAIN", "NoAnswer"])
async def test_no_record_is_missing(dnsmods: SimpleNamespace, error: str) -> None:
    script = _routed(dnsmods)
    script[(CHALLENGE_NAME, "TXT")] = getattr(dnsmods.resolver, error)()

    result, _ = await _check(dnsmods, script)

    assert result.proof == "missing"
    assert result.routing == "ok"


@pytest.mark.parametrize(
    "make_error",
    [
        pytest.param(lambda d: d.resolver.LifetimeTimeout(timeout=3.0, errors=[]), id="timeout"),
        pytest.param(lambda d: d.exception.Timeout(timeout=3.0), id="plain-timeout"),
        pytest.param(lambda d: d.resolver.NoNameservers(), id="servfail-no-nameservers"),
        pytest.param(lambda d: d.resolver.YXDOMAIN(), id="yxdomain"),
        pytest.param(lambda d: d.exception.DNSException(), id="other-dns-error"),
        pytest.param(lambda d: OSError("network unreachable"), id="os-error"),
    ],
)
async def test_indeterminate_lookup_is_unavailable(dnsmods: SimpleNamespace, make_error) -> None:
    script = _routed(dnsmods)
    script[(CHALLENGE_NAME, "TXT")] = make_error(dnsmods)

    result, _ = await _check(dnsmods, script)

    assert result.proof == "unavailable"


async def test_no_resolver_configuration_is_unavailable(dnsmods: SimpleNamespace) -> None:
    def no_configuration() -> Any:
        raise dnsmods.resolver.NoResolverConfiguration()

    resolver = adapter.DnsDomainResolver(resolver_factory=no_configuration)

    result = await resolver.check(HOST, TOKEN, TARGET)

    assert result == adapter.DomainCheck("unavailable", "unresolved")


# --- routing -------------------------------------------------------------


async def test_routing_ok_by_cname_chain(dnsmods: SimpleNamespace) -> None:
    middle = "alias.example.net"
    script: dict[tuple[str, str], Any] = {
        (CHALLENGE_NAME, "TXT"): _txt(dnsmods, f'"{EXPECTED}"'),
        (HOST, "A"): _answer(
            dnsmods,
            HOST,
            "A",
            f"{HOST}. 300 IN CNAME {middle}.",
            f"{middle}. 300 IN CNAME {TARGET.upper()}.",
            f"{TARGET}. 300 IN A 192.0.2.10",
        ),
        # The target's own lookup fails: the chain alone decides.
        (TARGET, "A"): dnsmods.resolver.NoNameservers(),
        (TARGET, "AAAA"): dnsmods.resolver.NoNameservers(),
    }

    result, _ = await _check(dnsmods, script)

    assert result == adapter.DomainCheck("ok", "ok")


async def test_routing_ok_by_cname_chain_on_aaaa_only(dnsmods: SimpleNamespace) -> None:
    script: dict[tuple[str, str], Any] = {
        (CHALLENGE_NAME, "TXT"): _txt(dnsmods, f'"{EXPECTED}"'),
        (HOST, "A"): dnsmods.resolver.NoAnswer(),
        (HOST, "AAAA"): _answer(
            dnsmods,
            HOST,
            "AAAA",
            f"{HOST}. 300 IN CNAME {TARGET}.",
            f"{TARGET}. 300 IN AAAA 2001:db8::10",
        ),
    }

    result, _ = await _check(dnsmods, script)

    assert result.routing == "ok"


async def test_routing_ok_by_address_intersection(dnsmods: SimpleNamespace) -> None:
    script: dict[tuple[str, str], Any] = {
        (CHALLENGE_NAME, "TXT"): _txt(dnsmods, f'"{EXPECTED}"'),
        (HOST, "A"): _answer(dnsmods, HOST, "A", f"{HOST}. 300 IN A 192.0.2.20"),
        (HOST, "AAAA"): _answer(dnsmods, HOST, "AAAA", f"{HOST}. 300 IN AAAA 2001:db8:0:0::10"),
        (TARGET, "A"): _answer(dnsmods, TARGET, "A", f"{TARGET}. 300 IN A 192.0.2.10"),
        (TARGET, "AAAA"): _answer(dnsmods, TARGET, "AAAA", f"{TARGET}. 300 IN AAAA 2001:db8::10"),
    }

    result, _ = await _check(dnsmods, script)

    assert result.routing == "ok"


async def test_routing_mismatch_when_both_resolve_elsewhere(dnsmods: SimpleNamespace) -> None:
    script: dict[tuple[str, str], Any] = {
        (CHALLENGE_NAME, "TXT"): _txt(dnsmods, f'"{EXPECTED}"'),
        (HOST, "A"): _answer(
            dnsmods,
            HOST,
            "A",
            f"{HOST}. 300 IN CNAME other.example.net.",
            "other.example.net. 300 IN A 198.51.100.7",
        ),
        (TARGET, "A"): _answer(dnsmods, TARGET, "A", f"{TARGET}. 300 IN A 192.0.2.10"),
    }

    result, _ = await _check(dnsmods, script)

    assert result == adapter.DomainCheck("ok", "mismatch")


@pytest.mark.parametrize(
    "script_name, script_target",
    [
        pytest.param(False, True, id="name-does-not-resolve"),
        pytest.param(True, False, id="target-does-not-resolve"),
        pytest.param(False, False, id="neither-resolves"),
    ],
)
async def test_routing_unresolved(
    dnsmods: SimpleNamespace, script_name: bool, script_target: bool
) -> None:
    script: dict[tuple[str, str], Any] = {
        (CHALLENGE_NAME, "TXT"): _txt(dnsmods, f'"{EXPECTED}"'),
    }
    if script_name:
        script[(HOST, "A")] = _answer(dnsmods, HOST, "A", f"{HOST}. 300 IN A 198.51.100.7")
    else:
        script[(HOST, "A")] = dnsmods.resolver.LifetimeTimeout(timeout=3.0, errors=[])
    if script_target:
        script[(TARGET, "A")] = _answer(dnsmods, TARGET, "A", f"{TARGET}. 300 IN A 192.0.2.10")

    result, _ = await _check(dnsmods, script)

    assert result == adapter.DomainCheck("ok", "unresolved")


async def test_routing_unresolved_without_a_target_and_no_query_for_it(
    dnsmods: SimpleNamespace,
) -> None:
    script = _routed(dnsmods)
    script[(CHALLENGE_NAME, "TXT")] = _txt(dnsmods, f'"{EXPECTED}"')

    result, fake = await _check(dnsmods, script, routing_target="")

    assert result == adapter.DomainCheck("ok", "unresolved")
    assert {call[0] for call in fake.calls} == {CHALLENGE_NAME}


async def test_routing_never_decides_the_proof(dnsmods: SimpleNamespace) -> None:
    script: dict[tuple[str, str], Any] = {(CHALLENGE_NAME, "TXT"): _txt(dnsmods, f'"{EXPECTED}"')}

    result, _ = await _check(dnsmods, script)

    assert result == adapter.DomainCheck("ok", "unresolved")


# --- the default resolver ------------------------------------------------


def test_domain_resolver_returns_the_dns_resolver() -> None:
    assert isinstance(adapter.domain_resolver(), adapter.DnsDomainResolver)


def test_system_resolver_uses_the_system_configuration_without_a_cache(
    dnsmods: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    class RecordingResolver:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            built.append((args, kwargs))
            self.cache = object()
            self.lifetime = 30.0

    monkeypatch.setattr(dnsmods.asyncresolver, "Resolver", RecordingResolver)

    resolver = adapter._system_resolver()

    # Constructed with the defaults: configured from the system files.
    assert built == [((), {})]
    assert resolver.cache is None
    assert resolver.lifetime == 3.0


async def test_a_new_resolver_is_built_for_every_check(dnsmods: SimpleNamespace) -> None:
    built: list[FakeResolver] = []

    def factory() -> FakeResolver:
        fake = FakeResolver(dnsmods, {(CHALLENGE_NAME, "TXT"): _txt(dnsmods, f'"{EXPECTED}"')})
        built.append(fake)
        return fake

    resolver = adapter.DnsDomainResolver(resolver_factory=factory)
    await resolver.check(HOST, TOKEN, TARGET)
    await resolver.check(HOST, TOKEN, TARGET)

    assert len(built) == 2


# --- structure (runs without the library) --------------------------------


def _module_source() -> str:
    return inspect.getsource(adapter)


def test_module_imports_only_the_standard_library_and_host_name_rules() -> None:
    tree = ast.parse(_module_source())
    allowed_local = {"utils.public_hostname"}
    for node in tree.body:
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = [node.module or ""]
        else:
            continue
        for module in modules:
            top = module.split(".", 1)[0]
            assert (
                top in sys.stdlib_module_names or module in allowed_local or top == "__future__"
            ), module


def test_dns_is_imported_inside_functions_only_and_without_ignore_comments() -> None:
    source = _module_source()
    tree = ast.parse(source)
    module_level = {id(node) for node in tree.body}
    dns_imports = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Import) and any(a.name.split(".")[0] == "dns" for a in node.names)
    ]
    assert dns_imports, "the resolver imports dns on first use"
    assert all(id(node) not in module_level for node in dns_imports)
    assert "type: ignore" not in source
    assert "pyright: ignore" not in source
