"""#3019 (f66): ``sleep_reports.coverage_state`` and ``memories.importance_reevaluated_at``.

1. The upgrade adds both columns, nullable with no default (JSON on
   ``sleep_reports``, TIMESTAMP WITHOUT TIME ZONE on ``memories``), and takes
   no lock on another table: it runs while ``contexts``, ``workspaces`` and
   ``users`` are held in ``ACCESS EXCLUSIVE`` by another transaction.
2. A row that existed reads NULL (no backfill); an INSERT that does not name
   the columns (the previous release's code) gets NULL.
3. A rerun after an interrupted upgrade keeps the stored values; downgrade
   drops the columns and the upgrade runs again.

Not executed in the local unit run — needs the DB container.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text

from tests.integration.test_alembic_migrations import (
    _downgrade_to,
    _reset_alembic_state,
    _sync_engine,
    _upgrade_to,
)

REVISION = "f66_3019_sleep_coverage"
BEFORE = "f65_3022_memory_delivery"
_COLUMNS = {
    ("sleep_reports", "coverage_state"): ("YES", "json", None),
    ("memories", "importance_reevaluated_at"): ("YES", "timestamp without time zone", None),
}

_INSERT_MEMORY = (
    "INSERT INTO memories (id, workspace_id, user_id, summary, content, type, scope, client, "
    "embedding_status, importance, confidence, long_term, access_count, source) "
    "VALUES (:id, :ws, 'f66-migration-test', 'f66 row', 'body', 'note', 'working', 'test', "
    "'success', 0.5, 1.0, false, 0, 'manual')"
)
_INSERT_REPORT = "INSERT INTO sleep_reports (id, user_id) VALUES (:id, 'f66-migration-test')"
_SELECT_MEMORY = "SELECT importance_reevaluated_at FROM memories WHERE id = :id"
_SELECT_REPORT = "SELECT coverage_state FROM sleep_reports WHERE id = :id"


def _columns(conn) -> dict[tuple[str, str], tuple[str, str, str | None]]:
    rows = conn.execute(
        text(
            "SELECT table_name, column_name, is_nullable, data_type, column_default "
            "FROM information_schema.columns "
            "WHERE (table_name, column_name) IN "
            "(('sleep_reports', 'coverage_state'), ('memories', 'importance_reevaluated_at'))"
        )
    ).all()
    return {(row[0], row[1]): (row[2], row[3], row[4]) for row in rows}


def test_f66_adds_two_nullable_columns_without_locking_other_tables() -> None:
    _reset_alembic_state()
    engine = _sync_engine()
    holder = _sync_engine()
    workspace_id = uuid.uuid4()
    memory, fresh_memory = uuid.uuid4(), uuid.uuid4()
    report, fresh_report = uuid.uuid4(), uuid.uuid4()
    try:
        _upgrade_to(BEFORE)
        with engine.connect() as conn:
            assert _columns(conn) == {}
            conn.execute(
                text(
                    "INSERT INTO workspaces (id, name, owner_user_id, plan_name) "
                    "VALUES (:id, :name, 'f66-migration-test', 'free')"
                ),
                {"id": workspace_id, "name": f"f66-{workspace_id.hex[:8]}"},
            )
            conn.execute(text(_INSERT_MEMORY), {"id": memory, "ws": workspace_id})
            conn.execute(text(_INSERT_REPORT), {"id": report})
            conn.commit()

        with holder.connect() as lock_conn:
            lock_tx = lock_conn.begin()
            lock_conn.execute(
                text("LOCK TABLE contexts, workspaces, users IN ACCESS EXCLUSIVE MODE")
            )
            try:
                _upgrade_to(REVISION)
            finally:
                lock_tx.rollback()

        with engine.connect() as conn:
            assert _columns(conn) == _COLUMNS
            assert conn.execute(text(_SELECT_MEMORY), {"id": memory}).scalar_one() is None
            assert conn.execute(text(_SELECT_REPORT), {"id": report}).scalar_one() is None
            # The previous release's INSERTs do not name the columns.
            conn.execute(text(_INSERT_MEMORY), {"id": fresh_memory, "ws": workspace_id})
            conn.execute(text(_INSERT_REPORT), {"id": fresh_report})
            assert conn.execute(text(_SELECT_MEMORY), {"id": fresh_memory}).scalar_one() is None
            assert conn.execute(text(_SELECT_REPORT), {"id": fresh_report}).scalar_one() is None
            conn.execute(
                text("UPDATE memories SET importance_reevaluated_at = now() WHERE id = :id"),
                {"id": fresh_memory},
            )
            conn.execute(
                text("UPDATE sleep_reports SET coverage_state = '{\"v\": 1}' WHERE id = :id"),
                {"id": fresh_report},
            )
            conn.commit()

        # A rerun after an interrupted upgrade keeps the stored values.
        with engine.begin() as conn:
            conn.execute(text("UPDATE alembic_version SET version_num = :v"), {"v": BEFORE})
        _upgrade_to(REVISION)
        with engine.connect() as conn:
            assert conn.execute(text(_SELECT_MEMORY), {"id": fresh_memory}).scalar_one() is not None
            assert conn.execute(text(_SELECT_REPORT), {"id": fresh_report}).scalar_one() == {"v": 1}

        _downgrade_to(BEFORE)
        with engine.connect() as conn:
            assert _columns(conn) == {}
        _upgrade_to(REVISION)
        with engine.connect() as conn:
            assert set(_columns(conn)) == set(_COLUMNS)
    finally:
        try:
            _reset_alembic_state()
            _upgrade_to("head")
        finally:
            engine.dispose()
            holder.dispose()
