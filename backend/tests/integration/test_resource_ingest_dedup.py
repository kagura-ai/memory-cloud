"""Integration tests pinning resource_events deduplication behavior.

Issue #318: validates that the constraint names declared in
``db.constraint_names`` actually match what PostgreSQL raises on a
violation. A drift between the two is exactly the bug that motivated
this fix — substring matching on ``str(IntegrityError)`` silently
disabled the 409 path when the migration renamed the index. These tests
fail loudly if the constants ever drift again.

Also pins the partial-UNIQUE semantics:

    - ``op='upsert'`` duplicate (resource_pk, doc_id, version) → conflict.
    - ``op='delete'`` duplicate → no conflict (predicate excludes deletes
      so the upsert → delete → upsert revival sequence stays valid).
    - ``idempotency_key`` collision → conflict on the auto-named UNIQUE.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from db.constraint_names import (
    RESOURCE_EVENTS_IDEMPOTENCY_UNIQUE,
    RESOURCE_EVENTS_UPSERT_UNIQUE,
    integrity_error_constraint_name,
)

from .test_alembic_migrations import _get_alembic_config, _reset_alembic_state
from .test_resources_foundation_migration import _point_alembic_at_test_db, _sync_engine


@pytest.fixture(scope="module")
def fresh_db_at_head():
    """Reset the test DB to head once per module and reuse across tests.

    Each test seeds a unique ``resource_id`` so cross-test isolation does
    not require a per-test reset; the migration round-trip is the
    expensive step we want to amortize.
    """
    from alembic import command

    with _point_alembic_at_test_db():
        _reset_alembic_state()
        config = _get_alembic_config()
        command.upgrade(config, "head")
        engine = _sync_engine()
        try:
            yield engine
        finally:
            engine.dispose()


def _seed_minimal_resource(conn: sa.Connection, resource_id: str) -> uuid.UUID:
    """Insert workspace + public context + resources row; return resource_pk."""
    workspace_id = uuid.uuid4()
    context_id = uuid.uuid4()
    resource_pk = uuid.uuid4()
    owner_id = "user-test-318"

    conn.execute(
        text(
            "INSERT INTO workspaces (id, name, owner_user_id, plan_name) "
            "VALUES (:id, :name, :owner, 'free')"
        ),
        {"id": workspace_id, "name": f"ws-{workspace_id.hex[:8]}", "owner": owner_id},
    )
    conn.execute(
        text(
            "INSERT INTO resources (id, workspace_id, resource_id, created_by) "
            "VALUES (:pk, :ws, :rid, :owner)"
        ),
        {"pk": resource_pk, "ws": workspace_id, "rid": resource_id, "owner": owner_id},
    )
    conn.execute(
        text(
            "INSERT INTO contexts (id, workspace_id, name, created_by, "
            "is_private, is_public, resource_id) "
            "VALUES (:id, :ws, :name, :owner, false, true, :rid)"
        ),
        {
            "id": context_id,
            "ws": workspace_id,
            "name": f"ctx-{resource_id}",
            "owner": owner_id,
            "rid": resource_id,
        },
    )
    return resource_pk


class TestResourceEventDedupConstraintNames:
    """Pin that PostgreSQL raises the constraint names we match on."""

    def test_upsert_duplicate_violates_named_partial_unique(self, fresh_db_at_head):
        engine = fresh_db_at_head
        with engine.begin() as conn:
            pk = _seed_minimal_resource(conn, "test-318-upsert-dup")
            conn.execute(
                text(
                    "INSERT INTO resource_events "
                    "(resource_id, resource_pk, op, doc_id, version, importance) "
                    "VALUES ('test-318-upsert-dup', :pk, 'upsert', 'doc1', 1, 0.5)"
                ),
                {"pk": pk},
            )

        with pytest.raises(IntegrityError) as exc_info:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO resource_events "
                        "(resource_id, resource_pk, op, doc_id, version, importance) "
                        "VALUES ('test-318-upsert-dup', :pk, 'upsert', 'doc1', 1, 0.5)"
                    ),
                    {"pk": pk},
                )

        assert integrity_error_constraint_name(exc_info.value) == RESOURCE_EVENTS_UPSERT_UNIQUE

    def test_delete_duplicate_does_not_violate(self, fresh_db_at_head):
        """op='delete' is outside the partial UNIQUE predicate."""
        engine = fresh_db_at_head
        with engine.begin() as conn:
            pk = _seed_minimal_resource(conn, "test-318-delete-dup")
            conn.execute(
                text(
                    "INSERT INTO resource_events "
                    "(resource_id, resource_pk, op, doc_id, version, importance) "
                    "VALUES ('test-318-delete-dup', :pk, 'delete', 'doc1', 1, 0.5), "
                    "('test-318-delete-dup', :pk, 'delete', 'doc1', 1, 0.5)"
                ),
                {"pk": pk},
            )

        with engine.connect() as conn:
            count = conn.execute(
                text(
                    "SELECT COUNT(*) FROM resource_events "
                    "WHERE resource_id = 'test-318-delete-dup' AND op = 'delete'"
                )
            ).scalar_one()
            assert count == 2

    def test_upsert_delete_upsert_revival_is_allowed(self, fresh_db_at_head):
        """The whole reason ``WHERE op='upsert'`` is in the predicate."""
        engine = fresh_db_at_head
        with engine.begin() as conn:
            pk = _seed_minimal_resource(conn, "test-318-revival")
            conn.execute(
                text(
                    "INSERT INTO resource_events "
                    "(resource_id, resource_pk, op, doc_id, version, importance) "
                    "VALUES "
                    "('test-318-revival', :pk, 'upsert', 'doc1', 1, 0.5), "
                    "('test-318-revival', :pk, 'delete', 'doc1', 1, 0.5), "
                    "('test-318-revival', :pk, 'upsert', 'doc1', 2, 0.5)"
                ),
                {"pk": pk},
            )

    def test_idempotency_key_duplicate_violates_baseline_unique(self, fresh_db_at_head):
        """The auto-named UNIQUE on idempotency_key (PG default `<table>_<col>_key`)."""
        engine = fresh_db_at_head
        with engine.begin() as conn:
            pk = _seed_minimal_resource(conn, "test-318-idem")
            conn.execute(
                text(
                    "INSERT INTO resource_events "
                    "(resource_id, resource_pk, op, doc_id, version, importance, "
                    "idempotency_key) VALUES "
                    "('test-318-idem', :pk, 'upsert', 'doc1', 1, 0.5, 'key-abc')"
                ),
                {"pk": pk},
            )

        with pytest.raises(IntegrityError) as exc_info:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO resource_events "
                        "(resource_id, resource_pk, op, doc_id, version, importance, "
                        "idempotency_key) VALUES "
                        "('test-318-idem', :pk, 'upsert', 'doc2', 1, 0.5, 'key-abc')"
                    ),
                    {"pk": pk},
                )

        assert integrity_error_constraint_name(exc_info.value) == RESOURCE_EVENTS_IDEMPOTENCY_UNIQUE
