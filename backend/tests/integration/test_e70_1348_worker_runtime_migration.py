"""DB contract for nullable connector runtime JSONB migration e70 (#1348)."""

import uuid

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection

from alembic import command
from tests.integration.test_alembic_migrations import (
    _alembic_at_test_db,
    _get_alembic_config,
    _reset_alembic_state,
    _sync_engine,
)

PRE_E70_REV = "e69_1331_location_cols"
E70_REV = "e70_1348_worker_runtime"


def _seed_connector(conn: Connection) -> str:
    suffix = uuid.uuid4().hex[:12]
    user_id = f"u-{suffix}"
    workspace_id = str(uuid.uuid4())
    resource_pk = str(uuid.uuid4())
    connector_id = str(uuid.uuid4())
    conn.execute(
        text(
            "INSERT INTO users "
            "(email, user_id, role, timezone, locale, is_initial_admin) "
            "VALUES (:email, :uid, 'user', 'UTC', 'en', false)"
        ),
        {"email": f"{user_id}@test.example", "uid": user_id},
    )
    conn.execute(
        text("INSERT INTO workspaces (id, name, owner_user_id) VALUES (:id, :name, :owner)"),
        {"id": workspace_id, "name": f"ws-{suffix}", "owner": user_id},
    )
    conn.execute(
        text("INSERT INTO resources (id, workspace_id, resource_id) VALUES (:id, :ws, :slug)"),
        {"id": resource_pk, "ws": workspace_id, "slug": f"r-{suffix}"},
    )
    conn.execute(
        text(
            "INSERT INTO workspace_connectors "
            "(id, resource_pk, workspace_id, connector_type) "
            "VALUES (:id, :resource, :workspace, 'slack')"
        ),
        {"id": connector_id, "resource": resource_pk, "workspace": workspace_id},
    )
    return connector_id


def _leave_db_at_head() -> None:
    with _alembic_at_test_db():
        command.upgrade(_get_alembic_config(), "head")


class TestE70WorkerRuntimeMigration:
    def test_upgrade_keeps_old_rows_null_and_accepts_jsonb(self) -> None:
        _reset_alembic_state()
        try:
            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), PRE_E70_REV)
            engine = _sync_engine()
            with engine.begin() as conn:
                connector_id = _seed_connector(conn)

            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), E70_REV)

            columns = {
                column["name"]: column
                for column in inspect(engine).get_columns("workspace_connectors")
            }
            assert columns["runtime_config"]["nullable"] is True
            with engine.begin() as conn:
                assert (
                    conn.execute(
                        text("SELECT runtime_config FROM workspace_connectors WHERE id = :id"),
                        {"id": connector_id},
                    ).scalar_one()
                    is None
                )
                conn.execute(
                    text(
                        "UPDATE workspace_connectors "
                        "SET runtime_config = CAST(:runtime AS jsonb) WHERE id = :id"
                    ),
                    {"id": connector_id, "runtime": '{"vision_enabled": false}'},
                )
                assert (
                    conn.execute(
                        text(
                            "SELECT runtime_config->>'vision_enabled' "
                            "FROM workspace_connectors WHERE id = :id"
                        ),
                        {"id": connector_id},
                    ).scalar_one()
                    == "false"
                )
        finally:
            _leave_db_at_head()

    def test_downgrade_drops_runtime_column(self) -> None:
        _reset_alembic_state()
        try:
            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), E70_REV)
                command.downgrade(_get_alembic_config(), PRE_E70_REV)
            columns = {
                column["name"]
                for column in inspect(_sync_engine()).get_columns("workspace_connectors")
            }
            assert "runtime_config" not in columns
        finally:
            _leave_db_at_head()
