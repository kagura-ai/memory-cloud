"""Integration tests for migration ``e28_850_workspace_connectors`` (#850, F6-a of #755).

Covers the DB-level schema behaviour that ``TestAlembicMigrations`` does not:

1. ``upgrade()`` creates ``workspace_connectors``; ``resource_pk`` is NOT NULL
   from creation (no Phase-1 nullable shadow window for a brand-new table).
2. The UNIQUE on ``resource_pk`` enforces the 1:1 connector -> resource contract
   (a second connector for the same resource is rejected).
3. The ``check_connector_type`` CHECK accepts ``slack`` / ``discord`` / ``teams``
   and rejects anything else.
4. Deleting the parent ``resources`` row CASCADE-deletes the connector.
5. ``downgrade()`` drops the table.

Pre-revision is ``e27_805_drop_ws_memory_limit`` — the head just before e28.
Mirrors ``test_e16_709_embedding_spend_cap_migration.py``.
"""

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

# Pinned so the test stays correct as more migrations land on top of e28.
PRE_E28_REV = "e27_805_drop_ws_memory_limit"


def _seed_user(conn, user_id: str | None = None) -> str:
    uid = user_id or f"u-{uuid.uuid4().hex[:12]}"
    conn.execute(
        text(
            "INSERT INTO users "
            "(email, user_id, role, timezone, locale, is_initial_admin) "
            "VALUES (:email, :uid, 'user', 'UTC', 'en', false)"
        ),
        {"email": f"{uid}@test.example", "uid": uid},
    )
    return uid


def _seed_workspace(conn, owner_user_id: str) -> str:
    ws_id = str(uuid.uuid4())
    conn.execute(
        text("INSERT INTO workspaces (id, name, owner_user_id) VALUES (:id, :name, :owner)"),
        {"id": ws_id, "name": f"ws-{ws_id[:8]}", "owner": owner_user_id},
    )
    return ws_id


def _seed_resource(conn, ws_id: str, slug: str | None = None) -> str:
    res_id = str(uuid.uuid4())
    conn.execute(
        text("INSERT INTO resources (id, workspace_id, resource_id) VALUES (:id, :ws, :slug)"),
        {"id": res_id, "ws": ws_id, "slug": slug or f"r-{res_id[:8]}"},
    )
    return res_id


def _insert_connector(conn, *, resource_pk: str, workspace_id: str, connector_type: str = "slack"):
    cid = str(uuid.uuid4())
    conn.execute(
        text(
            "INSERT INTO workspace_connectors "
            "(id, resource_pk, workspace_id, connector_type) "
            "VALUES (:id, :rpk, :ws, :ct)"
        ),
        {"id": cid, "rpk": resource_pk, "ws": workspace_id, "ct": connector_type},
    )
    return cid


def _leave_db_at_head() -> None:
    with _alembic_at_test_db():
        command.upgrade(_get_alembic_config(), "head")


