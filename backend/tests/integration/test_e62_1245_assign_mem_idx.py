"""Migration round-trip test for #1245 (e62_1245_assign_mem_idx).

Verifies (mirrors the e29_619 pattern):
1. Upgrade builds ``idx_memory_analysis_assignments_memory`` on
   ``memory_analysis_assignments (memory_id)`` — the leading-column index
   the memories ON DELETE CASCADE RI trigger needs.
2. Downgrade drops it; the pre-existing cluster indexes survive.

Complements the ORM-side parity pin in
``tests/services/analysis/test_query_service.py`` — without this, a rename
or typo in the migration would ship alembic-provisioned databases without
the index while the create_all-based suite stayed green.
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

E62_REVISION = "e62_1245_assign_mem_idx"
PRIOR_HEAD = "e61_1240_one_running_uq"
INDEX_NAME = "idx_memory_analysis_assignments_memory"


def _index_def(conn: Connection) -> str | None:
    """Return the ``pg_indexes`` definition for the FK index, or None."""
    return conn.execute(
        text("SELECT indexdef FROM pg_indexes WHERE indexname = :n"),
        {"n": INDEX_NAME},
    ).scalar_one_or_none()


def _leave_db_at_head() -> None:
    """Convention: integration suite expects the test DB at head after each test."""
    with _alembic_at_test_db():
        command.upgrade(_get_alembic_config(), "head")


def test_e62_upgrade_creates_memory_id_index() -> None:
    """Upgrade builds the memory_id leading-column index."""
    _reset_alembic_state()
    with _alembic_at_test_db():
        command.upgrade(_get_alembic_config(), PRIOR_HEAD)

    engine = _sync_engine()
    try:
        with engine.connect() as conn:
            assert _index_def(conn) is None, f"{INDEX_NAME} should not exist before upgrade"

        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), E62_REVISION)

        with engine.connect() as conn:
            indexdef = _index_def(conn)
            assert indexdef is not None, f"{INDEX_NAME} not created by upgrade"
            assert "memory_id" in indexdef
            assert "memory_analysis_assignments" in indexdef
    finally:
        engine.dispose()
        _leave_db_at_head()


def test_e62_downgrade_drops_index() -> None:
    """Downgrade removes the FK index; the cluster indexes survive."""
    _reset_alembic_state()
    with _alembic_at_test_db():
        command.upgrade(_get_alembic_config(), E62_REVISION)
        command.downgrade(_get_alembic_config(), PRIOR_HEAD)

    engine = _sync_engine()
    try:
        with engine.connect() as conn:
            assert _index_def(conn) is None, "index should be dropped on downgrade"
            surviving = (
                conn.execute(
                    text(
                        "SELECT indexname FROM pg_indexes "
                        "WHERE tablename = 'memory_analysis_assignments' "
                        "AND indexname IN ("
                        "'idx_memory_analysis_assignments_cluster', "
                        "'idx_memory_analysis_assignments_analysis_cluster')"
                    )
                )
                .scalars()
                .all()
            )
            assert len(surviving) == 2, f"pre-existing cluster indexes affected: {surviving}"
    finally:
        engine.dispose()
        _leave_db_at_head()
