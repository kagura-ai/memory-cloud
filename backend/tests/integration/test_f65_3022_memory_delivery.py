"""#3022 (f65): ``memories.first_delivered_at`` / ``last_delivered_at``.

1. The upgrade adds both columns, nullable TIMESTAMP WITHOUT TIME ZONE with no
   default, and takes no lock on another table: it runs while ``contexts``,
   ``workspaces`` and ``users`` are held in ``ACCESS EXCLUSIVE`` by another
   transaction.
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

REVISION = "f65_3022_memory_delivery"
BEFORE = "f64_3021_judge_switch"
_COLUMNS = ("first_delivered_at", "last_delivered_at")

_INSERT_MEMORY = (
    "INSERT INTO memories (id, workspace_id, user_id, summary, content, type, scope, client, "
    "embedding_status, importance, confidence, long_term, access_count, source) "
    "VALUES (:id, :ws, 'f65-migration-test', 'f65 row', 'body', 'note', 'working', 'test', "
    "'success', 0.5, 1.0, false, 0, 'manual')"
)
_SELECT = "SELECT first_delivered_at, last_delivered_at FROM memories WHERE id = :id"


def _columns(conn) -> dict[str, tuple[str, str, str | None]]:
    rows = conn.execute(
        text(
            "SELECT column_name, is_nullable, data_type, column_default "
            "FROM information_schema.columns "
            "WHERE table_name = 'memories' AND column_name = ANY(:names)"
        ),
        {"names": list(_COLUMNS)},
    ).all()
    return {row[0]: (row[1], row[2], row[3]) for row in rows}


def test_f65_adds_two_nullable_timestamps_without_locking_other_tables() -> None:
    _reset_alembic_state()
    engine = _sync_engine()
    holder = _sync_engine()
    workspace_id = uuid.uuid4()
    existing, fresh = uuid.uuid4(), uuid.uuid4()
    try:
        _upgrade_to(BEFORE)
        with engine.connect() as conn:
            assert _columns(conn) == {}
            conn.execute(
                text(
                    "INSERT INTO workspaces (id, name, owner_user_id, plan_name) "
                    "VALUES (:id, :name, 'f65-migration-test', 'free')"
                ),
                {"id": workspace_id, "name": f"f65-{workspace_id.hex[:8]}"},
            )
            conn.execute(text(_INSERT_MEMORY), {"id": existing, "ws": workspace_id})
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
            assert _columns(conn) == dict.fromkeys(
                _COLUMNS, ("YES", "timestamp without time zone", None)
            )
            assert tuple(conn.execute(text(_SELECT), {"id": existing}).one()) == (None, None)
            # The previous release's INSERT does not name the columns.
            conn.execute(text(_INSERT_MEMORY), {"id": fresh, "ws": workspace_id})
            assert tuple(conn.execute(text(_SELECT), {"id": fresh}).one()) == (None, None)
            conn.execute(
                text(
                    "UPDATE memories SET first_delivered_at = now(), "
                    "last_delivered_at = now() WHERE id = :id"
                ),
                {"id": fresh},
            )
            conn.commit()

        # A rerun after an interrupted upgrade keeps the stored values.
        with engine.begin() as conn:
            conn.execute(text("UPDATE alembic_version SET version_num = :v"), {"v": BEFORE})
        _upgrade_to(REVISION)
        with engine.connect() as conn:
            assert None not in tuple(conn.execute(text(_SELECT), {"id": fresh}).one())

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
