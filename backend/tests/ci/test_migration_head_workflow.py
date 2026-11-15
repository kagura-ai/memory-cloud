"""Guard tests for #2825: main itself is checked for a single alembic head.

A pull request's CI runs on the tree the branch had at its last push. Two pull
requests that each add a revision are both green and still leave ``main`` with
two heads once both are merged; ``alembic upgrade head`` then stops. The
workflow ``.github/workflows/migration-head.yml`` runs the existing head test
on ``main`` after every push that touches the revisions, so a second head is a
failed run on the merge commit. These tests pin the parts of that file the
check depends on.
"""

from pathlib import Path

import pytest
import yaml

# backend/tests/ci/test_migration_head_workflow.py -> ci -> tests -> backend -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "migration-head.yml"

# As in test_uv_version_lockstep.py: the workflows are not copied into the
# image, so the guard runs from a repository checkout.
pytestmark = pytest.mark.skipif(
    not (_REPO_ROOT / ".github" / "workflows" / "ci.yml").is_file(),
    reason="not a repository checkout (e.g. tests mounted into the api container)",
)

_HEAD_TEST = "tests/test_migration_head.py"


def _workflow() -> dict:
    return yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))


def _triggers() -> dict:
    # PyYAML reads the bare key ``on`` as the boolean True.
    workflow = _workflow()
    return workflow.get("on") or workflow[True]


def _steps() -> list[dict]:
    jobs = _workflow()["jobs"]
    assert list(jobs) == ["single-head"]
    return jobs["single-head"]["steps"]


def test_runs_on_pushes_to_main_that_touch_the_revisions() -> None:
    push = _triggers()["push"]
    assert push["branches"] == ["main"]
    assert "backend/alembic/**" in push["paths"]
    # A change to the check itself is tried at once.
    assert ".github/workflows/migration-head.yml" in push["paths"]


def test_can_be_started_by_hand() -> None:
    assert "workflow_dispatch" in _triggers()


def test_is_not_a_pull_request_check() -> None:
    """A pull request already runs the head test in CI, on its own tree."""
    assert "pull_request" not in _triggers()
    assert "pull_request_target" not in _triggers()


def test_reads_the_repository_only() -> None:
    assert _workflow()["permissions"] == {"contents": "read"}


def test_runs_the_existing_head_test() -> None:
    assert (_REPO_ROOT / "backend" / _HEAD_TEST).is_file()
    commands = [str(step.get("run", "")) for step in _steps()]
    assert any("pytest" in run and _HEAD_TEST in run for run in commands), commands


def test_the_job_is_bounded() -> None:
    assert _workflow()["jobs"]["single-head"]["timeout-minutes"] <= 15


def test_actions_are_pinned_like_ci() -> None:
    """Same actions at the same commits as ci.yml, so one bump covers both."""
    ci = yaml.safe_load((_REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    pinned = {
        str(step["uses"])
        for job in ci["jobs"].values()
        for step in job.get("steps") or []
        if "uses" in step
    }
    used = [str(step["uses"]) for step in _steps() if "uses" in step]
    assert used, "the job uses no action"
    assert [u for u in used if u not in pinned] == []
