"""#1885: ``REQUIRE_TEST_DATABASE=1`` turns the test-database skip into a failure.

``async_engine`` skips every ``db_session`` test when ``create_all`` fails.
That is right for the unit job and for a laptop without Postgres, but the
integration job exists to run those tests: there an unusable database must
turn the job red. No database needed.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests import conftest

_BACKEND = Path(__file__).resolve().parents[1]


def test_without_the_variable_an_unusable_database_skips(monkeypatch):
    monkeypatch.delenv("REQUIRE_TEST_DATABASE", raising=False)
    with pytest.raises(pytest.skip.Exception, match="Test database not available: boom"):
        conftest._test_database_unavailable(RuntimeError("boom"))


@pytest.mark.parametrize("value", ["", "0", "true"])
def test_only_the_value_1_requires_the_database(monkeypatch, value):
    monkeypatch.setenv("REQUIRE_TEST_DATABASE", value)
    with pytest.raises(pytest.skip.Exception):
        conftest._test_database_unavailable(RuntimeError("boom"))


def test_with_the_variable_an_unusable_database_fails(monkeypatch):
    monkeypatch.setenv("REQUIRE_TEST_DATABASE", "1")
    with pytest.raises(pytest.fail.Exception, match="REQUIRE_TEST_DATABASE=1: boom"):
        conftest._test_database_unavailable(RuntimeError("boom"))


_DB_TEST = """
async def test_needs_the_database(db_session):
    assert db_session is not None
"""


def _run_db_test_against_a_dead_database(tmp_path: Path, require: str | None) -> str:
    """Run one ``db_session`` test in a child pytest whose database refuses connections."""
    test_file = tmp_path / "test_needs_db.py"
    test_file.write_text(_DB_TEST, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "REQUIRE_TEST_DATABASE"}
    # Port 1 on loopback: nothing listens there, the connection is refused at once.
    env["TEST_DATABASE_URL"] = "postgresql+asyncpg://nobody:nobody@127.0.0.1:1/absent"
    if require is not None:
        env["REQUIRE_TEST_DATABASE"] = require
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(test_file),
            "-c",
            str(_BACKEND / "pyproject.toml"),
            "--rootdir",
            str(_BACKEND),
            # The file sits outside tests/, so load the suite's conftest by name.
            "-p",
            "tests.conftest",
            "-p",
            "no:cacheprovider",
            # No coverage in the child: it would write over the parent's data file.
            "--no-cov",
            "-q",
            "-rsE",
        ],
        cwd=_BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return result.stdout + result.stderr


def test_a_db_session_test_skips_when_the_database_is_optional(tmp_path):
    output = _run_db_test_against_a_dead_database(tmp_path, require=None)
    assert "1 skipped" in output, output
    assert "Test database not available" in output, output


def test_a_db_session_test_errors_when_the_database_is_required(tmp_path):
    output = _run_db_test_against_a_dead_database(tmp_path, require="1")
    assert "1 error" in output, output
    assert "skipped" not in output, output
    assert "REQUIRE_TEST_DATABASE=1" in output, output
