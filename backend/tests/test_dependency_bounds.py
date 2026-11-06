"""Guard test for #1705: auth-critical dependencies carry an upper bound.

CI and the Docker image install the tracked ``backend/uv.lock`` (#1706), so a
new release reaches them only through a lock refresh — Dependabot's weekly
lock PR or a manual ``uv lock``. The bound is what such a refresh
may move within: without it, a refresh could pull the next major of a library
that sits on an authentication, token, session or crypto path without anyone
deciding to. The reason for each bound is next to it in ``pyproject.toml``.
"""

import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

# backend/tests/test_dependency_bounds.py -> tests -> backend
_PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"

AUTH_CRITICAL = frozenset(
    {
        "authlib",
        "bcrypt",
        "cryptography",
        "google-auth",
        "google-auth-oauthlib",
        "joserfc",
        "mcp",
        "pyotp",
        "starlette",
    }
)

# Operators that cap a range from above (``~=`` implies ``<`` the next release).
_UPPER_BOUND_OPERATORS = frozenset({"<", "<=", "==", "===", "~="})


def _project() -> dict:
    return tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))["project"]


def _runtime_requirements() -> list[Requirement]:
    """``[project].dependencies``: what a plain ``pip install .`` installs, with no extra."""
    return [Requirement(line) for line in _project()["dependencies"]]


def _declared_requirements() -> list[Requirement]:
    """Every requirement in ``[project].dependencies`` and each optional group."""
    lines: list[str] = []
    for group in _project().get("optional-dependencies", {}).values():
        lines.extend(group)
    return _runtime_requirements() + [Requirement(line) for line in lines]


def _auth_critical() -> list[Requirement]:
    return [r for r in _declared_requirements() if canonicalize_name(r.name) in AUTH_CRITICAL]


def test_every_auth_critical_dependency_is_a_runtime_dependency() -> None:
    """A renamed or dropped line must fail here, not silently skip the bound check.

    Runtime code imports every one of these, so a declaration that only an
    optional group (e.g. ``dev``) carries does not count.
    """
    runtime = {canonicalize_name(r.name) for r in _runtime_requirements()}
    assert AUTH_CRITICAL <= runtime, sorted(AUTH_CRITICAL - runtime)


@pytest.mark.parametrize("requirement", _auth_critical(), ids=lambda r: r.name)
def test_auth_critical_dependency_has_upper_bound(requirement: Requirement) -> None:
    operators = {spec.operator for spec in requirement.specifier}
    assert operators & _UPPER_BOUND_OPERATORS, (
        f"{requirement} has no upper bound; see the #1705 comments in pyproject.toml"
    )


@pytest.mark.parametrize(
    ("version", "allowed"),
    [
        # #1701 overrides Authlib's PKCE and grant hooks; verified on 1.9.1 (#2449).
        ("1.9.1", True),
        ("1.9.9", True),
        # The floor is a security floor (#2449): 1.9.0 still has
        # GHSA-8m4g-j4wp-wgcg (empty HMAC key), 1.8 and older the other
        # advisories. (Before 1.8 the get_allowed_scope / PKCE behaviour also
        # differs from what the overrides expect, #1701.)
        ("1.9.0", False),
        ("1.8.0", False),
        ("1.7.9", False),
        # The next minor, including its pre-releases, needs a deliberate bump.
        ("1.10.0rc1", False),
        ("1.10.0", False),
        ("2.0.0", False),
    ],
)
def test_authlib_is_pinned_to_the_verified_minor(version: str, allowed: bool) -> None:
    (authlib,) = [r for r in _runtime_requirements() if canonicalize_name(r.name) == "authlib"]
    assert authlib.specifier.contains(version, prereleases=True) is allowed


# GHSA-3jp2-3mrg-2vxc (#2674): oauthlib's RFC 8628 server grant skips the
# device_code check in the range below. Nothing under src/ imports oauthlib; it
# arrives through google-auth-oauthlib -> requests-oauthlib, so the floor lives
# in pyproject.toml as a security floor and the lock is checked against it.
_OAUTHLIB_PATCHED = "4.0.1"
_UV_LOCK = Path(__file__).resolve().parents[1] / "uv.lock"


def _oauthlib_requirement() -> Requirement:
    (oauthlib,) = [r for r in _runtime_requirements() if canonicalize_name(r.name) == "oauthlib"]
    return oauthlib


@pytest.mark.parametrize(
    ("version", "allowed"),
    [
        ("4.0.1", True),
        ("4.1.0", True),
        # The advisory range is >= 3.3.0, < 4.0.1; older releases are below the floor too.
        ("4.0.0", False),
        ("3.3.1", False),
        ("3.3.0", False),
        ("3.2.2", False),
    ],
)
def test_oauthlib_floor_excludes_the_vulnerable_range(version: str, allowed: bool) -> None:
    assert _oauthlib_requirement().specifier.contains(version, prereleases=True) is allowed


def test_locked_oauthlib_is_patched() -> None:
    """The tracked lock is what CI and the image install, so it must not resolve below the floor.

    A lock may resolve a package once per Python-version fork (numpy and
    networkx already do), so every locked oauthlib entry is checked.
    """
    lock = tomllib.loads(_UV_LOCK.read_text(encoding="utf-8"))
    locked = [p["version"] for p in lock["package"] if canonicalize_name(p["name"]) == "oauthlib"]
    assert locked, "uv.lock resolves no oauthlib"
    floor = _oauthlib_requirement().specifier
    below = [
        v
        for v in locked
        if not floor.contains(v, prereleases=True) or Version(v) < Version(_OAUTHLIB_PATCHED)
    ]
    assert not below, below
