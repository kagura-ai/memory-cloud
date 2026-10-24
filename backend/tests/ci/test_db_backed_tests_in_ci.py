"""The DB-backed tests outside tests/integration/ run in CI (#2297).

``backend-unit`` has no Postgres, so every test that takes the ``db_session``
fixture skips there. ``backend-integration`` runs the backend-unit suite
again against Postgres in one step, then the integration suite in another. A
list of hand-picked files once stood in for that step and left about 1,000
tests unrun, 13 of them failing on main.
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

# Options that change how a run reports or times out, never which tests it
# collects. Anything else on the pytest command line counts as selection.
_NON_SELECTING = re.compile(r"^(-v|-q|-ra|--maxfail=\d+|--timeout=\d+)$")

# The one selection difference from backend-unit: tests/cli runs in the
# integration step, against the same database.
_EXTRA_IGNORES = {"--ignore=tests/cli"}


def _jobs() -> dict:
    return yaml.safe_load(CI_YML.read_text())["jobs"]


def _pytest_args(step: dict) -> list[str]:
    _, _, args = step.get("run", "").partition("pytest")
    return args.split()


def _selection(step: dict) -> set[str]:
    return {a for a in _pytest_args(step) if not _NON_SELECTING.match(a)}


def _pytest_steps(job: str) -> list[dict]:
    return [s for s in _jobs()[job]["steps"] if re.search(r"\bpytest\b", s.get("run", ""))]


def _db_step() -> dict:
    steps = [
        s for s in _pytest_steps("backend-integration") if "--ignore=tests/integration" in s["run"]
    ]
    assert len(steps) == 1, steps
    return steps[0]


def test_the_db_step_selects_what_backend_unit_selects() -> None:
    (unit,) = _pytest_steps("backend-unit")

    assert _selection(_db_step()) == _selection(unit) | _EXTRA_IGNORES


def test_the_db_step_runs_against_postgres() -> None:
    env = _db_step().get("env", {})

    assert env.get("REQUIRE_TEST_DATABASE") == "1"
    assert env.get("TEST_DATABASE_URL", "").startswith("postgresql")
    # The app's own engine reads DATABASE_URL; it must be the same database.
    assert env.get("DATABASE_URL") == env["TEST_DATABASE_URL"]


def test_what_the_db_step_leaves_out_runs_in_the_integration_step() -> None:
    steps = _pytest_steps("backend-integration")
    db_step = _db_step()
    ignored = {
        a.removeprefix("--ignore=").rstrip("/")
        for a in _pytest_args(db_step)
        if a.startswith("--ignore=")
    }
    later = steps[steps.index(db_step) + 1 :]
    covered = {a.rstrip("/") for s in later for a in _pytest_args(s) if a.startswith("tests/")}

    # tests/e2e needs a browser and a running stack; it has its own lanes.
    assert ignored - {"tests/e2e"} <= covered, ignored - covered
