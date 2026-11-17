"""Append-only upgrade, data preservation and irreversible billing-mode fence."""

from uuid import uuid4

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from tests.integration.test_alembic_migrations import (
    _downgrade_to,
    _reset_alembic_state,
    _sync_engine,
    _upgrade_to,
)


def test_ordered_mode_upgrade_preserves_old_state_and_round_trips():
    engine = _sync_engine()
    wid = uuid4()
    try:
        _reset_alembic_state()
        _upgrade_to("f53_2810_sync_activity")
        with engine.begin() as db:
            db.execute(
                text(
                    "INSERT INTO workspaces (id,name,owner_user_id) VALUES (:id,'Retain','owner')"
                ),
                {"id": wid},
            )
            db.execute(
                text(
                    "INSERT INTO workspace_integration_states "
                    "(workspace_id,revision,event_id,source,source_ref,updated_at) "
                    "VALUES (:id,7,'retained-event','external_billing','retained-source',now())"
                ),
                {"id": wid},
            )
        for _ in range(2):
            _upgrade_to("f54_2882_ordered_billing")
            with engine.begin() as db:
                row = db.execute(
                    text(
                        "SELECT revision,event_id,source_ref,ordered_billing FROM "
                        "workspace_integration_states WHERE workspace_id=:id"
                    ),
                    {"id": wid},
                ).one()
                assert tuple(row) == (7, "retained-event", "retained-source", False)
                db.execute(
                    text(
                        "UPDATE workspace_integration_states SET ordered_billing=true WHERE workspace_id=:id"
                    ),
                    {"id": wid},
                )
            for update in (
                "SET ordered_billing=false",
                "SET workspace_id='22222222-2222-4222-8222-222222222222'",
            ):
                with pytest.raises(IntegrityError) as rejected, engine.begin() as db:
                    db.execute(
                        text(
                            "UPDATE workspace_integration_states "
                            + update
                            + " WHERE workspace_id=:id"
                        ),
                        {"id": wid},
                    )
                assert (
                    getattr(rejected.value.orig, "sqlstate", None)
                    or getattr(rejected.value.orig, "pgcode", None)
                ) == "23514"
            _downgrade_to("f53_2810_sync_activity")
            assert "ordered_billing" not in {
                column["name"]
                for column in inspect(engine).get_columns("workspace_integration_states")
            }
            with engine.connect() as db:
                assert (
                    db.scalar(text("SELECT name FROM workspaces WHERE id=:id"), {"id": wid})
                    == "Retain"
                )
                assert (
                    db.scalar(
                        text(
                            "SELECT revision FROM workspace_integration_states WHERE workspace_id=:id"
                        ),
                        {"id": wid},
                    )
                    == 7
                )
    finally:
        try:
            _reset_alembic_state()
            _upgrade_to("head")
        finally:
            engine.dispose()
