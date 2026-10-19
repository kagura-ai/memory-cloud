"""Migration round-trip for f06_2144_llm_selector (#2144).

1. upgrade adds ``llm_selector_enabled`` (NOT NULL, default false) and the
   nullable ``llm_selector_enabled_at``; a row that existed before reads
   false / NULL (no backfill);
2. downgrade drops both columns again.

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

F06_REVISION = "f06_2144_llm_selector"
PRIOR_HEAD = "f05_2121_rollback_window"
COLUMNS = ("llm_selector_enabled", "llm_selector_enabled_at")


def _columns(conn) -> dict[str, tuple[str, str, str | None]]:
    rows = conn.execute(
        text(
            "SELECT column_name, is_nullable, data_type, column_default "
            "FROM information_schema.columns "
            "WHERE table_name = 'context_search_configs' "
            "AND column_name IN ('llm_selector_enabled', 'llm_selector_enabled_at')"
        )
    ).all()
    return {name: (nullable, data_type, default) for name, nullable, data_type, default in rows}


def test_f06_round_trip_defaults_off_without_backfill() -> None:
    _reset_alembic_state()
    engine = _sync_engine()
    workspace_id = uuid.uuid4()
    ctx_id = uuid.uuid4()
    try:
        _upgrade_to(PRIOR_HEAD)
        with engine.connect() as conn:
            assert _columns(conn) == {}
            conn.execute(
                text(
                    "INSERT INTO workspaces (id, name, owner_user_id, plan_name) "
                    "VALUES (:id, :name, 'f06-migration-test', 'free')"
                ),
                {"id": workspace_id, "name": f"f06-{workspace_id.hex[:8]}"},
            )
            conn.execute(
                text(
                    "INSERT INTO contexts (id, workspace_id, name, created_by) "
                    "VALUES (:id, :ws, :name, 'f06-migration-test')"
                ),
                {"id": ctx_id, "ws": workspace_id, "name": f"f06-{ctx_id.hex[:8]}"},
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

        _upgrade_to(F06_REVISION)
        with engine.connect() as conn:
            columns = _columns(conn)
            assert set(columns) == set(COLUMNS)
            nullable, data_type, default = columns["llm_selector_enabled"]
            assert (nullable, data_type) == ("NO", "boolean")
            assert default is not None and "false" in default
            nullable, data_type, default = columns["llm_selector_enabled_at"]
            assert (nullable, data_type, default) == (
                "YES",
                "timestamp with time zone",
                None,
            )
            row = conn.execute(
                text(
                    "SELECT llm_selector_enabled, llm_selector_enabled_at "
                    "FROM context_search_configs WHERE context_id = :id"
                ),
                {"id": ctx_id},
            ).one()
            assert tuple(row) == (False, None), "an existing row reads off, no backfill"

        _downgrade_to(PRIOR_HEAD)
        with engine.connect() as conn:
            assert _columns(conn) == {}
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
