"""Migration round-trip for f08_2107_chunk_gap (#2107).

1. upgrade adds the nullable ``content_chunks_gap_at`` (timestamptz) and
   ``content_chunks_gap_cursor`` (uuid) and the partial index on the stamp;
   no row is stamped (no data step: the scheduler leader stamps once at
   startup where the deployment gates content chunks) and ``updated_at`` is
   left alone;
2. downgrade drops the index and both columns again.

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

F08_REVISION = "f08_2107_chunk_gap"
PRIOR_HEAD = "f07_2098_embedding_switched_at"
INDEX = "ix_context_search_configs_chunk_gap"


def _columns(conn) -> dict[str, tuple[str, str]]:
    rows = conn.execute(
        text(
            "SELECT column_name, is_nullable, data_type "
            "FROM information_schema.columns "
            "WHERE table_name = 'context_search_configs' "
            "AND column_name IN ('content_chunks_gap_at', 'content_chunks_gap_cursor')"
        )
    ).all()
    return {name: (nullable, data_type) for name, nullable, data_type in rows}


def _index(conn) -> str | None:
    return conn.execute(
        text("SELECT indexdef FROM pg_indexes WHERE indexname = :name"), {"name": INDEX}
    ).scalar_one_or_none()


def test_f08_round_trip_adds_the_columns_without_a_data_step() -> None:
    _reset_alembic_state()
    engine = _sync_engine()
    workspace_id = uuid.uuid4()
    on_id, off_id = uuid.uuid4(), uuid.uuid4()
    try:
        _upgrade_to(PRIOR_HEAD)
        with engine.connect() as conn:
            assert _columns(conn) == {}
            conn.execute(
                text(
                    "INSERT INTO workspaces (id, name, owner_user_id, plan_name) "
                    "VALUES (:id, :name, 'f08-migration-test', 'free')"
                ),
                {"id": workspace_id, "name": f"f08-{workspace_id.hex[:8]}"},
            )
            for ctx_id, on in ((on_id, True), (off_id, False)):
                conn.execute(
                    text(
                        "INSERT INTO contexts (id, workspace_id, name, created_by) "
                        "VALUES (:id, :ws, :name, 'f08-migration-test')"
                    ),
                    {"id": ctx_id, "ws": workspace_id, "name": f"f08-{ctx_id.hex[:8]}"},
                )
                conn.execute(
                    text(
                        "INSERT INTO context_search_configs "
                        "(context_id, semantic_weight, bm25_weight, fetch_factor, use_rerank, "
                        "embedding_model, embedding_dimensions, content_chunks_enabled, "
                        "updated_at) "
                        "VALUES (:id, 0.6, 0.4, 3, false, 'text-embedding-3-small', 512, :on, "
                        "'2026-01-02 03:04:05+00')"
                    ),
                    {"id": ctx_id, "on": on},
                )
            conn.commit()

        _upgrade_to(F08_REVISION)
        with engine.connect() as conn:
            assert _columns(conn) == {
                "content_chunks_gap_at": ("YES", "timestamp with time zone"),
                "content_chunks_gap_cursor": ("YES", "uuid"),
            }
            indexdef = _index(conn)
            assert indexdef is not None
            assert "WHERE (content_chunks_gap_at IS NOT NULL)" in indexdef
            rows = dict(
                conn.execute(
                    text(
                        "SELECT context_id, content_chunks_gap_at IS NOT NULL "
                        "FROM context_search_configs WHERE context_id IN (:on, :off)"
                    ),
                    {"on": on_id, "off": off_id},
                ).all()
            )
            assert rows == {on_id: False, off_id: False}, "no data step"
            unchanged = conn.execute(
                text(
                    "SELECT count(*) FROM context_search_configs "
                    "WHERE context_id IN (:on, :off) "
                    "AND updated_at = '2026-01-02 03:04:05+00'"
                ),
                {"on": on_id, "off": off_id},
            ).scalar_one()
            assert unchanged == 2, "updated_at is left alone"

        _downgrade_to(PRIOR_HEAD)
        with engine.connect() as conn:
            assert _columns(conn) == {}
            assert _index(conn) is None
    finally:
        try:
            _upgrade_to("head")
            with engine.connect() as conn:
                conn.execute(
                    text("DELETE FROM context_search_configs WHERE context_id IN (:a, :b)"),
                    {"a": on_id, "b": off_id},
                )
                conn.execute(
                    text("DELETE FROM contexts WHERE workspace_id = :ws"), {"ws": workspace_id}
                )
                conn.execute(text("DELETE FROM workspaces WHERE id = :ws"), {"ws": workspace_id})
                conn.commit()
        finally:
            engine.dispose()
