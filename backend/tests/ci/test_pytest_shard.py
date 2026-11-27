"""The integration suite's shards cover every test file exactly once (#3027).

``backend-integration`` runs the integration selection as parallel jobs, each
given its files by ``.github/scripts/pytest_shard.py``. A file that fell in no
shard would silently stop running, so the split is checked on the real tree
and the workflow's matrix is checked against the shard count it passes.
"""

from __future__ import annotations

import importlib.util
import os
import re
from pathlib import Path

import pytest
import yaml

from tests.ci.test_pytest_timeouts import CI_YML

pytestmark = pytest.mark.skipif(
    not CI_YML.is_file(),
    reason="not a repository checkout (e.g. tests mounted into the api container)",
)

_REPO_ROOT = CI_YML.parents[2]
_SCRIPT = _REPO_ROOT / ".github" / "scripts" / "pytest_shard.py"
_BACKEND = _REPO_ROOT / "backend"


def _shard_module():
    spec = importlib.util.spec_from_file_location("pytest_shard", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _job() -> dict:
    return yaml.safe_load(CI_YML.read_text())["jobs"]["backend-integration"]


def _integration_step() -> dict:
    (step,) = [s for s in _job()["steps"] if "pytest_shard.py" in s.get("run", "")]
    return step


def _roots() -> list[str]:
    run = _integration_step()["run"]
    return re.findall(r"(?<!\S)tests/\S+", run.partition("pytest_shard.py")[2].partition(">")[0])


@pytest.fixture
def in_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(_BACKEND)


def test_the_workflow_shards_the_whole_integration_selection() -> None:
    assert _roots() == ["tests/integration/", "tests/cli/", "tests/smoke/test_all_routes.py"]


def test_every_test_file_is_in_exactly_one_shard(in_backend: None) -> None:
    shard = _shard_module()
    total = int(_integration_step()["env"]["INTEGRATION_SHARDS"])
    files = shard.collect(_roots())
    shards = shard.assign(files, total)

    assert len(shards) == total
    assert all(shards), "an empty shard"
    flat = [path for one in shards for path in one]
    assert sorted(flat) == files
    assert len(flat) == len(set(flat))
    # Every test file on disk under the roots is one the script found.
    on_disk = {
        Path(root, name)
        for base in ("tests/integration", "tests/cli")
        for root, _, names in os.walk(base)
        for name in names
        if name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))
    }
    assert on_disk <= set(files)


def test_the_matrix_is_the_db_job_plus_one_job_per_shard() -> None:
    total = int(_integration_step()["env"]["INTEGRATION_SHARDS"])
    matrix = _job()["strategy"]["matrix"]["shard"]

    assert matrix == ["db", *[str(n) for n in range(1, total + 1)]]
    assert _job()["strategy"]["fail-fast"] is False
    assert _integration_step()["if"] == "${{ matrix.shard != 'db' }}"


def test_integration_starts_with_the_unit_job() -> None:
    assert _job()["needs"] == ["detect-changes"]


def test_a_tag_push_starts_no_ci_run() -> None:
    # PyYAML reads the bare key ``on`` as the boolean True.
    document = yaml.safe_load(CI_YML.read_text())
    triggers = document.get("on", document.get(True))

    assert set(triggers) == {"pull_request", "workflow_dispatch"}
    assert "github.event_name == 'push'" not in CI_YML.read_text()


def test_the_script_refuses_an_empty_selection(tmp_path: Path) -> None:
    shard = _shard_module()
    (tmp_path / "empty").mkdir()
    with pytest.raises(SystemExit):
        shard.collect([str(tmp_path / "empty")])
    one = tmp_path / "test_one.py"
    one.write_text("def test_x():\n    pass\n")
    with pytest.raises(SystemExit):
        shard.main(["--shard", "2", "--total", "2", str(one)])
