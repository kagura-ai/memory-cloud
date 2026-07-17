"""DB contract tests for worker app identity migration e68 (#1315)."""

import uuid

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from alembic import command
from tests.integration.test_alembic_migrations import (
    _alembic_at_test_db,
    _get_alembic_config,
    _reset_alembic_state,
    _sync_engine,
)

PRE_E68_REV = "e67_1281_agent_ws"
E68_REV = "e68_1315_worker_apps"


def _seed_connector(conn, *, team_id: str) -> tuple[str, str]:
    suffix = uuid.uuid4().hex[:12]
    user_id = f"u-{suffix}"
    workspace_id = str(uuid.uuid4())
    resource_id = str(uuid.uuid4())
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
        {"id": resource_id, "ws": workspace_id, "slug": f"r-{suffix}"},
    )
    conn.execute(
        text(
            "INSERT INTO workspace_connectors "
            "(id, resource_pk, workspace_id, connector_type, external_team_id) "
            "VALUES (:id, :resource, :workspace, 'slack', :team)"
        ),
        {
            "id": connector_id,
            "resource": resource_id,
            "workspace": workspace_id,
            "team": team_id,
        },
    )
    return connector_id, workspace_id


def _leave_db_at_head() -> None:
    with _alembic_at_test_db():
        command.upgrade(_get_alembic_config(), "head")


class TestE68WorkerAppMigration:
    def test_backfills_default_and_allows_same_team_under_two_apps(self):
        _reset_alembic_state()
        try:
            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), PRE_E68_REV)
            engine = _sync_engine()
            with engine.begin() as conn:
                connector_id, workspace_id = _seed_connector(conn, team_id="T0SHARED")

            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), E68_REV)

            with engine.begin() as conn:
                assert (
                    conn.execute(
                        text("SELECT app_key FROM workspace_connectors WHERE id = :id"),
                        {"id": connector_id},
                    ).scalar_one()
                    == "default"
                )
                assert (
                    conn.execute(
                        text(
                            "SELECT status FROM worker_app_identities "
                            "WHERE platform = 'slack' AND app_key = 'default'"
                        )
                    ).scalar_one()
                    == "unconfigured"
                )
                conn.execute(
                    text(
                        "INSERT INTO worker_app_identities "
                        "(platform, app_key, display_name, status) "
                        "VALUES ('slack', 'sales', 'Sales', 'active')"
                    )
                )
                second_resource = str(uuid.uuid4())
                conn.execute(
                    text(
                        "INSERT INTO resources (id, workspace_id, resource_id) "
                        "VALUES (:id, :ws, :slug)"
                    ),
                    {"id": second_resource, "ws": workspace_id, "slug": f"r-{uuid.uuid4().hex}"},
                )
                conn.execute(
                    text(
                        "INSERT INTO workspace_connectors "
                        "(resource_pk, workspace_id, connector_type, app_key, external_team_id) "
                        "VALUES (:resource, :workspace, 'slack', 'sales', 'T0SHARED')"
                    ),
                    {"resource": second_resource, "workspace": workspace_id},
                )

            with pytest.raises(IntegrityError), engine.begin() as conn:
                third_resource = str(uuid.uuid4())
                conn.execute(
                    text(
                        "INSERT INTO resources (id, workspace_id, resource_id) "
                        "VALUES (:id, :ws, :slug)"
                    ),
                    {"id": third_resource, "ws": workspace_id, "slug": f"r-{uuid.uuid4().hex}"},
                )
                conn.execute(
                    text(
                        "INSERT INTO workspace_connectors "
                        "(resource_pk, workspace_id, connector_type, app_key, external_team_id) "
                        "VALUES (:resource, :workspace, 'slack', 'sales', 'T0SHARED')"
                    ),
                    {"resource": third_resource, "workspace": workspace_id},
                )
        finally:
            _leave_db_at_head()

    def test_downgrade_restores_legacy_schema_without_ambiguous_rows(self):
        _reset_alembic_state()
        try:
            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), PRE_E68_REV)
            engine = _sync_engine()
            with engine.begin() as conn:
                _seed_connector(conn, team_id="T0ONE")
            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), E68_REV)
                command.downgrade(_get_alembic_config(), PRE_E68_REV)

            inspector = inspect(engine)
            assert "worker_app_identities" not in inspector.get_table_names()
            columns = {column["name"] for column in inspector.get_columns("workspace_connectors")}
            assert "app_key" not in columns
            indexes = {
                index["name"]: index for index in inspector.get_indexes("workspace_connectors")
            }
            assert "ix_workspace_connectors_type_team" in indexes
            # Name alone is not enough: the legacy index is the cross-tenant
            # dispatch-hijack guard, so losing unique=True on downgrade would
            # silently allow a second connector for the same Slack team.
            assert indexes["ix_workspace_connectors_type_team"]["unique"] is True
        finally:
            _leave_db_at_head()
