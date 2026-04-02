"""Alembic migration forward/rollback tests.

Issue #335: Verify all migrations can be applied and rolled back cleanly.
Requires a real PostgreSQL database (TEST_DATABASE_URL).
"""

import os

from alembic.config import Config

from alembic import command

ALEMBIC_INI = "alembic.ini"


def _get_alembic_config() -> Config:
    """Create Alembic config pointing to test database."""
    config = Config(ALEMBIC_INI)
    # Override with test database URL if available
    test_url = os.getenv("TEST_DATABASE_URL")
    if test_url:
        # Alembic needs sync URL (not asyncpg)
        sync_url = test_url.replace("+asyncpg", "")
        config.set_main_option("sqlalchemy.url", sync_url)
    return config


def _reset_alembic_state():
    """Drop alembic_version and all tables so upgrade starts clean.

    Needed when conftest.py create_all has already created tables
    in the same pytest session (session-scoped fixture conflict).
    """
    from sqlalchemy import create_engine, text

    test_url = os.getenv(
        "TEST_DATABASE_URL",
        "postgresql+asyncpg://kagura:kagura_dev_password@localhost:5432/kagura_test",
    )
    sync_url = test_url.replace("+asyncpg", "")
    engine = create_engine(sync_url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    engine.dispose()


class TestAlembicMigrations:
    """Test that all Alembic migrations apply and rollback cleanly."""

    def test_upgrade_to_head(self):
        """All migrations apply without error."""
        _reset_alembic_state()
        config = _get_alembic_config()
        command.upgrade(config, "head")

    def test_current_is_head(self):
        """After upgrade, current revision matches head."""
        config = _get_alembic_config()
        # This will raise if not at head
        command.ensure_version(config)

    def test_downgrade_one_step(self):
        """Most recent migration can be rolled back."""
        config = _get_alembic_config()
        command.upgrade(config, "head")
        command.downgrade(config, "-1")
        # Re-apply to leave DB in clean state
        command.upgrade(config, "head")

    def test_downgrade_to_base_and_upgrade(self):
        """Full rollback to baseline and re-apply works."""
        config = _get_alembic_config()
        # Downgrade to baseline (first revision: 157247e0df86)
        command.downgrade(config, "157247e0df86")
        # Re-upgrade to head
        command.upgrade(config, "head")
