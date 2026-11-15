"""Host names of public MCP endpoint aliases (#2809).

``utils.public_hostname`` is pure: one normal form for a claimed name, the two
special-use suffix lists (internal callers vs never claimable), the Host
header's name, and the shapes that count as internal callers.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from utils.public_hostname import (
    MAX_HOSTNAME_LENGTH,
    PASS_THROUGH_SUFFIXES,
    RESERVED_SPECIAL_USE,
    host_from_header,
    is_internal_shape,
    is_pass_through_name,
    is_reserved_special_use,
    normalize_hostname,
)

_SOURCE = Path(__file__).resolve().parents[2] / "src" / "utils" / "public_hostname.py"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("mcp.example.com", "mcp.example.com"),
        ("MCP.Example.COM", "mcp.example.com"),
        ("mcp.example.com.", "mcp.example.com"),
        ("  mcp.example.com  ", "mcp.example.com"),
        ("xn--bcher-kva.example.org", "xn--bcher-kva.example.org"),
        ("a-b.example.org", "a-b.example.org"),
        ("1.example.org", "1.example.org"),
        ("a.io", "a.io"),
        ("a" * 63 + ".example.org", "a" * 63 + ".example.org"),
    ],
)
def test_normalize_accepts(raw: str, expected: str) -> None:
    assert normalize_hostname(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "localhost",  # single label
        "example",  # single label
        "mcp.example.com..",  # one trailing dot only
        ".mcp.example.com",  # empty first label
        "mcp..example.com",  # empty label
        "mcp.example.com:443",  # port
        "1.2.3.4",  # IPv4
        "[::1]",  # IPv6
        "::1",
        "fe80::1",
        "a_b.example.com",  # underscore
        "_kagura-public-mcp.example.com",
        "*.example.com",  # wildcard
        "mcp@example.com",
        "mcp.example.com/mcp",
        "mcp example.com",  # inner whitespace
        "mcp.exa\x00mple.com",  # control character
        "mcp.exa\tmple.com",
        "bücher.example.org",  # non-ASCII
        "mcp.example.comK",  # KELVIN SIGN lower-cases to ASCII "k"
        "-mcp.example.com",  # leading hyphen
        "mcp-.example.com",  # trailing hyphen
        "a" * 64 + ".example.org",  # 64-character label
        "example.123",  # numeric last label
        "a.b",  # 3 characters
    ],
)
def test_normalize_refuses(raw: str) -> None:
    assert normalize_hostname(raw) is None


def test_length_bound_is_234() -> None:
    assert MAX_HOSTNAME_LENGTH == 234
    # 234 characters: accepted.
    name = ".".join(["a" * 63, "b" * 63, "c" * 63, "d" * 39]) + ".io"
    assert len(name) == 234
    assert normalize_hostname(name) == name
    # 235 characters: refused.
    longer = ".".join(["a" * 63, "b" * 63, "c" * 63, "d" * 40]) + ".io"
    assert len(longer) == 235
    assert normalize_hostname(longer) is None
    # The trailing dot does not count.
    assert normalize_hostname(name + ".") == name


def test_the_two_suffix_lists() -> None:
    assert PASS_THROUGH_SUFFIXES == (
        "localhost",
        "local",
        "internal",
        "invalid",
        "test",
        "example",
        "home",
        "lan",
        "corp",
    )
    assert RESERVED_SPECIAL_USE == PASS_THROUGH_SUFFIXES + ("arpa", "onion")
    assert "arpa" not in PASS_THROUGH_SUFFIXES
    assert "onion" not in PASS_THROUGH_SUFFIXES


@pytest.mark.parametrize(
    ("name", "pass_through", "reserved"),
    [
        ("x.onion", False, True),
        ("x.ip6.arpa", False, True),
        ("x.1.0.0.2.ip6.arpa", False, True),
        ("4.3.2.1.in-addr.arpa", False, True),
        ("nas.home.arpa", True, True),
        ("home.arpa", True, True),
        ("x.internal", True, True),
        ("db.local", True, True),
        ("app.localhost", True, True),
        ("x.test", True, True),
        ("x.example", True, True),
        ("x.invalid", True, True),
        ("x.home", True, True),
        ("x.lan", True, True),
        ("x.corp", True, True),
        ("mcp.example.com", False, False),
        ("example.org", False, False),
        ("internal.example.com", False, False),
    ],
)
def test_suffix_classification(name: str, pass_through: bool, reserved: bool) -> None:
    assert is_pass_through_name(name) is pass_through
    assert is_reserved_special_use(name) is reserved


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (b"mcp.example.com", "mcp.example.com"),
        (b"MCP.Example.com:8443", "mcp.example.com"),
        (b"mcp.example.com.", "mcp.example.com"),
        (b"mcp.example.com.:443", "mcp.example.com"),
        (b"api-blue:8080", "api-blue"),
        (b"testserver", "testserver"),
        (b"127.0.0.1:8080", "127.0.0.1"),
        (b"[::1]:8080", "[::1]"),
        (b"[::1]", "[::1]"),
        (b"", ""),
    ],
)
def test_host_from_header(value: bytes, expected: str) -> None:
    assert host_from_header(value) == expected


@pytest.mark.parametrize(
    ("host", "internal"),
    [
        ("", True),
        ("testserver", True),
        ("api-blue", True),
        ("127.0.0.1", True),
        ("10.0.0.5", True),
        ("[::1]", True),
        ("[fe80::1]", True),
        ("x.internal", True),
        ("nas.home.arpa", True),
        ("localhost", True),
        ("app.localhost", True),
        ("x.1.0.0.2.ip6.arpa", False),
        ("4.3.2.1.in-addr.arpa", False),
        ("x.onion", False),
        ("mcp.example.com", False),
        ("1.2.3.4.5", False),
    ],
)
def test_internal_shape(host: str, internal: bool) -> None:
    assert is_internal_shape(host) is internal


def test_module_imports_nothing_from_the_application() -> None:
    tree = ast.parse(_SOURCE.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            imported.add((node.module or "").split(".")[0])
    assert imported <= {"__future__", "ipaddress", "re"}, imported
