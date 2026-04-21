"""Migration smoke test for b04_358_signup_gate (Issue #358 Phase 1).

TestAlembicMigrations (same module as the imports below) already covers the
mechanical forward + rollback paths once this migration becomes head. These
tests add a data-shape check on top: verify that after upgrade the expected
tables exist AND the singleton config row has been seeded with OSS-preserving
defaults (enabled=false, mode=manual).
"""

from sqlalchemy import inspect, text

from alembic import command
from tests.integration.test_alembic_migrations import (
    _alembic_at_test_db,
    _get_alembic_config,
    _reset_alembic_state,
    _sync_engine,
)


class TestSignupGateMigration:
    """Data-shape checks for b04_358_signup_gate."""

    def test_upgrade_creates_tables_and_seeds_singleton(self):
        _reset_alembic_state()
        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), "head")

        engine = _sync_engine()
        try:
            inspector = inspect(engine)
            tables = set(inspector.get_table_names())
            assert "signup_gate_config" in tables
            assert "signup_allowlist" in tables

            with engine.begin() as conn:
                rows = conn.execute(
                    text("SELECT id, enabled, mode FROM signup_gate_config")
                ).fetchall()
            # Singleton: exactly one row with id=1 and OSS-preserving defaults.
            assert len(rows) == 1
            assert rows[0][0] == 1
            assert rows[0][1] is False
            assert rows[0][2] == "manual"
        finally:
            engine.dispose()

    def test_downgrade_drops_tables(self):
        _reset_alembic_state()
        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), "head")
            command.downgrade(_get_alembic_config(), "-1")

        engine = _sync_engine()
        try:
            inspector = inspect(engine)
            tables = set(inspector.get_table_names())
            assert "signup_gate_config" not in tables
            assert "signup_allowlist" not in tables
        finally:
            engine.dispose()

        # Leave the DB at head so the rest of the integration suite sees a
        # clean, fully-migrated schema (same convention as TestAlembicMigrations
        # in the sibling module).
        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), "head")
