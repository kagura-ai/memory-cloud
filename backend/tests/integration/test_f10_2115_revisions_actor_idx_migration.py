"""Migration round-trip for f10_2115_revisions_actor_idx (#2115).

1. upgrade builds ``idx_memory_revisions_actor`` — a plain btree on
   ``memory_revisions (actor_user_id)``, no WHERE (the column is NOT NULL) —
   CONCURRENTLY, over existing revision rows, and leaves it VALID;
2. a second upgrade run over an INVALID leftover of the same name (what an
   interrupted concurrent build leaves behind) drops and rebuilds it;
3. downgrade drops it again.

Not executed in the local unit run — needs the DB container.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tests.integration.test_alembic_migrations import (
    _downgrade_to,
    _reset_alembic_state,
    _sync_engine,
    _upgrade_to,
)

F10_REVISION = "f10_2115_revisions_actor_idx"
PRIOR_HEAD = "f09_2129_cred_default_ctx"
INDEX = "idx_memory_revisions_actor"
EXPECTED_DEF = (
    "CREATE INDEX idx_memory_revisions_actor ON public.memory_revisions USING btree (actor_user_id)"
)
ACTOR = "f10-migration-test"


def _indexdef(conn: Any) -> str | None:
    return conn.execute(
        text("SELECT indexdef FROM pg_indexes WHERE indexname = :name"), {"name": INDEX}
    ).scalar_one_or_none()


def _index_valid(conn: Any) -> Any:
    return conn.execute(
        text(
            "SELECT i.indisvalid FROM pg_class c JOIN pg_index i "
            "ON c.oid = i.indexrelid WHERE c.relname = :n"
        ),
        {"n": INDEX},
    ).scalar()


def _seed(conn: Any, workspace_id: uuid.UUID, context_id: uuid.UUID) -> uuid.UUID:
    """One memory with two events by the same actor, in the f09 schema.

    Raw SQL with explicit columns, so a later model column cannot break the
    insert at the prior head.
    """
    memory_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO workspaces (id, name, owner_user_id, plan_name) "
            "VALUES (:id, :name, :actor, 'free')"
        ),
        {"id": workspace_id, "name": f"f10-{workspace_id.hex[:8]}", "actor": ACTOR},
    )
    conn.execute(
        text(
            "INSERT INTO contexts (id, workspace_id, name, created_by) "
            "VALUES (:id, :ws, :name, :actor)"
        ),
        {"id": context_id, "ws": workspace_id, "name": f"f10-{context_id.hex[:8]}", "actor": ACTOR},
    )
    conn.execute(
        text(
            "INSERT INTO memories (id, user_id, workspace_id, context_id, summary, content, "
            "type, client, embedding_status, importance, confidence, scope, long_term, "
            "source, access_count) "
            "VALUES (:id, :actor, :ws, :ctx, 'f10 seed', 'f10 seed', 'fact', 'pytest', "
            "'pending', 0.5, 1.0, 'working', false, 'mcp_remember', 0)"
        ),
        {"id": memory_id, "actor": ACTOR, "ws": workspace_id, "ctx": context_id},
    )
    conn.execute(
        text(
            "INSERT INTO memory_revisions "
            "(memory_id, context_id, version, kind, writer, actor_user_id) "
            "VALUES (:m, :ctx, 1, 'created', 'api', :actor), "
            "(:m, :ctx, 2, 'updated', 'api', :actor)"
        ),
        {"m": memory_id, "ctx": context_id, "actor": ACTOR},
    )
    return memory_id


def _leave_an_invalid_index(engine: Any) -> None:
    """A failed ``CREATE ... CONCURRENTLY`` leaves the index behind INVALID.

    A UNIQUE build over the two same-actor events fails on the duplicate the
    way an interrupted build does — no catalog write, no superuser needed.
    """
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        with pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX CONCURRENTLY idx_memory_revisions_actor "
                    "ON memory_revisions (actor_user_id)"
                )
            )


def test_f10_round_trip_builds_the_actor_index() -> None:
    _reset_alembic_state()
    engine = _sync_engine()
    workspace_id, context_id = uuid.uuid4(), uuid.uuid4()
    try:
        _upgrade_to(PRIOR_HEAD)
        with engine.begin() as conn:
            assert _indexdef(conn) is None
            # Seeded rows, so the concurrent build runs over data.
            _seed(conn, workspace_id, context_id)

        _upgrade_to(F10_REVISION)
        with engine.connect() as conn:
            assert _indexdef(conn) == EXPECTED_DEF
            assert _index_valid(conn) is True

        # Re-running the upgrade after an interrupted build: the INVALID
        # leftover (here a UNIQUE one) is dropped and the plain index rebuilt.
        _downgrade_to(PRIOR_HEAD)
        _leave_an_invalid_index(engine)
        with engine.connect() as conn:
            assert _index_valid(conn) is False
        _upgrade_to(F10_REVISION)
        with engine.connect() as conn:
            assert _indexdef(conn) == EXPECTED_DEF
            assert _index_valid(conn) is True

        _downgrade_to(PRIOR_HEAD)
        with engine.connect() as conn:
            assert _indexdef(conn) is None
    finally:
        try:
            _upgrade_to("head")
        finally:
            try:
                with engine.begin() as conn:
                    conn.execute(
                        text(
                            "DELETE FROM memory_revisions WHERE context_id = :c "
                            "AND actor_user_id = :actor"
                        ),
                        {"c": context_id, "actor": ACTOR},
                    )
                    conn.execute(
                        text("DELETE FROM memories WHERE context_id = :c"), {"c": context_id}
                    )
                    conn.execute(text("DELETE FROM contexts WHERE id = :c"), {"c": context_id})
                    conn.execute(
                        text("DELETE FROM workspaces WHERE id = :ws"), {"ws": workspace_id}
                    )
            finally:
                engine.dispose()
