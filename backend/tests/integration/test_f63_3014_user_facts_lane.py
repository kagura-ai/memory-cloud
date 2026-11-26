"""#3014 (f63): ``context_search_configs.user_facts_lane``, a boolean with a constant default.

1. The upgrade adds ``user_facts_lane`` BOOLEAN NOT NULL DEFAULT false and takes
   no lock on another table: it runs while ``contexts``, ``workspaces`` and
   ``users`` are held in ``ACCESS EXCLUSIVE`` by another transaction.
2. A row that existed reads false; an INSERT that does not name the column (the
   previous release's code) gets false.
3. A rerun after an interrupted upgrade is a no-op; downgrade drops the column
   and the upgrade runs again.

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

REVISION = "f63_3014_user_facts_lane"
BEFORE = "f62_2996_resource_rows_persist"

_INSERT_CONFIG = (
    "INSERT INTO context_search_configs "
    "(context_id, semantic_weight, bm25_weight, fetch_factor, use_rerank, "
    "embedding_model, embedding_dimensions) "
    "VALUES (:id, 0.6, 0.4, 3, false, 'text-embedding-3-small', 512)"
)
_SELECT = "SELECT user_facts_lane FROM context_search_configs WHERE context_id = :id"


def _column(conn) -> tuple[str, str, str | None] | None:
    row = conn.execute(
        text(
            "SELECT is_nullable, data_type, column_default "
            "FROM information_schema.columns "
            "WHERE table_name = 'context_search_configs' AND column_name = 'user_facts_lane'"
        )
    ).first()
    return tuple(row) if row is not None else None


def _context(conn, workspace_id: uuid.UUID) -> uuid.UUID:
    ctx_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO contexts (id, workspace_id, name, created_by) "
            "VALUES (:id, :ws, :name, 'f63-migration-test')"
        ),
        {"id": ctx_id, "ws": workspace_id, "name": f"f63-{ctx_id.hex[:8]}"},
    )
    return ctx_id


def test_f63_round_trip_defaults_false_without_locking_other_tables() -> None:
    _reset_alembic_state()
    engine = _sync_engine()
    holder = _sync_engine()
    workspace_id = uuid.uuid4()
    try:
        _upgrade_to(BEFORE)
        with engine.connect() as conn:
            assert _column(conn) is None
            conn.execute(
                text(
                    "INSERT INTO workspaces (id, name, owner_user_id, plan_name) "
                    "VALUES (:id, :name, 'f63-migration-test', 'free')"
                ),
                {"id": workspace_id, "name": f"f63-{workspace_id.hex[:8]}"},
            )
            existing = _context(conn, workspace_id)
            conn.execute(text(_INSERT_CONFIG), {"id": existing})
            fresh = _context(conn, workspace_id)
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
            nullable, data_type, default = _column(conn)
            assert (nullable, data_type) == ("NO", "boolean")
            assert default == "false"
            assert conn.scalar(text(_SELECT), {"id": existing}) is False, "no backfill"
            # The previous release's INSERT does not name the column.
            conn.execute(text(_INSERT_CONFIG), {"id": fresh})
            assert conn.scalar(text(_SELECT), {"id": fresh}) is False
            conn.execute(
                text(
                    "UPDATE context_search_configs SET user_facts_lane = true "
                    "WHERE context_id = :id"
                ),
                {"id": fresh},
            )
            conn.commit()

        # A rerun after an interrupted upgrade keeps the stored values.
        with engine.begin() as conn:
            conn.execute(text("UPDATE alembic_version SET version_num = :v"), {"v": BEFORE})
        _upgrade_to(REVISION)
        with engine.connect() as conn:
            assert conn.scalar(text(_SELECT), {"id": fresh}) is True

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
            with engine.connect() as conn:
                conn.execute(
                    text(
                        "DELETE FROM context_search_configs WHERE context_id IN "
                        "(SELECT id FROM contexts WHERE workspace_id = :ws)"
                    ),
                    {"ws": workspace_id},
                )
                conn.execute(
                    text("DELETE FROM contexts WHERE workspace_id = :ws"), {"ws": workspace_id}
                )
                conn.execute(text("DELETE FROM workspaces WHERE id = :ws"), {"ws": workspace_id})
                conn.commit()
        finally:
            engine.dispose()
            holder.dispose()
