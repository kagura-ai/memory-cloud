"""Execute the documented repair entrypoint outside pytest's import graph."""

import os
import subprocess
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[2]


def _env():
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["ENV_FILE"] = str(_BACKEND / "nonexistent.env")
    env["ENVIRONMENT"] = "development"
    return env


def test_documented_module_entrypoint_works_without_pythonpath():
    result = subprocess.run(
        [sys.executable, "-m", "src.cli.cleanup_erased_vectors", "--help"],
        cwd=_BACKEND,
        env=_env(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--apply" in result.stdout and "--yes" in result.stdout
    assert "Traceback" not in result.stderr


def test_registered_vector_jobs_resolve_request_fk_in_a_fresh_process():
    env = _env()
    env["PYTHONPATH"] = str(_BACKEND / "src")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import models; from db.base import Base; "
            "from sqlalchemy.schema import CreateTable; "
            "from sqlalchemy.dialects.postgresql import dialect; "
            "str(CreateTable(Base.metadata.tables['vector_erasure_jobs']).compile(dialect=dialect())); "
            "print('resolved')",
        ],
        cwd=_BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "resolved" in result.stdout