class TestE28WorkspaceConnectorsMigration:
    def test_upgrade_creates_table_with_expected_columns(self):
        _reset_alembic_state()
        try:
            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), PRE_E28_REV)
            insp = inspect(_sync_engine())
            assert "workspace_connectors" not in insp.get_table_names()

            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), "e28_850_workspace_connectors")
            insp = inspect(_sync_engine())
            cols = {c["name"]: c for c in insp.get_columns("workspace_connectors")}
            expected = {
                "id",
                "resource_pk",
                "workspace_id",
                "connector_type",
                "oauth_tokens_encrypted",
                "pii_guardrail_config",
                "litellm_virtual_key_id",
                "config_version",
                "virtual_key_valid_until",
                "created_by",
                "created_at",
                "updated_at",
            }
            assert expected <= set(cols)
            # resource_pk is NOT NULL from creation (no shadow window).
            assert cols["resource_pk"]["nullable"] is False
        finally:
            _leave_db_at_head()

    def test_unique_resource_pk_enforces_one_to_one(self):
        _reset_alembic_state()
        try:
            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), "e28_850_workspace_connectors")
            eng = _sync_engine()
            with eng.begin() as conn:
                uid = _seed_user(conn)
                ws = _seed_workspace(conn, uid)
                res = _seed_resource(conn, ws)
                _insert_connector(conn, resource_pk=res, workspace_id=ws)
            # Second connector for the SAME resource must violate the UNIQUE.
            with pytest.raises(IntegrityError) as exc_info, eng.begin() as conn:
                _insert_connector(conn, resource_pk=res, workspace_id=ws, connector_type="discord")
            # Pin the failure to the 1:1 constraint, not some unrelated future
            # NOT NULL/FK that might also reject the second insert.
            assert "uq_workspace_connectors_resource_pk" in str(exc_info.value)
        finally:
            _leave_db_at_head()

    def test_connector_type_check_constraint(self):
        _reset_alembic_state()
        try:
            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), "e28_850_workspace_connectors")
            eng = _sync_engine()
            # Valid types accepted.
            for ct in ("slack", "discord", "teams"):
                with eng.begin() as conn:
                    uid = _seed_user(conn)
                    ws = _seed_workspace(conn, uid)
                    res = _seed_resource(conn, ws)
                    _insert_connector(conn, resource_pk=res, workspace_id=ws, connector_type=ct)
            # Invalid type rejected by check_connector_type.
            with pytest.raises(IntegrityError), eng.begin() as conn:
                uid = _seed_user(conn)
                ws = _seed_workspace(conn, uid)
                res = _seed_resource(conn, ws)
                _insert_connector(conn, resource_pk=res, workspace_id=ws, connector_type="telegram")
        finally:
            _leave_db_at_head()

    def test_resource_delete_cascades_to_connector(self):
        _reset_alembic_state()
        try:
            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), "e28_850_workspace_connectors")
            eng = _sync_engine()
            with eng.begin() as conn:
                uid = _seed_user(conn)
                ws = _seed_workspace(conn, uid)
                res = _seed_resource(conn, ws)
                _insert_connector(conn, resource_pk=res, workspace_id=ws)
            with eng.begin() as conn:
                conn.execute(text("DELETE FROM resources WHERE id = :id"), {"id": res})
            with eng.connect() as conn:
                remaining = conn.execute(
                    text("SELECT count(*) FROM workspace_connectors WHERE resource_pk = :rpk"),
                    {"rpk": res},
                ).scalar()
            assert remaining == 0
        finally:
            _leave_db_at_head()

    def test_workspace_delete_cascades_to_connector(self):
        """Deleting the owning workspace must not orphan or block on the
        connector — the workspace_id FK is ON DELETE CASCADE (independent of
        the resource_pk CASCADE path)."""
        _reset_alembic_state()
        try:
            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), "e28_850_workspace_connectors")
            eng = _sync_engine()
            with eng.begin() as conn:
                uid = _seed_user(conn)
                ws = _seed_workspace(conn, uid)
                res = _seed_resource(conn, ws)
                _insert_connector(conn, resource_pk=res, workspace_id=ws)
            with eng.begin() as conn:
                conn.execute(text("DELETE FROM workspaces WHERE id = :id"), {"id": ws})
            with eng.connect() as conn:
                remaining = conn.execute(
                    text("SELECT count(*) FROM workspace_connectors WHERE workspace_id = :ws"),
                    {"ws": ws},
                ).scalar()
            assert remaining == 0
        finally:
            _leave_db_at_head()

    def test_downgrade_drops_table(self):
        _reset_alembic_state()
        try:
            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), "e28_850_workspace_connectors")
            assert "workspace_connectors" in inspect(_sync_engine()).get_table_names()

            with _alembic_at_test_db():
                command.downgrade(_get_alembic_config(), PRE_E28_REV)
            assert "workspace_connectors" not in inspect(_sync_engine()).get_table_names()
        finally:
            _leave_db_at_head()
