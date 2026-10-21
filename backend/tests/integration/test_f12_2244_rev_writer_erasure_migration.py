"""Migration round-trip for f12_2244_rev_writer_erasure (#2244).

1. upgrade widens ``valid_revision_writer`` with ``'erasure'``, appended after
   ``'rollback'`` and validated; an ``erasure`` row is accepted;
2. downgrade with no such row restores the two-value CHECK, validated;
3. downgrade with an ``erasure`` row refuses before any change (the CHECK
   stays the three-value one) and keeps the row; once the row is gone the
   downgrade goes through.

Not executed in the local unit run — needs the DB container.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text

from tests.integration.test_alembic_migrations import (
    _downgrade_to,
    _reset_alembic_state,
    _sync_engine,
    _upgrade_to,
)

F12_REVISION = "f12_2244_rev_writer_erasure"
PRIOR_HEAD = "f11_2092_embedded_text_digest"
ACTOR = "f12-migration-test"


def _writer_check(conn: Any) -> str:
    return conn.execute(
        text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'valid_revision_writer'"
        )
    ).scalar_one()


def _writer_check_validated(conn: Any) -> bool:
    return conn.execute(
        text("SELECT convalidated FROM pg_constraint WHERE conname = 'valid_revision_writer'")
    ).scalar_one()


def _seed(conn: Any, workspace_id: uuid.UUID, context_id: uuid.UUID) -> uuid.UUID:
    """One memory with an ``erasure`` event, in the f12 schema (raw SQL with
    explicit columns, so a later model column cannot break the insert)."""
    memory_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO workspaces (id, name, owner_user_id, plan_name) "
            "VALUES (:id, :name, :actor, 'free')"
        ),
        {"id": workspace_id, "name": f"f12-{workspace_id.hex[:8]}", "actor": ACTOR},
    )
    conn.execute(
        text(
            "INSERT INTO contexts (id, workspace_id, name, created_by) "
            "VALUES (:id, :ws, :name, :actor)"
        ),
        {"id": context_id, "ws": workspace_id, "name": f"f12-{context_id.hex[:8]}", "actor": ACTOR},
    )
    conn.execute(
        text(
            "INSERT INTO memories (id, user_id, workspace_id, context_id, summary, content, "
            "type, client, embedding_status, importance, confidence, scope, long_term, "
            "source, access_count, version) "
            "VALUES (:id, :actor, :ws, :ctx, 'f12 seed', 'f12 seed', 'fact', 'pytest', "
            "'pending', 0.5, 1.0, 'working', false, 'mcp_remember', 0, 2)"
        ),
        {"id": memory_id, "actor": ACTOR, "ws": workspace_id, "ctx": context_id},
    )
    conn.execute(
        text(
            "INSERT INTO memory_revisions "
            "(memory_id, context_id, version, kind, writer, actor_user_id, prior) "
            "VALUES (:m, :ctx, 2, 'updated', 'erasure', :actor, "
            '\'{"erased": ["details"]}\'::jsonb)'
        ),
        {"m": memory_id, "ctx": context_id, "actor": ACTOR},
    )
    return memory_id


def test_f12_round_trip_check() -> None:
    _reset_alembic_state()
    engine = _sync_engine()
    workspace_id, context_id = uuid.uuid4(), uuid.uuid4()
    try:
        _upgrade_to(PRIOR_HEAD)
        with engine.connect() as conn:
            check = _writer_check(conn)
            assert "'erasure'" not in check and "'rollback'" in check

        _upgrade_to(F12_REVISION)
        with engine.connect() as conn:
            check = _writer_check(conn)
            # Appended, never reordered.
            assert check.index("'api'") < check.index("'rollback'") < check.index("'erasure'")
            assert _writer_check_validated(conn) is True

        _downgrade_to(PRIOR_HEAD)
        with engine.connect() as conn:
            assert "'erasure'" not in _writer_check(conn)
            assert _writer_check_validated(conn) is True

        _upgrade_to(F12_REVISION)
        with engine.begin() as conn:
            _seed(conn, workspace_id, context_id)

        with pytest.raises(RuntimeError, match=r"1 memory_revisions row\(s\) have writer"):
            _downgrade_to(PRIOR_HEAD)
        with engine.connect() as conn:
            assert "'erasure'" in _writer_check(conn)
            assert _writer_check_validated(conn) is True
            assert (
                conn.execute(
                    text("SELECT count(*) FROM memory_revisions WHERE writer = 'erasure'")
                ).scalar_one()
                == 1
            )
            assert (
                conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                == F12_REVISION
            )

        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM memory_revisions WHERE context_id = :c"), {"c": context_id}
            )
        _downgrade_to(PRIOR_HEAD)
        with engine.connect() as conn:
            assert "'erasure'" not in _writer_check(conn)
            assert _writer_check_validated(conn) is True
    finally:
        try:
            _upgrade_to("head")
        finally:
            try:
                with engine.begin() as conn:
                    conn.execute(
                        text("DELETE FROM memory_revisions WHERE context_id = :c"),
                        {"c": context_id},
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
