"""Guard tests for #1625: every version string a release bumps equals ``APP_VERSION``.

``/release`` (``.claude/commands/release.md``) bumps seven files and prepends a
``CHANGELOG.md`` entry in one commit. Two guards already cover part of that set:

* ``tests/test_codex_plugin_manifest.py`` — Codex plugin manifest ==
  ``APP_VERSION``, and Codex == Claude plugin manifest.
* ``frontend/src/lib/version.test.ts`` — frontend ``APP_VERSION`` ==
  ``frontend/package.json``.

This module pins the rest, so a release that bumps one file and forgets another
fails in CI instead of shipping a mixed version:

1. ``backend/pyproject.toml`` ``[project].version``, ``backend/src/__init__.py``
   ``__version__``, ``frontend/package.json``, both version fields of
   ``frontend/package-lock.json`` and both plugin manifests equal
   ``APP_VERSION`` (the canonical runtime source in ``config.constants``).
2. The first ``## [vX.Y.Z]`` heading in ``CHANGELOG.md`` names ``v{APP_VERSION}``
   and carries an ISO ``YYYY-MM-DD`` date.
"""

import json
import re
import tomllib
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest

from config.constants import APP_VERSION

# backend/tests/test_release_version_lockstep.py -> tests -> backend -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[2]

# ``## [vX.Y.Z](<release URL>) — YYYY-MM-DD`` — the heading shape of every entry.
_CHANGELOG_HEADING = re.compile(
    r"^## \[(?P<tag>v\d+\.\d+\.\d+)\]\([^)]*\) — (?P<date>\d{4}-\d{2}-\d{2})$",
    re.MULTILINE,
)


def _read(rel: str) -> str:
    return (_REPO_ROOT / rel).read_text(encoding="utf-8")


def _json_field(rel: str, *keys: str) -> str:
    node = json.loads(_read(rel))
    for key in keys:
        node = node[key]
    return node


def _pyproject_version() -> str:
    return tomllib.loads(_read("backend/pyproject.toml"))["project"]["version"]


def _dunder_version() -> str:
    # ``backend/src/__init__.py`` is not importable under ``pythonpath = src``
    # (it is the directory's own init), so read the assignment textually.
    match = re.search(r'^__version__ = "([^"]+)"', _read("backend/src/__init__.py"), re.MULTILINE)
    assert match, "backend/src/__init__.py has no __version__ assignment"
    return match.group(1)


_VERSION_SOURCES: list[tuple[str, Callable[[], str]]] = [
    ("backend/pyproject.toml [project].version", _pyproject_version),
    ("backend/src/__init__.py __version__", _dunder_version),
    ("frontend/package.json .version", lambda: _json_field("frontend/package.json", "version")),
    (
        "frontend/package-lock.json .version",
        lambda: _json_field("frontend/package-lock.json", "version"),
    ),
    (
        'frontend/package-lock.json .packages[""].version',
        lambda: _json_field("frontend/package-lock.json", "packages", "", "version"),
    ),
    (
        ".claude-plugin/plugin.json .version",
        lambda: _json_field(".claude-plugin/plugin.json", "version"),
    ),
    (
        "plugins/kagura-memory/.codex-plugin/plugin.json .version",
        lambda: _json_field("plugins/kagura-memory/.codex-plugin/plugin.json", "version"),
    ),
]


@pytest.mark.parametrize(
    ("label", "read_version"), _VERSION_SOURCES, ids=[label for label, _ in _VERSION_SOURCES]
)
def test_version_file_matches_app_version(label: str, read_version: Callable[[], str]) -> None:
    """Each file the release bumps carries exactly ``APP_VERSION``."""
    assert read_version() == APP_VERSION, f"{label} is out of lockstep with APP_VERSION"


def test_changelog_top_entry_is_current_release() -> None:
    """The newest ``CHANGELOG.md`` heading names ``v{APP_VERSION}`` with a ``YYYY-MM-DD`` date."""
    match = _CHANGELOG_HEADING.search(_read("CHANGELOG.md"))
    assert match, "CHANGELOG.md has no '## [vX.Y.Z](url) — YYYY-MM-DD' heading"
    assert match.group("tag") == f"v{APP_VERSION}", (
        f"CHANGELOG.md top entry is {match.group('tag')}, APP_VERSION is {APP_VERSION}"
    )
    # ``fromisoformat`` rejects a well-shaped but impossible date such as 2026-13-40.
    date.fromisoformat(match.group("date"))
