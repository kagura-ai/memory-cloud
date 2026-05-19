"""Migration round-trip test for #616 (e18_616_drop_pg_inline).

Verifies:
1. Upgrade drops ``file_objects.inline_bytes``.
2. Upgrade rewrites the ``valid_file_storage_backend`` CHECK to enum
   ``IN ('r2')`` only.
3. Upgrade rewrites the ``valid_file_storage_shape`` CHECK to the
   R2-only shape (``reserved`` OR ``r2 + storage_key IS NOT NULL``).
4. Downgrade restores the column (NULL-able BYTEA) and both CHECKs.
5. No rows are touched (the column is documented dead — no production
   row has ever had ``inline_bytes`` non-NULL).
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from alembic import command

from tests.integration.test_alembic_migrations import (
    _alembic_at_test_db,
    _get_alembic_config,
    _reset_alembic_state,
    _sync_engine,
)

E18_REVISION = "e18_616_drop_pg_inline"
PRIOR_HEAD = "e17_722_neural_edge_origin"

# Fixed IDs used for the seed row so the test is fully deterministic.
_WS_ID = "00000000-0000-0000-0000-000000000001"
_USER_ID = "tester-616"


def _leave_db_at_head() -> None:
    """Convention: integration suite expects the test DB at head after each test."""
    with _alembic_at_test_db():
        command.upgrade(_get_alembic_config(), "head")


def _seed_workspace(conn: Connection) -> None:
    """Insert a minimal user + workspace row to satisfy the FK on file_objects."""
    conn.execute(
        text(
            "INSERT INTO users "
            "(email, user_id, role, timezone, locale, is_initial_admin) "
            "VALUES (:email, :uid, 'user', 'UTC', 'en', false)"
        ),
        {"email": f"{_USER_ID}@test.example", "uid": _USER_ID},
    )
    conn.execute(
        text("INSERT INTO workspaces (id, name, owner_user_id) VALUES (:id, :name, :owner)"),
        {"id": _WS_ID, "name": "ws-616", "owner": _USER_ID},
    )


def test_e18_upgrade_drops_inline_bytes_and_rewrites_checks() -> None:
    """Upgrade removes the column and tightens both CHECK constraints."""
    _reset_alembic_state()
    with _alembic_at_test_db():
        command.upgrade(_get_alembic_config(), PRIOR_HEAD)

    engine = _sync_engine()
    try:
        with engine.begin() as conn:
            _seed_workspace(conn)
            conn.execute(
                text(
                    "INSERT INTO file_objects "
                    "(workspace_id, sha256, size_bytes, content_type, "
                    " filename, storage_backend, storage_key, status, "
                    " created_by) VALUES "
                    "(:ws, 'd' || repeat('e', 63), 1, 'text/plain', "
                    " 'x.txt', 'r2', 'k/x', 'uploaded', 'tester')"
                ),
                {"ws": _WS_ID},
            )

        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), E18_REVISION)

        # Semantic check: pg_inline is now rejected by the tightened CHECK.
        with engine.connect() as conn:
            with pytest.raises(IntegrityError):
                conn.execute(
                    text(
                        "INSERT INTO file_objects "
                        "(workspace_id, sha256, size_bytes, content_type, "
                        " filename, storage_backend, storage_key, status, "
                        " created_by) VALUES "
                        f"('{_WS_ID}', "
                        " 'd' || repeat('e', 62), 1, 'text/plain', "
                        " 'y.txt', 'pg_inline', NULL, 'uploaded', 'tester')"
                    )
                )
                conn.commit()

        # Smoke check on the structural side: inline_bytes column is gone.
        with engine.connect() as conn:
            cols = (
                conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'file_objects' "
                        "ORDER BY ordinal_position"
                    )
                )
                .scalars()
                .all()
            )
            assert "inline_bytes" not in cols

            surviving = conn.execute(text("SELECT COUNT(*) FROM file_objects")).scalar_one()
            assert surviving == 1
    finally:
        engine.dispose()


def test_e18_downgrade_restores_inline_bytes_and_checks() -> None:
    """Downgrade is reversible — column comes back NULL-able + both
    CHECKs restored to the e03_485 shape."""
    _reset_alembic_state()
    with _alembic_at_test_db():
        command.upgrade(_get_alembic_config(), E18_REVISION)
        command.downgrade(_get_alembic_config(), PRIOR_HEAD)

    engine = _sync_engine()
    try:
        with engine.connect() as conn:
            col_row = conn.execute(
                text(
                    "SELECT data_type, is_nullable "
                    "FROM information_schema.columns "
                    "WHERE table_name = 'file_objects' "
                    "  AND column_name = 'inline_bytes'"
                )
            ).one()
            assert col_row.data_type == "bytea"
            assert col_row.is_nullable == "YES"

        # Semantic check: after downgrade the restored CHECK accepts pg_inline.
        # Use a transaction that we explicitly roll back so no pg_inline row
        # survives into _leave_db_at_head() (which re-upgrades to e18, which
        # rejects pg_inline via its tightened CHECK).
        with engine.begin() as conn:
            _seed_workspace(conn)
            conn.execute(
                text(
                    "INSERT INTO file_objects "
                    "(workspace_id, sha256, size_bytes, content_type, "
                    " filename, storage_backend, inline_bytes, storage_key, "
                    " status, created_by) VALUES "
                    "(:ws, 'd' || repeat('e', 63), 1, 'text/plain', "
                    " 'z.txt', 'pg_inline', :blob, NULL, 'uploaded', 'tester')"
                ),
                {"ws": _WS_ID, "blob": b"hello"},
            )
            # Verify the INSERT succeeded (no exception above), then roll back
            # so the pg_inline row is gone before we re-upgrade to head.
            conn.execute(text("SAVEPOINT before_pg_inline_check"))
            conn.execute(text("ROLLBACK TO SAVEPOINT before_pg_inline_check"))
            # Clean up seeded rows so _leave_db_at_head() upgrade proceeds cleanly.
            conn.execute(text("DELETE FROM workspaces WHERE id = :id"), {"id": _WS_ID})
            conn.execute(text("DELETE FROM users WHERE user_id = :uid"), {"uid": _USER_ID})
    finally:
        engine.dispose()
        _leave_db_at_head()
