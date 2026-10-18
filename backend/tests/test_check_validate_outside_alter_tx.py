"""Pin VALIDATE CONSTRAINT outside the ALTER's transaction (#2073).

The CHECK-widening migrations swap a constraint with ``DROP ..., ADD ... NOT
VALID`` (ACCESS EXCLUSIVE) and then ``VALIDATE CONSTRAINT``. ``alembic/env.py``
runs a whole upgrade in one transaction, so a VALIDATE in that transaction
scans the table while the ALTER's ACCESS EXCLUSIVE lock is still held, blocking
reads and writes. Each VALIDATE must run in ``autocommit_block()``: the NOT
VALID add commits first, and VALIDATE then takes only SHARE UPDATE EXCLUSIVE.

Each migration runs under a real offline (``--sql``) PostgreSQL context, so the
captured script carries the ``COMMIT;`` / ``BEGIN;`` alembic emits around an
autocommit block. The offline context's connection is a recorder, so the reads
some of these migrations make (the INVALID-index probe, the downgrade leftover
check) land in the script in order and find no rows. No database is used.
"""

from __future__ import annotations

import importlib
import io
import sys
from pathlib import Path
from types import ModuleType

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations

_ALEMBIC_VERSIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"
if str(_ALEMBIC_VERSIONS) not in sys.path:
    sys.path.insert(0, str(_ALEMBIC_VERSIONS))

_CASES = [
    ("e74_1401_mae_explore", "upgrade"),
    ("e74_1401_mae_explore", "downgrade"),
    ("e84_1619_tool_guardrails", "upgrade"),
    ("e84_1619_tool_guardrails", "downgrade"),
    ("e98_2007_mae_list_changes_since", "upgrade"),
    ("e98_2007_mae_list_changes_since", "downgrade"),
    ("f00_2019_llm_log_supersede", "upgrade"),
    ("f00_2019_llm_log_supersede", "downgrade"),
]

# Revisions that still VALIDATE inside the upgrade transaction. Applied on every
# instance past them; not rewritten here. Never add to this set.
_LEGACY_IN_TRANSACTION = {
    "b03_396_neural_edges_ws_ctx_not_null.py",
    "d05_523_costgrade_source_paid_by.py",
    "d07_495_cluster_label_phase.py",
    "e01_546_cache_write_and_pricing_model.py",
}


class _NoRows:
    def first(self) -> None:
        return None


class _RecordingBind:
    """Writes each read into the offline script, in order, and returns no rows."""

    def __init__(self, buf: io.StringIO) -> None:
        self._buf = buf

    def execute(self, statement, *_args, **_kwargs) -> _NoRows:
        self._buf.write(f"{statement};\n")
        return _NoRows()


def _offline_script(module: ModuleType, direction: str) -> list[str]:
    buf = io.StringIO()
    ctx = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": buf, "transactional_ddl": True},
    )
    # ``op.get_bind()`` returns ``impl.bind`` (the connection, None offline).
    # Offline DDL goes to the buffer, never through it, so only reads see this.
    ctx.impl.connection = _RecordingBind(buf)  # type: ignore[assignment]
    with Operations.context(ctx):
        getattr(module, direction)()
    return [line.strip() for line in buf.getvalue().splitlines() if line.strip()]


@pytest.mark.parametrize(("revision", "direction"), _CASES)
def test_validate_runs_after_the_not_valid_add_commits(revision, direction):
    module = importlib.import_module(revision)
    lines = _offline_script(module, direction)

    add = next(i for i, s in enumerate(lines) if s.endswith("NOT VALID;"))
    validate = next(i for i, s in enumerate(lines) if "VALIDATE CONSTRAINT" in s)
    commits = [i for i, s in enumerate(lines) if s == "COMMIT;"]
    begins = [i for i, s in enumerate(lines) if s == "BEGIN;"]

    # A COMMIT separates the ACCESS EXCLUSIVE add from the scan ...
    assert any(add < c < validate for c in commits), lines
    # ... and VALIDATE sits inside the autocommit block (before its BEGIN).
    opened = max(c for c in commits if c < validate)
    assert any(b > validate for b in begins), lines
    assert not any(opened < b < validate for b in begins), lines


def test_every_new_validate_uses_an_autocommit_block():
    """Tripwire for the next migration: a VALIDATE needs ``autocommit_block()``.

    Source-level and coarse (it cannot see which statement sits in the block);
    the offline-script test above pins the order for the revisions it lists.
    """
    offenders = {
        path.name
        for path in _ALEMBIC_VERSIONS.glob("*.py")
        if "VALIDATE CONSTRAINT" in (text := path.read_text()) and "autocommit_block()" not in text
    }
    assert offenders == _LEGACY_IN_TRANSACTION
