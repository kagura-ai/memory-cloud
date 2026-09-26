"""Guard tests for #1706: the uv pin and the tracked lock stay in lockstep.

``backend/uv.lock`` is what CI, ``backend/Dockerfile`` and ``setup.sh`` install
(``uv sync --locked``). uv's own version is pinned in four places, and a
mismatch is not cosmetic: a newer uv rewrites the lock in a format the pinned
one may not read, and a different resolver makes ``uv lock --check`` disagree
across machines. So every site must carry the one version that
``[tool.uv] required-version`` in ``backend/pyproject.toml`` names:

1. ``astral-sh/setup-uv`` ``version:`` in ``.github/workflows/ci.yml`` and
   ``.github/workflows/eval-nightly.yml`` (every occurrence),
2. the ``ghcr.io/astral-sh/uv:<version>`` image stage in ``backend/Dockerfile``,
3. the ``UV_REQUIRED="<version>"`` the standalone installer pins in ``setup.sh``.

The lock itself must be tracked (not ignored by ``.gitignore``) and resolved
for the same ``requires-python`` as ``pyproject.toml``.
"""

import re
import tomllib
from collections.abc import Callable
from pathlib import Path

import pytest

# backend/tests/test_uv_version_lockstep.py -> tests -> backend -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[2]

_SETUP_UV_VERSION = re.compile(
    r"uses:\s*astral-sh/setup-uv@[^\n]*\n(?:[ \t]*if:[^\n]*\n)?\s*with:\s*\n\s*version:\s*\"([^\"]+)\"",
    re.MULTILINE,
)
_DOCKER_UV_IMAGE = re.compile(r"ghcr\.io/astral-sh/uv:([0-9][^\s/]*)")
_SETUP_SH_PIN = re.compile(r'^UV_REQUIRED="([0-9][^"]*)"', re.MULTILINE)


def _read(rel: str) -> str:
    return (_REPO_ROOT / rel).read_text(encoding="utf-8")


def _pyproject() -> dict:
    return tomllib.loads(_read("backend/pyproject.toml"))


def required_uv_version() -> str:
    """The canonical uv version: ``[tool.uv] required-version`` without its ``==``."""
    spec = _pyproject()["tool"]["uv"]["required-version"]
    assert spec.startswith("=="), f"required-version must pin one exact version, got {spec!r}"
    return spec[2:]


def setup_uv_versions(rel: str) -> list[str]:
    """Every ``version:`` given to ``astral-sh/setup-uv`` in a workflow file."""
    return _SETUP_UV_VERSION.findall(_read(rel))


def dockerfile_uv_version() -> str:
    match = _DOCKER_UV_IMAGE.search(_read("backend/Dockerfile"))
    assert match, "backend/Dockerfile does not copy uv from ghcr.io/astral-sh/uv:<version>"
    return match.group(1)


def setup_sh_uv_version() -> str:
    match = _SETUP_SH_PIN.search(_read("setup.sh"))
    assert match, 'setup.sh does not pin the uv it installs (UV_REQUIRED="<version>")'
    return match.group(1)


_WORKFLOWS = [".github/workflows/ci.yml", ".github/workflows/eval-nightly.yml"]

_VERSION_SITES: list[tuple[str, Callable[[], list[str]]]] = [
    *[(f"{rel} setup-uv version", (lambda rel=rel: setup_uv_versions(rel))) for rel in _WORKFLOWS],
    ("backend/Dockerfile uv image tag", lambda: [dockerfile_uv_version()]),
    ("setup.sh UV_REQUIRED pin", lambda: [setup_sh_uv_version()]),
]


@pytest.mark.parametrize(
    ("label", "read_versions"), _VERSION_SITES, ids=[label for label, _ in _VERSION_SITES]
)
def test_uv_pin_matches_required_version(
    label: str, read_versions: Callable[[], list[str]]
) -> None:
    """Each site pins exactly the uv version pyproject.toml requires."""
    versions = read_versions()
    assert versions, f"{label}: no uv version found"
    assert set(versions) == {required_uv_version()}, (
        f"{label} is {versions}, out of lockstep with required-version {required_uv_version()}"
    )


def test_every_python_job_pins_uv() -> None:
    """A workflow job that sets up Python also sets up the pinned uv (no unpinned installs)."""
    for rel in _WORKFLOWS:
        text = _read(rel)
        assert "pip install -e" not in text, f"{rel} still installs from ranges (pip install -e)"
        assert text.count("actions/setup-python@") == len(setup_uv_versions(rel)), (
            f"{rel}: every actions/setup-python step needs a pinned astral-sh/setup-uv step"
        )


def test_lock_is_tracked_and_current_python() -> None:
    """uv.lock exists, is not ignored, and was resolved for pyproject's requires-python."""
    lock_path = _REPO_ROOT / "backend" / "uv.lock"
    assert lock_path.is_file(), "backend/uv.lock is missing — run `cd backend && uv lock`"
    ignored = [
        line.strip()
        for line in _read(".gitignore").splitlines()
        if line.strip() in {"backend/uv.lock", "uv.lock", "*.lock"}
    ]
    assert ignored == [], f".gitignore ignores the lock: {ignored}"
    lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    assert lock["requires-python"] == _pyproject()["project"]["requires-python"]


def test_locked_installs_everywhere() -> None:
    """CI and the image never resolve afresh: every uv sync is --locked."""
    for rel in [*_WORKFLOWS, "backend/Dockerfile", "setup.sh"]:
        text = _read(rel)
        # Command lines only: a backticked `uv sync` in a comment is prose.
        syncs = re.findall(r"(?<!`)uv sync[^\n`]*(?!`)", text)
        assert syncs, f"{rel}: no `uv sync` found"
        unlocked = [line for line in syncs if "--locked" not in line]
        assert unlocked == [], f"{rel}: uv sync without --locked: {unlocked}"
