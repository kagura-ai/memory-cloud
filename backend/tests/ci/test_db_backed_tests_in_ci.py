"""The DB-backed tests outside tests/integration/ run in CI (#2297).

``backend-unit`` has no Postgres, so every test that takes the ``db_session``
fixture skips there. ``backend-integration`` runs the whole backend suite
against Postgres in one step, then the integration suite in another. A list
of hand-picked files once stood in for that step and left about 1,000 tests
unrun, three of them failing on main.
"""

from __future__ import annotations

import re

import pytest
import yaml

from tests.ci.test_pytest_timeouts import CI_YML

pytestmark = pytest.mark.skipif(
    not CI_YML.is_file(),
    reason="not a repository checkout (e.g. tests mounted into the api container)",
)


def _integration_steps() -> list[dict]:
    workflow = yaml.safe_load(CI_YML.read_text())
    return workflow["jobs"]["backend-integration"]["steps"]


def _pytest_args(step: dict) -> list[str]:
    run = step.get("run", "")
    _, _, args = run.partition("pytest")
    return args.split()


def test_one_integration_step_runs_the_suite_outside_tests_integration() -> None:
    steps = [
        s
        for s in _integration_steps()
        if "pytest" in s.get("run", "") and "--ignore=tests/integration" in s.get("run", "")
    ]
    assert len(steps) == 1, steps
    step = steps[0]

    env = step.get("env", {})
    assert env.get("REQUIRE_TEST_DATABASE") == "1"
    assert env.get("TEST_DATABASE_URL", "").startswith("postgresql")

    args = _pytest_args(step)
    # The whole suite, not a list of files: no positional test paths.
    assert [a for a in args if a.startswith("tests/")] == [], args


def test_what_that_step_leaves_out_runs_in_the_next_one() -> None:
    steps = [s for s in _integration_steps() if "pytest" in s.get("run", "")]
    db_step = next(s for s in steps if "--ignore=tests/integration" in s["run"])
    ignored = {
        m.group(1).rstrip("/")
        for m in re.finditer(r"--ignore=(\S+)", " ".join(_pytest_args(db_step)))
    }
    later = steps[steps.index(db_step) + 1 :]
    covered = {a.rstrip("/") for s in later for a in _pytest_args(s) if a.startswith("tests/")}
    # tests/e2e needs a browser and a running stack; it has its own lanes.
    assert ignored - {"tests/e2e"} <= covered, ignored - covered
