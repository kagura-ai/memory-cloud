"""Guard tests for #2523: a local ``make type-check`` resolves what CI's does.

pyright stops analysing a module whose imports it cannot resolve, so CI's
``type-check`` job installs every optional extra (its comment says why). A
local venv built with ``--extra dev`` alone reported the provider packages as
"could not be resolved" on a clean ``main``. So:

- ``make install`` builds ``backend/.venv`` from CI's set of extras minus
  ``_LOCAL_SKIP``;
- ``make type-check`` runs pyright against that venv's interpreter. Without
  ``--pythonpath`` pyright analyses against whichever ``python`` is first on
  PATH, not the venv it was installed into.

``neural`` is the one extra left out: about 2.3 GB of torch/CUDA wheels, and
nothing pyright checks imports it. The last test keeps that true.
"""

import ast
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

# backend/tests/ci/test_type_check_env.py -> ci -> tests -> backend -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[3]

# As in test_uv_version_lockstep.py: the workflows and the Makefile are not
# copied into the image, so the guard runs from a repository checkout.
pytestmark = pytest.mark.skipif(
    not (_REPO_ROOT / ".github" / "workflows" / "ci.yml").is_file(),
    reason="not a repository checkout (e.g. tests mounted into the api container)",
)

# Extras CI type-checks with that `make install` leaves out.
_LOCAL_SKIP = {"neural"}

_EXTRA_FLAG = re.compile(r"--extra[ =](\S+)")


def _read(rel: str) -> str:
    return (_REPO_ROOT / rel).read_text(encoding="utf-8")


def _norm(name: str) -> str:
    """PEP 503 / PEP 685 name normalisation (uv writes ``llm-providers``)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _uv_sync_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if re.search(r"\buv sync\b", line)]


def _extras(line: str) -> set[str]:
    return {_norm(extra) for extra in _EXTRA_FLAG.findall(line)}


def _pyproject_extras() -> set[str]:
    pyproject = tomllib.loads(_read("backend/pyproject.toml"))
    return {_norm(extra) for extra in pyproject["project"]["optional-dependencies"]}


def _ci_type_check_sync() -> str:
    steps = yaml.safe_load(_read(".github/workflows/ci.yml"))["jobs"]["type-check"]["steps"]
    syncs = [line for step in steps for line in _uv_sync_lines(str(step.get("run", "")))]
    assert len(syncs) == 1, f"type-check job: expected one `uv sync`, got {syncs}"
    return syncs[0]


def _make_recipe(target: str) -> str:
    """A Makefile target's rule and recipe, up to the next blank line."""
    match = re.search(
        rf"^{re.escape(target)}:.*?(?=\n[ \t]*\n|\Z)", _read("Makefile"), re.MULTILINE | re.DOTALL
    )
    assert match, f"Makefile has no `{target}` target"
    return match.group(0)


def _make_install_sync() -> str:
    syncs = _uv_sync_lines(_make_recipe("install"))
    assert len(syncs) == 1, f"make install: expected one `uv sync`, got {syncs}"
    return syncs[0]


def test_ci_type_check_installs_every_extra() -> None:
    """The ci.yml rule "if a new extra is added to pyproject.toml, add it here too"."""
    assert _extras(_ci_type_check_sync()) == _pyproject_extras()


def test_make_install_matches_ci_type_check() -> None:
    assert _LOCAL_SKIP <= _pyproject_extras(), f"stale _LOCAL_SKIP entry: {_LOCAL_SKIP}"
    install = _make_install_sync()
    assert "--locked" in install, install
    assert _extras(install) == _extras(_ci_type_check_sync()) - _LOCAL_SKIP


def test_setup_sh_installs_the_make_install_set() -> None:
    """README's one-line setup builds the same venv as ``make install``.

    ``uv sync`` is exact: a smaller set in setup.sh would remove the provider
    packages again, and ``make type-check`` would fail on their imports.
    """
    syncs = [
        line for line in _uv_sync_lines(_read("setup.sh")) if not re.match(r"\s*(#|echo\b)", line)
    ]
    assert len(syncs) == 1, f"setup.sh: expected one `uv sync` command, got {syncs}"
    assert "--locked" in syncs[0], syncs[0]
    assert _extras(syncs[0]) == _extras(_make_install_sync())


def _pyright_line() -> str:
    """The recipe's pyright command (the ``@`` lines are its guard and echoes)."""
    lines = [
        line
        for line in _make_recipe("type-check").splitlines()
        if re.match(r"\t[^@]", line) and "pyright" in line
    ]
    assert len(lines) == 1, f"make type-check: expected one pyright command, got {lines}"
    return lines[0]


