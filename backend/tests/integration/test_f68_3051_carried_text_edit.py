"""#3051 (f68): ``memories.carried_text_edited_at``.

1. The upgrade adds the column, a nullable TIMESTAMP WITHOUT TIME ZONE with no
   default, and takes no lock on another table: it runs while ``contexts``,
   ``workspaces``, ``users`` and ``memory_revisions`` are held in
   ``ACCESS EXCLUSIVE`` by another transaction.
2. A row that existed reads NULL (no backfill); an INSERT that does not name
   the column (the previous release's code) gets NULL.
3. A rerun after an interrupted upgrade keeps the stored values; downgrade
   drops the column and the upgrade runs again.

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

REVISION = "f68_3051_carried_text_edit"
BEFORE = "f67_3035_public_visitors"
_COLUMN = "carried_text_edited_at"

_INSERT_MEMORY = (
    "INSERT INTO memories (id, workspace_id, user_id, summary, content, type, scope, client, "
    "embedding_status, importance, confidence, long_term, access_count, source) "
    "VALUES (:id, :ws, 'f68-migration-test', 'f68 row', 'body', 'note', 'working', 'test', "
    "'success', 0.5, 1.0, false, 0, 'manual')"
)
_SELECT = "SELECT carried_text_edited_at FROM memories WHERE id = :id"


def _column(conn) -> tuple[str, str, str | None] | None:
    row = conn.execute(
        text(
            "SELECT is_nullable, data_type, column_default "
            "FROM information_schema.columns "
            "WHERE table_name = 'memories' AND column_name = :name"
        ),
        {"name": _COLUMN},
    ).one_or_none()
    return None if row is None else (row[0], row[1], row[2])


def test_f68_adds_a_nullable_timestamp_without_locking_other_tables() -> None:
    _reset_alembic_state()
    engine = _sync_engine()
    holder = _sync_engine()
    workspace_id = uuid.uuid4()
    existing, fresh = uuid.uuid4(), uuid.uuid4()
    try:
        _upgrade_to(BEFORE)
        with engine.connect() as conn:
            assert _column(conn) is None
            conn.execute(
                text(
                    "INSERT INTO workspaces (id, name, owner_user_id, plan_name) "
                    "VALUES (:id, :name, 'f68-migration-test', 'free')"
                ),
                {"id": workspace_id, "name": f"f68-{workspace_id.hex[:8]}"},
            )
            conn.execute(text(_INSERT_MEMORY), {"id": existing, "ws": workspace_id})
            conn.commit()

        with holder.connect() as lock_conn:
            lock_tx = lock_conn.begin()
            lock_conn.execute(
                text(
                    "LOCK TABLE contexts, workspaces, users, memory_revisions "
                    "IN ACCESS EXCLUSIVE MODE"
                )
            )
            try:
                _upgrade_to(REVISION)
            finally:
                lock_tx.rollback()

        with engine.connect() as conn:
            assert _column(conn) == ("YES", "timestamp without time zone", None)
            assert conn.execute(text(_SELECT), {"id": existing}).scalar_one() is None
            # The previous release's INSERT does not name the column.
            conn.execute(text(_INSERT_MEMORY), {"id": fresh, "ws": workspace_id})
            assert conn.execute(text(_SELECT), {"id": fresh}).scalar_one() is None
            conn.execute(
                text("UPDATE memories SET carried_text_edited_at = now() WHERE id = :id"),
                {"id": fresh},
            )
            conn.commit()

        # A rerun after an interrupted upgrade keeps the stored value.
        with engine.begin() as conn:
            conn.execute(text("UPDATE alembic_version SET version_num = :v"), {"v": BEFORE})
        _upgrade_to(REVISION)
        with engine.connect() as conn:
            assert conn.execute(text(_SELECT), {"id": fresh}).scalar_one() is not None

        _downgrade_to(BEFORE)
        with engine.connect() as conn:
            assert _column(conn) is None
        _upgrade_to(REVISION)
        with engine.connect() as conn:
            assert _column(conn) is not None
    finally:
        try:
            _reset_alembic_state()
            _upgrade_to("head")
        finally:
            engine.dispose()
            holder.dispose()
