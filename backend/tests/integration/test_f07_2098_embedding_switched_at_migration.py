"""Migration round-trip for f07_2098_embedding_switched_at (#2098).

1. upgrade adds the nullable ``embedding_switched_at`` (TIMESTAMP WITH TIME
   ZONE, no default); a row that existed before reads NULL (no backfill);
2. downgrade drops it again.

Not executed in the local unit run — needs the DB container.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text

from tests.integration.test_alembic_migrations import (
    _downgrade_to,
    _reset_alembic_state,
    _sync_engine,
    _upgrade_to,
)

F07_REVISION = "f07_2098_embedding_switched_at"
PRIOR_HEAD = "f06_2144_llm_selector"


def _column(conn) -> tuple[str, str, str | None] | None:
    row = conn.execute(
        text(
            "SELECT is_nullable, data_type, column_default "
            "FROM information_schema.columns "
            "WHERE table_name = 'context_search_configs' "
            "AND column_name = 'embedding_switched_at'"
        )
    ).one_or_none()
    return None if row is None else tuple(row)


def test_f07_round_trip_adds_a_nullable_column_without_backfill() -> None:
    _reset_alembic_state()
    engine = _sync_engine()
    workspace_id = uuid.uuid4()
    ctx_id = uuid.uuid4()
    try:
        _upgrade_to(PRIOR_HEAD)
        with engine.connect() as conn:
            assert _column(conn) is None
            conn.execute(
                text(
                    "INSERT INTO workspaces (id, name, owner_user_id, plan_name) "
                    "VALUES (:id, :name, 'f07-migration-test', 'free')"
                ),
                {"id": workspace_id, "name": f"f07-{workspace_id.hex[:8]}"},
            )
            conn.execute(
                text(
                    "INSERT INTO contexts (id, workspace_id, name, created_by) "
                    "VALUES (:id, :ws, :name, 'f07-migration-test')"
                ),
                {"id": ctx_id, "ws": workspace_id, "name": f"f07-{ctx_id.hex[:8]}"},
            )
            conn.execute(
                text(
                    "INSERT INTO context_search_configs "
                    "(context_id, semantic_weight, bm25_weight, fetch_factor, use_rerank, "
                    "embedding_model, embedding_dimensions) "
                    "VALUES (:id, 0.6, 0.4, 3, false, 'text-embedding-3-small', 512)"
                ),
                {"id": ctx_id},
            )
            conn.commit()

        _upgrade_to(F07_REVISION)
        with engine.connect() as conn:
            assert _column(conn) == ("YES", "timestamp with time zone", None)
            switched_at = conn.execute(
                text(
                    "SELECT embedding_switched_at FROM context_search_configs "
                    "WHERE context_id = :id"
                ),
                {"id": ctx_id},
            ).scalar_one()
            assert switched_at is None, "an existing row reads NULL, no backfill"

        _downgrade_to(PRIOR_HEAD)
        with engine.connect() as conn:
            assert _column(conn) is None
    finally:
        try:
            _upgrade_to("head")
            with engine.connect() as conn:
                conn.execute(
                    text("DELETE FROM context_search_configs WHERE context_id = :id"),
                    {"id": ctx_id},
                )
                conn.execute(
                    text("DELETE FROM contexts WHERE workspace_id = :ws"), {"ws": workspace_id}
                )
                conn.execute(text("DELETE FROM workspaces WHERE id = :ws"), {"ws": workspace_id})
                conn.commit()
        finally:
            engine.dispose()