def test_make_type_check_takes_pyright_and_interpreter_from_one_venv() -> None:
    line = _pyright_line()
    match = re.search(r"(\S+)/bin/pyright\s+--pythonpath[ =](\S+)/bin/python\s", line)
    assert match, f"pyright must get the venv's interpreter via --pythonpath: {line}"
    venv = match.group(1)
    assert match.group(2) == venv, line
    variable = re.fullmatch(r"\$\((\w+)\)", venv)
    if variable:
        definition = re.search(rf"^{variable.group(1)}\s*[?:]?=\s*(.+)$", _read("Makefile"), re.M)
        assert definition, f"Makefile does not define {venv}"
        venv = definition.group(1)
    assert re.search(r"(\$\(BACKEND_DIR\)|backend)/\.venv\b", venv), f"default venv: {venv}"


@pytest.mark.skipif(shutil.which("make") is None, reason="make is not installed")
@pytest.mark.parametrize("given_as", ["argument", "environment"])
def test_make_type_check_resolves_backend_venv_from_the_repo_root(given_as: str) -> None:
    """A relative BACKEND_VENV, as a make argument or from the environment.

    The guard runs in the repository root and pyright after ``cd backend``, so
    both must see the same absolute path.
    """
    relative = "some/where/.venv"
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"BACKEND_VENV", "MAKEFLAGS", "MAKELEVEL", "MFLAGS"}
    }
    args = ["make", "--no-print-directory", "-n", "-C", str(_REPO_ROOT), "type-check"]
    if given_as == "argument":
        args.append(f"BACKEND_VENV={relative}")
    else:
        env["BACKEND_VENV"] = relative
    dry_run = subprocess.run(args, capture_output=True, text=True, env=env, check=True).stdout
    venv = _REPO_ROOT / relative
    assert f"test -x {venv}/bin/pyright " in dry_run, dry_run
    assert f"{venv}/bin/pyright --pythonpath {venv}/bin/python " in dry_run, dry_run


def _checked_dirs() -> list[Path]:
    """The directories ``make type-check`` hands pyright (it runs in backend/)."""
    dirs = re.findall(r"(?<=\s)([\w./-]+/)(?=\s|$)", _pyright_line())
    assert "src/" in dirs, dirs
    return [(_REPO_ROOT / "backend" / rel).resolve() for rel in dirs]


def _skip_only_modules() -> set[str]:
    """Top-level import names of the packages only ``_LOCAL_SKIP`` brings in.

    Distribution name with ``-`` -> ``_`` is a guess that holds for the ones
    that matter (sentence_transformers, torch, transformers); a miss only makes
    this guard weaker, never a false failure.
    """
    lock = tomllib.loads(_read("backend/uv.lock"))
    packages = {pkg["name"]: pkg for pkg in lock["package"]}
    root = next(pkg for pkg in lock["package"] if pkg.get("source") == {"editable": "."})

    def closure(names: list[str]) -> set[str]:
        seen: set[str] = set()
        stack = list(names)
        while stack:
            name = stack.pop()
            if name in seen or name not in packages:
                continue
            seen.add(name)
            stack.extend(dep["name"] for dep in packages[name].get("dependencies", []))
        return seen

    optional = root.get("optional-dependencies", {})
    kept = [dep["name"] for dep in root.get("dependencies", [])] + [
        dep["name"]
        for extra, deps in optional.items()
        if _norm(extra) not in _LOCAL_SKIP
        for dep in deps
    ]
    skipped = [
        dep["name"]
        for extra, deps in optional.items()
        if _norm(extra) in _LOCAL_SKIP
        for dep in deps
    ]
    assert skipped, f"uv.lock has no packages for {_LOCAL_SKIP}"
    return {name.replace("-", "_") for name in closure(skipped) - closure(kept)}


def _top_level_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_locally_skipped_extras_are_not_imported_by_checked_code() -> None:
    """Otherwise a local type-check fails on imports CI resolves: install them, or stop skipping."""
    modules = _skip_only_modules()
    assert "sentence_transformers" in modules, sorted(modules)
    hits = sorted(
        f"{path.relative_to(_REPO_ROOT)}: {name}"
        for root in _checked_dirs()
        for path in root.rglob("*.py")
        for name in _top_level_imports(path) & modules
    )
    assert hits == [], hits
