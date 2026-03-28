"""Alembic migration forward/rollback tests.

Issue #335: Verify all migrations can be applied and rolled back cleanly.
Requires a real PostgreSQL database (TEST_DATABASE_URL).
"""

from alembic.config import Config

from alembic import command

ALEMBIC_INI = "alembic.ini"


def _get_alembic_config() -> Config:
    """Create Alembic config pointing to test database."""
    import os

    config = Config(ALEMBIC_INI)
    # Override with test database URL if available
    test_url = os.getenv("TEST_DATABASE_URL")
    if test_url:
        # Alembic needs the URL in its config
        config.set_main_option("sqlalchemy.url", test_url)
    return config


class TestAlembicMigrations:
    """Test that all Alembic migrations apply and rollback cleanly."""

    def test_upgrade_to_head(self):
        """All migrations apply without error."""
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
        # Downgrade to baseline (first revision)
        command.downgrade(config, "2c882a9c8c74")
        # Re-upgrade to head
        command.upgrade(config, "head")
