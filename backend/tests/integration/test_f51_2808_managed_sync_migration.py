"""The new ownership tables round-trip without rewriting deployed migration history."""

from sqlalchemy import inspect, text

from tests.integration.test_alembic_migrations import (
    _downgrade_to,
    _reset_alembic_state,
    _sync_engine,
    _upgrade_to,
)

TABLES = {
    "sync_managed_targets",
    "sync_resume_candidates",
    "sync_resume_previews",
    "sync_mode_operations",
    "sync_manual_retirements",
}


def test_managed_sync_migration_round_trip():
    _reset_alembic_state()
    engine = _sync_engine()
    try:
        _upgrade_to("f50_2807_merge_sync_heads")
        with engine.connect() as conn:
            old_tables = set(inspect(conn).get_table_names())
        _upgrade_to("f51_2808_managed_sync")
        with engine.connect() as conn:
            inspector = inspect(conn)
            assert set(inspector.get_table_names()) == old_tables | TABLES
            assert len(inspector.get_foreign_keys("sync_managed_targets")) == 1
            assert any(
                "stopping" in row["sqltext"]
                for row in inspector.get_check_constraints("sync_managed_targets")
            )
            assert inspector.get_columns("sync_resume_candidates")[7]["name"] == "records"
            assert (
                conn.scalar(text("SELECT version_num FROM alembic_version"))
                == "f51_2808_managed_sync"
            )
        _downgrade_to("f50_2807_merge_sync_heads")
        with engine.connect() as conn:
            assert set(inspect(conn).get_table_names()) == old_tables
        _upgrade_to("head")
    finally:
        # Leave no test-owned user or schema state for later DB fixture sessions.
        _reset_alembic_state()
        _upgrade_to("head")
        engine.dispose()
