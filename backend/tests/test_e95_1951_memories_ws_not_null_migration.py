"""Pin the shape of e95_1951 (``memories.workspace_id`` NOT NULL, #1951).

The DB-level behaviour (backfill, refusal on unrecoverable rows, untouched
mismatches, downgrade) is exercised against PostgreSQL in
``tests/integration/test_alembic_migrations.py``. These string-level pins are
the cheap guard against the realistic regression: someone editing the SQL and
losing the ``IS NULL`` guard (silent overwrite of existing tenancy) or the
orphan refusal (``SET NOT NULL`` then fails half-way through a deploy).
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

_ALEMBIC_VERSIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"
if str(_ALEMBIC_VERSIONS) not in sys.path:
    sys.path.insert(0, str(_ALEMBIC_VERSIONS))

e95 = importlib.import_module("e95_1951_memories_ws_not_null")


def test_chain_and_id_length() -> None:
    assert e95.revision == "e95_1951_memories_ws_not_null"
    assert len(e95.revision) <= 32
    assert e95.down_revision == "e94_1869_summary_emb_idx"


def test_backfill_writes_only_null_rows_from_the_memory_context() -> None:
    sql = " ".join(e95.BACKFILL_SQL.split())
    assert "SET workspace_id = c.workspace_id" in sql
    assert "m.context_id = c.id" in sql
    assert "m.workspace_id IS NULL" in sql
    assert "COALESCE" not in sql.upper()


def test_orphan_guard_targets_live_rows_nothing_can_fill() -> None:
    for sql in (e95.ORPHAN_COUNT_SQL, e95.ORPHAN_SAMPLE_SQL):
        assert "workspace_id IS NULL AND context_id IS NULL" in sql
        assert "deleted_at IS NULL" in sql


def test_tombstoned_orphans_are_purged_not_refused() -> None:
    sql = e95.TOMBSTONED_ORPHAN_PURGE_SQL
    assert sql.lstrip().upper().startswith("DELETE")
    assert "workspace_id IS NULL AND context_id IS NULL" in sql
    assert "deleted_at IS NOT NULL" in sql


def test_no_audit_scan_runs_inside_the_migration() -> None:
    assert not hasattr(e95, "MISMATCH_COUNT_SQL")
