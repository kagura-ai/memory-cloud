"""``beta_invites`` must not make ``Base.metadata`` unresolvable (Issue #1581).

``models/__init__`` registers ``models.beta_invite`` in every process, and that
table has an FK to ``signup_allowlist``. If the target is not registered along
with it, ``Base.metadata`` raises ``NoReferencedTableError`` in any process whose
import graph does not happen to reach ``models.signup_gate``:

* ``tests/conftest.py::async_engine`` wraps ``create_all`` in ``try/except`` and
  turns the error into a **skip** — so a per-file run silently skips every
  DB-backed test while a whole-directory run stays green;
* ``alembic/env.py`` imports a fixed model set, so ``--autogenerate`` / ``check``
  break.

Each case runs in a fresh interpreter: inside the pytest process some other test
module has usually imported the signup gate already, which is exactly the
import-order luck that hid the bug.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
SRC_DIR = BACKEND_DIR / "src"
ALEMBIC_ENV = BACKEND_DIR / "alembic" / "env.py"

_RESOLVE = (
    "from db.base import Base\n"
    "names = {t.name for t in Base.metadata.sorted_tables}\n"
    "missing = {'beta_invites', 'signup_allowlist'} - names\n"
    "assert not missing, missing\n"
)


def _run(code: str) -> subprocess.CompletedProcess[str]:
    """Run ``code`` in a fresh interpreter with ``src`` importable."""
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
        cwd=SRC_DIR,
        env={**os.environ, "PYTHONPATH": str(SRC_DIR)},
        timeout=120,
    )


def _alembic_env_model_imports() -> str:
    """The ``import models.*`` lines of ``alembic/env.py``, verbatim.

    Read from the file rather than copied so this test keeps describing the
    import graph alembic really has.
    """
    lines = re.findall(r"^import models\.\w+", ALEMBIC_ENV.read_text(encoding="utf-8"), re.M)
    assert lines, "alembic/env.py no longer imports models.* — update this test"
    return "\n".join(lines) + "\n"


def test_beta_invite_brings_its_signup_gate_fk_target_along() -> None:
    """Importing the model alone registers ``signup_allowlist``.

    Only that FK is asserted: ``users`` comes from ``models.auth``, which every
    real process imports first, and no model module in this package imports it
    for itself.
    """
    result = _run(
        "import models.beta_invite\n"
        "from db.base import Base\n"
        "table = Base.metadata.tables['beta_invites']\n"
        "fk = next(f for f in table.foreign_keys if f.parent.name == 'redeemed_allowlist_entry_id')\n"
        "assert fk.column.table.name == 'signup_allowlist'\n"
    )
    assert result.returncode == 0, result.stderr[-2000:]


@pytest.mark.parametrize(
    "imports",
    [
        pytest.param(None, id="alembic-env"),
        # What ``tests/conftest.py`` imports before ``create_all``.
        pytest.param("import models.auth\nimport models.memory\n", id="tests-conftest"),
    ],
)
def test_metadata_resolves_without_an_explicit_signup_gate_import(imports: str | None) -> None:
    """``sorted_tables`` resolves in processes that never import the signup gate."""
    code = imports if imports is not None else _alembic_env_model_imports()
    assert "signup_gate" not in code

    result = _run(code + _RESOLVE)

    assert result.returncode == 0, result.stderr[-2000:]
