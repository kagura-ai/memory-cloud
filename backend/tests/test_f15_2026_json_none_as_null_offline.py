"""f15_2026_json_none_as_null offline (``alembic upgrade --sql``) form (#2026).

Offline there are no results to drive the batched walk, so the migration emits
one whole-table UPDATE per walked table — the same statements
docs/deployment.md gives for the rerun. No database is used.
"""

from __future__ import annotations

import importlib
import io
import re
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


def test_offline_upgrade_emits_one_update_per_table() -> None:
    expected = [
        f"{f15.rewrite_all_sql(table, columns)};" for table, _pk_type, columns in f15.TABLES
    ]
    assert _offline("upgrade") == expected
    assert len(expected) == 7
    assert expected[1] == (
        "UPDATE sleep_actions SET details = CASE WHEN "
        "(pg_column_size(details) < 16 AND details::text = 'null') THEN NULL ELSE details END "
        "WHERE ((pg_column_size(details) < 16 AND details::text = 'null'));"
    )


def test_offline_downgrade_emits_nothing() -> None:
    assert _offline("downgrade") == []


def test_only_the_small_tables_are_walked() -> None:
    """Neither memories (its rewrite is non-HOT into every index) nor the large
    or append-only logs are walked (#2026 D3, D4)."""
    walked = {table for table, _pk_type, _columns in f15.TABLES}
    assert walked == {
        "sleep_reports",
        "sleep_actions",
        "measurements",
        "memory_analyses",
        "bm25_idf_drift_log",
        "erasure_requests",
        "workspace_connectors",
    }


def test_docs_rerun_statements_are_the_offline_ones() -> None:
    """docs/deployment.md lays the per-table statements out for reading; up to
    whitespace and parentheses they are exactly what the migration emits."""
    docs = (Path(__file__).resolve().parents[2] / "docs" / "deployment.md").read_text()
    section = docs[docs.index("### Migration `f15_2026_json_none_as_null`") :]
    block = section[section.index("```sql\nSET lock_timeout = '5s';\n\nUPDATE") :]
    block = block[len("```sql\n") : block.index("\n```")]

    def bare(sql: str) -> str:
        return re.sub(r"[\s()]", "", sql)

    expected = ["SET lock_timeout = '5s';"] + [
        f"{f15.rewrite_all_sql(table, columns)};" for table, _pk_type, columns in f15.TABLES
    ]
    assert [bare(s) for s in block.split(";") if s.strip()] == [
        bare(s).rstrip(";") for s in expected
    ]
