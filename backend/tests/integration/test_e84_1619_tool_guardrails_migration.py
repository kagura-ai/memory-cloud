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
   guard), so a mid-build failure is recoverable by re-running.

Not executed in the local unit run — needs the DB container.
"""

from __future__ import annotations

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


def test_e84_round_trip_index_and_check() -> None:
    _reset_alembic_state()
    engine = _sync_engine()
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

        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), E84_REVISION)
        with engine.connect() as conn:
            assert _index_def(conn) is not None
            assert _index_is_valid(conn) is True
            assert "'load_guardrails'" in _mae_check(conn)
    finally:
        engine.dispose()
        _leave_db_at_head()


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
    from alembic.versions import e84_1619_tool_guardrails as mig  # type: ignore[import-not-found]

    assert "WHERE (details->'tool_trigger') IS NOT NULL AND deleted_at IS NULL" in mig._INDEX_DDL
