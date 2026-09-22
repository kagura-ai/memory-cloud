"""Migration round-trip test for the tool-guardrails migration (e84_1619_tool_guardrails).

Mirrors ``test_e29_619_memories_ws_ctx_idx_migration.py`` (concurrent partial
index) and the e74 CHECK-swap precedent:

1. upgrade creates ``idx_memories_tool_trigger`` — a VALID partial B-tree on
   ``context_id`` whose predicate is the json-path presence test the ORM
   ``Index`` declares byte-identically (``test_create_all_vs_alembic_drift``
   catches a mismatch) — and widens ``valid_mae_operation`` with
   ``'load_guardrails'``;
2. downgrade drops the index and restores the previous CHECK;
3. re-running the upgrade is a no-op (``IF NOT EXISTS`` + the invalid-leftover
   guard), so a mid-build failure is recoverable by re-running;
4. a downgrade with ``operation='load_guardrails'`` audit rows present keeps
   the rows (append-only table) and leaves the restored CHECK ``NOT VALID``;
   the next upgrade validates it again.

Not executed in the local unit run — needs the DB container.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path
from types import ModuleType

from sqlalchemy import text
from sqlalchemy.engine import Connection

from alembic import command
from tests.integration.test_alembic_migrations import (
    _alembic_at_test_db,
    _get_alembic_config,
    _reset_alembic_state,
    _sync_engine,
)

E84_REVISION = "e84_1619_tool_guardrails"
PRIOR_HEAD = "e83_1595_beta_invite_label"
INDEX_NAME = "idx_memories_tool_trigger"
_MIGRATION_FILE = (
    Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{E84_REVISION}.py"
)


def _load_migration_module() -> ModuleType:
    """Import the revision file by path — ``backend/alembic/versions`` is not a
    package (no ``__init__.py``), and ``alembic`` resolves to the installed
    library, so ``from alembic.versions import ...`` cannot find it."""
    spec = importlib.util.spec_from_file_location(f"_mig_{E84_REVISION}", _MIGRATION_FILE)
    assert spec is not None and spec.loader is not None, _MIGRATION_FILE
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _leave_db_at_head() -> None:
    with _alembic_at_test_db():
        command.upgrade(_get_alembic_config(), "head")


def _index_def(conn: Connection) -> str | None:
    return conn.execute(
        text("SELECT indexdef FROM pg_indexes WHERE indexname = :n"), {"n": INDEX_NAME}
    ).scalar_one_or_none()


def _index_is_valid(conn: Connection) -> bool | None:
    return conn.execute(
        text(
            "SELECT i.indisvalid FROM pg_class c JOIN pg_index i ON c.oid = i.indexrelid "
            "WHERE c.relname = :n"
        ),
        {"n": INDEX_NAME},
    ).scalar_one_or_none()


def _mae_check(conn: Connection) -> str:
    return conn.execute(
        text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'valid_mae_operation'"
        )
    ).scalar_one()


def _mae_check_validated(conn: Connection) -> bool:
    return conn.execute(
        text("SELECT convalidated FROM pg_constraint WHERE conname = 'valid_mae_operation'")
    ).scalar_one()


def _insert_load_guardrails_audit_row(conn: Connection, user_id: str) -> None:
    """One minimal ``memory_access_events`` row for the new operation — every
    NOT NULL column and every CHECK vocabulary satisfied, nothing else."""
    conn.execute(
        text(
            "INSERT INTO memory_access_events "
            "(workspace_id, user_id, principal_type, surface, operation, outcome) "
            "VALUES (:ws, :uid, 'oauth', 'mcp', 'load_guardrails', 'success')"
        ),
        {"ws": str(uuid.uuid4()), "uid": user_id},
    )
    conn.commit()


def _count_audit_rows(conn: Connection, user_id: str) -> int:
    return conn.execute(
        text("SELECT count(*) FROM memory_access_events WHERE user_id = :uid"), {"uid": user_id}
    ).scalar_one()


def test_e84_round_trip_index_and_check() -> None:
    _reset_alembic_state()
    engine = _sync_engine()
    audit_user = f"e84-audit-{uuid.uuid4().hex[:8]}"
    try:
        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), PRIOR_HEAD)
        with engine.connect() as conn:
            assert _index_def(conn) is None, f"{INDEX_NAME} should not exist before upgrade"
            assert "load_guardrails" not in _mae_check(conn)
            assert "'explore'" in _mae_check(conn)

        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), E84_REVISION)
        with engine.connect() as conn:
            indexdef = _index_def(conn)
            assert indexdef is not None, f"{INDEX_NAME} not created by upgrade"
            assert "(context_id)" in indexdef
            assert "tool_trigger" in indexdef
            assert "IS NOT NULL" in indexdef
            assert "deleted_at IS NULL" in indexdef
            assert _index_is_valid(conn) is True  # no INVALID leftover from CONCURRENTLY
            check = _mae_check(conn)
            assert "'load_guardrails'" in check
            # Appended, never reordered: explore still precedes it.
            assert check.index("'explore'") < check.index("'load_guardrails'")
            assert (
                conn.execute(
                    text(
                        "SELECT convalidated FROM pg_constraint WHERE conname = 'valid_mae_operation'"
                    )
                ).scalar_one()
                is True
            )

        # Re-running the upgrade path is a no-op (IF NOT EXISTS + the
        # invalid-leftover guard): simulate by downgrading only the alembic
        # pointer is not possible, so downgrade + upgrade twice instead.
        with _alembic_at_test_db():
            command.downgrade(_get_alembic_config(), PRIOR_HEAD)
        with engine.connect() as conn:
            assert _index_def(conn) is None, "index should be dropped on downgrade"
            assert "load_guardrails" not in _mae_check(conn)
            assert "'explore'" in _mae_check(conn)
            # No load_guardrails rows: the restored CHECK is fully validated.
            assert _mae_check_validated(conn) is True

        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), E84_REVISION)
        with engine.connect() as conn:
            assert _index_def(conn) is not None
            assert _index_is_valid(conn) is True
            assert "'load_guardrails'" in _mae_check(conn)
            assert _mae_check_validated(conn) is True

        # Audit rows for the new operation survive a downgrade: the table is
        # append-only, so the restored CHECK is left NOT VALID instead of the
        # operator being told to delete them. New writes are still checked.
        with engine.connect() as conn:
            _insert_load_guardrails_audit_row(conn, audit_user)
        with _alembic_at_test_db():
            command.downgrade(_get_alembic_config(), PRIOR_HEAD)
        with engine.connect() as conn:
            assert _count_audit_rows(conn, audit_user) == 1, "downgrade must keep audit rows"
            assert "load_guardrails" not in _mae_check(conn)
            assert _mae_check_validated(conn) is False

        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), E84_REVISION)
        with engine.connect() as conn:
            assert _count_audit_rows(conn, audit_user) == 1
            assert "'load_guardrails'" in _mae_check(conn)
            assert _mae_check_validated(conn) is True  # the wider CHECK validates again
    finally:
        try:
            _leave_db_at_head()
            with engine.connect() as conn:
                conn.execute(
                    text("DELETE FROM memory_access_events WHERE user_id = :uid"),
                    {"uid": audit_user},
                )
                conn.commit()
        finally:
            engine.dispose()


def test_e84_index_predicate_matches_the_orm_declaration() -> None:
    """The ORM ``Index(... postgresql_where=...)`` text and the migration DDL
    must describe the same predicate — pinned at the SQL level so a
    ``create_all`` schema and an alembic schema agree (the drift detector
    compares the two; this test names the exact index)."""
    from models.memory import Memory

    orm_index = next(i for i in Memory.__table__.indexes if i.name == INDEX_NAME)
    assert (
        str(orm_index.dialect_options["postgresql"]["where"])
        == "(details->'tool_trigger') IS NOT NULL AND deleted_at IS NULL"
    )
    mig = _load_migration_module()
    assert "WHERE (details->'tool_trigger') IS NOT NULL AND deleted_at IS NULL" in mig._INDEX_DDL
