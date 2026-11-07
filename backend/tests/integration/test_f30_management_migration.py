"""The delegation schema upgrades and downgrades on a throwaway database."""

from sqlalchemy import inspect

from tests.integration.test_alembic_migrations import (
    _downgrade_to,
    _reset_alembic_state,
    _sync_engine,
    _upgrade_to,
)


def test_management_migration_round_trip():
    _reset_alembic_state()
    engine = _sync_engine()
    try:
        _upgrade_to("f29_2679_observation_credentials")
        _upgrade_to("f30_resource_management_grants")
        with engine.connect() as conn:
            inspector = inspect(conn)
            assert inspector.has_table("resource_management_grants")
            assert inspector.has_table("resource_management_operations")
            columns = {c["name"] for c in inspector.get_columns("resource_management_grants")}
            assert "key_hash" in columns and "secret" not in columns
            assert {
                "workspace_id",
                "resource_pk",
                "context_id",
                "endpoint_id",
                "expires_at",
                "revoked_at",
            }.issubset(columns)
            assert len(inspector.get_foreign_keys("resource_management_grants")) == 4
            assert len(inspector.get_check_constraints("resource_management_grants")) == 2
        _downgrade_to("f29_2679_observation_credentials")
        with engine.connect() as conn:
            assert not inspect(conn).has_table("resource_management_grants")
            assert not inspect(conn).has_table("resource_management_operations")
        _upgrade_to("f30_resource_management_grants")
    finally:
        engine.dispose()
