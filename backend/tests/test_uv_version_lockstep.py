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

Every workflow job that sets up Python must also set up the pinned uv, every
``uv sync`` must be ``--locked``, and the lock itself must be tracked (git does
not ignore it) and resolved for the same ``requires-python`` as
``pyproject.toml``.
"""

import re
import subprocess
import tomllib
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

# backend/tests/test_uv_version_lockstep.py -> tests -> backend -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[2]

_WORKFLOWS = [".github/workflows/ci.yml", ".github/workflows/eval-nightly.yml"]
_INSTALL_SITES = [*_WORKFLOWS, "backend/Dockerfile", "setup.sh"]

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


def _jobs(rel: str) -> dict[str, list[dict]]:
    """``job name -> steps`` for a workflow file."""
    workflow = yaml.safe_load(_read(rel))
    return {name: job.get("steps") or [] for name, job in workflow["jobs"].items()}


def _uses(step: dict, action: str) -> bool:
    return str(step.get("uses", "")).split("@")[0] == action


def setup_uv_versions(rel: str) -> list[str]:
    """Every ``version:`` given to ``astral-sh/setup-uv`` in a workflow file."""
    return [
        str((step.get("with") or {}).get("version", ""))
        for steps in _jobs(rel).values()
        for step in steps
        if _uses(step, "astral-sh/setup-uv")
    ]


def dockerfile_uv_version() -> str:
    match = _DOCKER_UV_IMAGE.search(_read("backend/Dockerfile"))
    assert match, "backend/Dockerfile does not copy uv from ghcr.io/astral-sh/uv:<version>"
    return match.group(1)


def setup_sh_uv_version() -> str:
    match = _SETUP_SH_PIN.search(_read("setup.sh"))
    assert match, 'setup.sh does not pin the uv it installs (UV_REQUIRED="<version>")'
    return match.group(1)


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


def test_every_python_job_sets_up_the_pinned_uv() -> None:
    """Per job: a step that sets up Python is paired with a pinned setup-uv step."""
    for rel in _WORKFLOWS:
        for name, steps in _jobs(rel).items():
            has_python = any(_uses(step, "actions/setup-python") for step in steps)
            has_uv = any(_uses(step, "astral-sh/setup-uv") for step in steps)
            assert has_uv or not has_python, f"{rel}: job {name!r} sets up Python without setup-uv"
            for step in steps:
                run = str(step.get("run", ""))
                assert "pip install -e" not in run, (
                    f"{rel}: job {name!r} still installs from ranges (pip install -e)"
                )


def _command_lines(rel: str) -> list[str]:
    """Non-comment lines of a file (a ``# ... uv sync`` remark is prose, not a command)."""
    return [
        line
        for line in _read(rel).splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_locked_installs_everywhere() -> None:
    """CI and the image never resolve afresh: every uv sync is --locked."""
    for rel in _INSTALL_SITES:
        syncs = [line for line in _command_lines(rel) if re.search(r"\buv sync\b", line)]
        assert syncs, f"{rel}: no `uv sync` found"
        unlocked = [line.strip() for line in syncs if "--locked" not in line]
        assert unlocked == [], f"{rel}: uv sync without --locked: {unlocked}"


def test_lock_is_tracked_and_current_python() -> None:
    """uv.lock exists, git does not ignore it, and it matches pyproject's requires-python."""
    lock_path = _REPO_ROOT / "backend" / "uv.lock"
    assert lock_path.is_file(), "backend/uv.lock is missing — run `cd backend && uv lock`"
    # git is the authority on ignore rules; exit 1 means "not ignored".
    check = subprocess.run(
        ["git", "check-ignore", "-q", "backend/uv.lock"], cwd=_REPO_ROOT, check=False
    )
    assert check.returncode == 1, "git ignores backend/uv.lock — the lock must be tracked"
    lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    assert lock["requires-python"] == _pyproject()["project"]["requires-python"]
