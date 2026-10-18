"""Migration round-trip for f02_2103_chunks_enabled_at (#2103).

1. upgrade adds the nullable ``content_chunks_enabled_at`` column and fills it
   from ``updated_at`` for rows already on; rows off stay NULL;
2. downgrade drops the column again.

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

F02_REVISION = "f02_2103_chunks_enabled_at"
PRIOR_HEAD = "f01_2015_content_chunks"


def _has_column(conn) -> bool:
    return (
        conn.execute(
            text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'context_search_configs' "
                "AND column_name = 'content_chunks_enabled_at'"
            )
        ).scalar_one()
        == 1
    )


def test_f02_round_trip_and_backfill() -> None:
    _reset_alembic_state()
    engine = _sync_engine()
    workspace_id = uuid.uuid4()
    on_ctx, off_ctx = uuid.uuid4(), uuid.uuid4()
    try:
        _upgrade_to(PRIOR_HEAD)
        with engine.connect() as conn:
            assert not _has_column(conn)
            conn.execute(
                text(
                    "INSERT INTO workspaces (id, name, owner_user_id, plan_name) "
                    "VALUES (:id, :name, 'f02-migration-test', 'free')"
                ),
                {"id": workspace_id, "name": f"f02-{workspace_id.hex[:8]}"},
            )
            for ctx_id, flag in ((on_ctx, True), (off_ctx, False)):
                conn.execute(
                    text(
                        "INSERT INTO contexts (id, workspace_id, name, created_by) "
                        "VALUES (:id, :ws, :name, 'f02-migration-test')"
                    ),
                    {"id": ctx_id, "ws": workspace_id, "name": f"f02-{ctx_id.hex[:8]}"},
                )
                conn.execute(
                    text(
                        "INSERT INTO context_search_configs "
                        "(context_id, semantic_weight, bm25_weight, fetch_factor, use_rerank, "
                        "embedding_model, embedding_dimensions, content_chunks_enabled, "
                        "updated_at) "
                        "VALUES (:id, 0.6, 0.4, 3, false, 'text-embedding-3-small', 512, "
                        ":flag, '2026-01-02T03:04:05+00:00')"
                    ),
                    {"id": ctx_id, "flag": flag},
                )
            conn.commit()

        _upgrade_to(F02_REVISION)
        with engine.connect() as conn:
            assert _has_column(conn)
            rows = dict(
                conn.execute(
                    text(
                        "SELECT context_id, content_chunks_enabled_at = updated_at "
                        "FROM context_search_configs WHERE context_id IN (:on, :off)"
                    ),
                    {"on": on_ctx, "off": off_ctx},
                ).all()
            )
            assert rows[on_ctx] is True, "a row already on is stamped with its updated_at"
            assert rows[off_ctx] is None, "a row off stays NULL"

        _downgrade_to(PRIOR_HEAD)
        with engine.connect() as conn:
            assert not _has_column(conn)
    finally:
        try:
            _upgrade_to("head")
            with engine.connect() as conn:
                conn.execute(
                    text("DELETE FROM context_search_configs WHERE context_id IN (:a, :b)"),
                    {"a": on_ctx, "b": off_ctx},
                )
                conn.execute(
                    text("DELETE FROM contexts WHERE workspace_id = :ws"), {"ws": workspace_id}
                )
                conn.execute(text("DELETE FROM workspaces WHERE id = :ws"), {"ws": workspace_id})
                conn.commit()
        finally:
            engine.dispose()
