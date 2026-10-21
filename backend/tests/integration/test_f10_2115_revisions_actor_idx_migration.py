"""Migration round-trip for f10_2115_revisions_actor_idx (#2115).

1. upgrade builds ``idx_memory_revisions_actor`` — a plain btree on
   ``memory_revisions (actor_user_id)``, no WHERE (the column is NOT NULL) —
   CONCURRENTLY, over existing revision rows, and leaves it VALID;
2. a second upgrade run over an INVALID leftover (an interrupted concurrent
   build) drops and rebuilds it;
3. downgrade drops it again.

Not executed in the local unit run — needs the DB container.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import text

from models.auth import Context, Workspace
from models.memory import Memory, MemoryRevision
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


def test_f10_round_trip_builds_the_actor_index() -> None:
    _reset_alembic_state()
    engine = _sync_engine()
    workspace_id, context_id, memory_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    try:
        _upgrade_to(PRIOR_HEAD)
        with engine.connect() as conn:
            assert _indexdef(conn) is None
        with engine.begin() as conn:
            # One revision row, so the concurrent build runs over data. Through
            # the model tables, so their Python-side defaults apply.
            conn.execute(
                Workspace.__table__.insert().values(
                    id=workspace_id, name=f"f10-{workspace_id.hex[:8]}", owner_user_id=ACTOR
                )
            )
            conn.execute(
                Context.__table__.insert().values(
                    id=context_id, workspace_id=workspace_id, name="f10", created_by=ACTOR
                )
            )
            conn.execute(
                Memory.__table__.insert().values(
                    id=memory_id,
                    context_id=context_id,
                    workspace_id=workspace_id,
                    user_id=ACTOR,
                    summary="f10 seed",
                    content="f10 seed",
                    type="fact",
                    client="f10-migration-test",
                )
            )
            conn.execute(
                MemoryRevision.__table__.insert().values(
                    memory_id=memory_id,
                    context_id=context_id,
                    version=1,
                    kind="created",
                    writer="api",
                    actor_user_id=ACTOR,
                )
            )

        _upgrade_to(F10_REVISION)
        with engine.connect() as conn:
            assert _indexdef(conn) == EXPECTED_DEF
            assert _index_valid(conn) is True

        # An interrupted CONCURRENTLY build leaves an INVALID index behind;
        # re-running the upgrade drops it and builds it again.
        _downgrade_to(PRIOR_HEAD)
        with engine.connect() as conn:
            conn.execute(text(f"CREATE INDEX {INDEX} ON memory_revisions (actor_user_id)"))
            conn.execute(
                text(
                    "UPDATE pg_index SET indisvalid = false WHERE indexrelid = CAST(:n AS regclass)"
                ),
                {"n": INDEX},
            )
            conn.commit()
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
            with engine.connect() as conn:
                conn.execute(
                    text("DELETE FROM memory_revisions WHERE memory_id = :m"), {"m": memory_id}
                )
                conn.execute(text("DELETE FROM memories WHERE id = :m"), {"m": memory_id})
                conn.execute(text("DELETE FROM contexts WHERE id = :c"), {"c": context_id})
                conn.execute(text("DELETE FROM workspaces WHERE id = :ws"), {"ws": workspace_id})
                conn.commit()
        finally:
            engine.dispose()
