"""f15_2026_json_none_as_null offline (``alembic upgrade --sql``) form (#2026).

Offline there are no results to drive the batched walk, so the migration emits
one whole-column UPDATE per walked column — the same statements
docs/deployment.md gives for the rerun. No database is used.
"""

from __future__ import annotations

import importlib
import io
import sys
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations

_VERSIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"
if str(_VERSIONS) not in sys.path:
    sys.path.insert(0, str(_VERSIONS))
f15 = importlib.import_module("f15_2026_json_none_as_null")


def _offline(direction: str) -> list[str]:
    buf = io.StringIO()
    ctx = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": buf, "transactional_ddl": True},
    )
    with Operations.context(ctx):
        getattr(f15, direction)()
    return [line.strip() for line in buf.getvalue().splitlines() if line.strip()]


def test_offline_upgrade_emits_one_update_per_column() -> None:
    expected = [
        f"{f15.rewrite_all_sql(table, column)};"
        for table, _pk_type, columns in f15.TABLES
        for column in columns
    ]
    assert _offline("upgrade") == expected
    assert len(expected) == 17
    assert expected[0] == (
        "UPDATE memories SET details = NULL "
        "WHERE pg_column_size(details) < 16 AND details::text = 'null';"
    )


def test_offline_downgrade_emits_nothing() -> None:
    assert _offline("downgrade") == []


def test_walked_tables_are_the_small_and_memories_ones() -> None:
    """The large or append-only logs are not walked (#2026 D4)."""
    walked = {table for table, _pk_type, _columns in f15.TABLES}
    assert walked == {
        "memories",
        "sleep_reports",
        "sleep_actions",
        "measurements",
        "memory_analyses",
        "bm25_idf_drift_log",
        "erasure_requests",
        "workspace_connectors",
    }
