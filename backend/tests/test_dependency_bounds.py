"""Guard test for #1705: auth-critical dependencies carry an upper bound.

CI and the Docker image install the tracked ``backend/uv.lock`` (#1706), so a
new release reaches them only through a lock refresh — Renovate's weekly
lock-maintenance PR or a manual ``uv lock``. The bound is what such a refresh
may move within: without it, a refresh could pull the next major of a library
that sits on an authentication, token, session or crypto path without anyone
deciding to. The reason for each bound is next to it in ``pyproject.toml``.
"""

import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

# backend/tests/test_dependency_bounds.py -> tests -> backend
_PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"

AUTH_CRITICAL = frozenset(
    {
        "authlib",
        "bcrypt",
        "cryptography",
        "google-auth",
        "google-auth-oauthlib",
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
        # #1701 overrides Authlib's PKCE and grant hooks; verified on 1.8.
        ("1.8.0", True),
        ("1.8.9", True),
        # Before 1.8: get_allowed_scope / PKCE behaviour the overrides do not expect.
        ("1.7.9", False),
        # The next minor, including its pre-releases, needs a deliberate bump.
        ("1.9.0rc1", False),
        ("1.9.0", False),
        ("2.0.0", False),
    ],
)
def test_authlib_is_pinned_to_the_verified_minor(version: str, allowed: bool) -> None:
    (authlib,) = [r for r in _runtime_requirements() if canonicalize_name(r.name) == "authlib"]
    assert authlib.specifier.contains(version, prereleases=True) is allowed
