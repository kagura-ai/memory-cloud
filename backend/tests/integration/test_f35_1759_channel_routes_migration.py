"""Migration round trip for f35_1759_channel_routes (#1759).

1. upgrade adds ``workspace_connectors.unrouted_private_channels`` (NOT NULL,
   server default ``default``, its CHECK validated) and the
   ``workspace_connector_channel_routes`` table with its two CHECKs and its
   foreign keys (connector CASCADE, context SET NULL);
2. the downgrade is refused while a connector still uses routing, and runs
   once none does;
3. re-upgrade works (the steps before the VALIDATE commit are re-runnable).

A file of its own, like the other ``test_<revision>_..._migration.py`` files:
``_reset_alembic_state`` rebuilds the schema through every migration, seed
rows included (e.g. the global ``neural_config`` defaults), so it must not
run ahead of the numbered ``test_<issue>_..._db.py`` files, which assume the
``create_all`` schema without seed rows.

Not executed in the local unit run — needs the DB container.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text


def test_f35_round_trip_and_the_guarded_downgrade() -> None:
    from tests.integration.test_alembic_migrations import (
        TestB03NeuralEdgesBackfillMigration as _B03,
    )
    from tests.integration.test_alembic_migrations import (
        _downgrade_to,
        _reset_alembic_state,
        _sync_engine,
        _upgrade_to,
    )

    prior = "f34_2739_history_retention_addon"
    _reset_alembic_state()
    engine = _sync_engine()

    def table_exists(conn) -> bool:
        return (
            conn.execute(text("SELECT to_regclass('workspace_connector_channel_routes')")).scalar()
            is not None
        )

    def column_default(conn):
        return conn.execute(
            text(
                "SELECT column_default, is_nullable FROM information_schema.columns "
                "WHERE table_name = 'workspace_connectors' "
                "AND column_name = 'unrouted_private_channels'"
            )
        ).first()

    try:
        _upgrade_to(prior)
        with engine.connect() as conn:
            assert not table_exists(conn)
            assert column_default(conn) is None
        _upgrade_to("f35_1759_channel_routes")
        with engine.connect() as conn:
            assert table_exists(conn)
            default, nullable = column_default(conn)
            assert "default" in default and nullable == "NO"
            checks = set(
                conn.execute(
                    text(
                        "SELECT conname FROM pg_constraint WHERE convalidated AND conname IN "
                        "('check_workspace_connectors_unrouted_private_channels', "
                        "'check_workspace_connector_channel_routes_action', "
                        "'check_workspace_connector_channel_routes_skip_no_context')"
                    )
                ).scalars()
            )
            assert len(checks) == 3
            fks = dict(
                conn.execute(
                    text(
                        "SELECT conname, confdeltype FROM pg_constraint WHERE conrelid = "
                        "'workspace_connector_channel_routes'::regclass AND contype = 'f'"
                    )
                ).all()
            )
            assert fks == {
                "fk_workspace_connector_channel_routes_connector_id": "c",
                "fk_workspace_connector_channel_routes_context_id": "n",
            }

        # A connector with the switch on blocks the downgrade.
        with engine.begin() as conn:
            ws, _ = _B03._seed_workspace_and_context(conn, "f35-owner")
            conn.execute(
                text(
                    "INSERT INTO resources (id, workspace_id, resource_id, name) "
                    "VALUES (:r, :w, 'f35-res', 'f35')"
                ),
                {"r": (res := uuid4()), "w": ws},
            )
            conn.execute(
                text(
                    "INSERT INTO workspace_connectors "
                    "(id, resource_pk, workspace_id, connector_type, unrouted_private_channels) "
                    "VALUES (:c, :r, :w, 'slack', 'skip')"
                ),
                {"c": (cid := uuid4()), "r": res, "w": ws},
            )
        with pytest.raises(RuntimeError, match="downgrade refused"):
            _downgrade_to(prior)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE workspace_connectors SET unrouted_private_channels = 'default' "
                    "WHERE id = :c"
                ),
                {"c": cid},
            )
        _downgrade_to(prior)
        with engine.connect() as conn:
            assert not table_exists(conn)
            assert column_default(conn) is None
        # Re-run: the upgrade's steps before the VALIDATE commit are re-runnable.
        _upgrade_to("f35_1759_channel_routes")
        with engine.connect() as conn:
            assert table_exists(conn)
    finally:
        try:
            _upgrade_to("head")
        finally:
            try:
                with engine.begin() as conn:
                    conn.execute(text("DELETE FROM workspaces WHERE owner_user_id = 'f35-owner'"))
            finally:
                engine.dispose()
