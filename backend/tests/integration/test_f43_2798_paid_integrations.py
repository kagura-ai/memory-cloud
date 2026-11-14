"""Migration round trip and database authority constraints for paid integrations."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from alembic import command
from tests.integration.test_alembic_migrations import (
    _alembic_at_test_db,
    _get_alembic_config,
    _reset_alembic_state,
    _sync_engine,
)

PATH = Path(__file__).resolve().parents[2] / "alembic/versions/f43_2798_paid_integrations.py"


def migration():
    spec = importlib.util.spec_from_file_location("_paid_integrations_migration", PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_constraints_cascade_and_repeatable_downgrade():
    engine = _sync_engine()
    mod = migration()
    wid = uuid4()
    try:
        _reset_alembic_state()
        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), mod.down_revision)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO workspaces (id, name, owner_user_id) VALUES (:id, 'Keep workspace', 'owner')"
                ),
                {"id": wid},
            )
        for _ in range(2):
            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), mod.revision)
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO workspace_integration_states (workspace_id, revision, event_id, source, source_ref, updated_at) "
                        "VALUES (:id, 1, 'event', 'external_billing', 'snapshot', now())"
                    ),
                    {"id": wid},
                )
                conn.execute(
                    text(
                        "INSERT INTO workspace_integration_licenses (workspace_id, service_id, valid_from, valid_until, evidence_ref) "
                        "VALUES (:id, 'catalog-sync', now(), now() + interval '1 day', 'paid')"
                    ),
                    {"id": wid},
                )
                conn.execute(
                    text(
                        "INSERT INTO workspace_integration_events (workspace_id, event_id, revision, payload_digest, received_at) "
                        "VALUES (:id, 'event', 1, :digest, now())"
                    ),
                    {"id": wid, "digest": "a" * 64},
                )
            for sql in (
                "UPDATE workspace_integration_states SET revision = 0 WHERE workspace_id = :id",
                "UPDATE workspace_integration_states SET source = 'public_client' WHERE workspace_id = :id",
                "UPDATE workspace_integration_states SET max_valid_from = now() WHERE workspace_id = :id",
                "UPDATE workspace_integration_licenses SET valid_until = valid_from WHERE workspace_id = :id",
                "UPDATE workspace_integration_events SET payload_digest = 'short' WHERE workspace_id = :id",
                "INSERT INTO workspace_integration_events (workspace_id, event_id, revision, payload_digest, received_at) "
                "SELECT workspace_id, 'other', revision, payload_digest, now() FROM workspace_integration_events WHERE workspace_id = :id",
            ):
                with pytest.raises(IntegrityError), engine.begin() as conn:
                    conn.execute(text(sql), {"id": wid})
            with _alembic_at_test_db():
                command.downgrade(_get_alembic_config(), mod.down_revision)
            with engine.begin() as conn:
                assert (
                    conn.scalar(text("SELECT name FROM workspaces WHERE id = :id"), {"id": wid})
                    == "Keep workspace"
                )
                assert (
                    conn.scalar(text("SELECT to_regclass('workspace_integration_states')")) is None
                )
        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), mod.revision)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO workspace_integration_states (workspace_id, revision, event_id, source, source_ref, updated_at) "
                    "VALUES (:id, 1, 'event', 'external_billing', 'snapshot', now())"
                ),
                {"id": wid},
            )
            conn.execute(
                text(
                    "INSERT INTO workspace_integration_licenses (workspace_id, service_id, valid_from, valid_until, evidence_ref) "
                    "VALUES (:id, 'catalog-sync', now(), now() + interval '1 day', 'paid')"
                ),
                {"id": wid},
            )
            conn.execute(text("DELETE FROM workspaces WHERE id = :id"), {"id": wid})
            assert (
                conn.scalar(
                    text(
                        "SELECT count(*) FROM workspace_integration_states WHERE workspace_id = :id"
                    ),
                    {"id": wid},
                )
                == 0
            )
            assert (
                conn.scalar(
                    text(
                        "SELECT count(*) FROM workspace_integration_licenses WHERE workspace_id = :id"
                    ),
                    {"id": wid},
                )
                == 0
            )
    finally:
        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), "head")
        engine.dispose()
