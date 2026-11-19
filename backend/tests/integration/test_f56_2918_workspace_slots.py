"""#2918 (f56): the two slot tables, created without touching an existing table.

The upgrade runs while another transaction holds ``ACCESS EXCLUSIVE`` on
``workspaces`` and ``users``: a revision that needed any lock on them would
wait into the migration ``lock_timeout`` and fail. A rerun (the revision
stamped back, the tables still there) is a no-op; downgrade drops both tables
and keeps every existing row.
"""

from uuid import uuid4

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from tests.integration.test_alembic_migrations import (
    _downgrade_to,
    _reset_alembic_state,
    _sync_engine,
    _upgrade_to,
)

BEFORE = "f55_2916_sync_run_retention"
REVISION = "f56_2918_workspace_slots"
TABLES = {"workspace_slot_states", "workspace_slot_suspensions"}


def _version(engine) -> str:
    with engine.connect() as conn:
        return conn.scalar(text("SELECT version_num FROM alembic_version"))


def _type_name(column_type) -> str:
    item = getattr(column_type, "item_type", None)
    return f"{item}[]" if item is not None else str(column_type)


def test_slot_tables_round_trip_without_locking_existing_tables():
    engine = _sync_engine()
    holder = _sync_engine()
    wid = uuid4()
    try:
        _reset_alembic_state()
        _upgrade_to(BEFORE)
        with engine.begin() as db:
            db.execute(
                text("INSERT INTO workspaces (id, name, owner_user_id) VALUES (:id, 'Keep', 'o')"),
                {"id": wid},
            )

        # Upgrade while workspaces and users are locked against everything.
        with holder.connect() as lock_conn:
            lock_tx = lock_conn.begin()
            lock_conn.execute(text("LOCK TABLE workspaces, users IN ACCESS EXCLUSIVE MODE"))
            try:
                _upgrade_to(REVISION)
            finally:
                lock_tx.rollback()
        assert _version(engine) == REVISION
        assert TABLES <= set(inspect(engine).get_table_names())

        columns = {
            c["name"]: (_type_name(c["type"]), c["nullable"])
            for c in inspect(engine).get_columns("workspace_slot_states")
        }
        assert columns == {
            "owner_user_id": ("VARCHAR(255)", False),
            "keep_workspace_ids": ("UUID[]", False),
            "selection_saved_at": ("TIMESTAMP", True),
            "selection_required_since": ("TIMESTAMP", True),
            "version": ("BIGINT", False),
            "updated_at": ("TIMESTAMP", False),
        }
        assert [i["name"] for i in inspect(engine).get_indexes("workspace_slot_suspensions")] == [
            "ix_workspace_slot_suspensions_owner_user_id"
        ]
        assert not inspect(engine).get_foreign_keys("workspace_slot_suspensions")
        assert not inspect(engine).get_foreign_keys("workspace_slot_states")

        with engine.begin() as db:
            db.execute(
                text(
                    "INSERT INTO workspace_slot_states (owner_user_id, updated_at) VALUES ('o', now())"
                )
            )
            row = db.execute(
                text(
                    "SELECT keep_workspace_ids, version, selection_saved_at "
                    "FROM workspace_slot_states WHERE owner_user_id = 'o'"
                )
            ).one()
            assert (list(row[0]), row[1], row[2]) == ([], 0, None)
            db.execute(
                text(
                    "INSERT INTO workspace_slot_suspensions "
                    "(workspace_id, owner_user_id, cause, suspended_at) "
                    "VALUES (:id, 'o', 'billing_snapshot', now())"
                ),
                {"id": wid},
            )
            assert (
                db.scalar(
                    text("SELECT reason FROM workspace_slot_suspensions WHERE workspace_id = :id"),
                    {"id": wid},
                )
                == "allowance"
            )
        with pytest.raises(IntegrityError) as rejected, engine.begin() as db:
            db.execute(
                text(
                    "INSERT INTO workspace_slot_suspensions "
                    "(workspace_id, owner_user_id, reason, cause, suspended_at) "
                    "VALUES (:id, 'o', 'other', 'billing_snapshot', now())"
                ),
                {"id": uuid4()},
            )
        assert (
            getattr(rejected.value.orig, "sqlstate", None)
            or getattr(rejected.value.orig, "pgcode", None)
        ) == "23514"

        # A rerun after an interrupted upgrade: the tables exist, the revision
        # is not recorded. The upgrade is a no-op and keeps the rows.
        with engine.begin() as db:
            db.execute(text("UPDATE alembic_version SET version_num = :v"), {"v": BEFORE})
        _upgrade_to(REVISION)
        assert _version(engine) == REVISION
        with engine.connect() as db:
            assert db.scalar(text("SELECT count(*) FROM workspace_slot_suspensions")) == 1

        _downgrade_to(BEFORE)
        assert not TABLES & set(inspect(engine).get_table_names())
        with engine.connect() as db:
            assert (
                db.scalar(text("SELECT name FROM workspaces WHERE id = :id"), {"id": wid}) == "Keep"
            )
        _upgrade_to(REVISION)
        assert TABLES <= set(inspect(engine).get_table_names())
    finally:
        try:
            _reset_alembic_state()
            _upgrade_to("head")
        finally:
            engine.dispose()
            holder.dispose()
