"""Migration round-trip test for #619 (e29_619_memories_ws_ctx_idx).

Verifies:
1. Upgrade creates the ``idx_memories_ws_ctx`` compound partial B-tree index.
2. The planner uses that index for the canonical three-column scope predicate
   (``workspace_id = … AND context_id = … AND deleted_at IS NULL``) that
   ``aggregate_tags``, ``get_context_stats``, and ``_refresh_hub_tag_cache``
   all share.
3. Downgrade drops the index; the existing single-column indexes are unaffected.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection

from alembic import command
from tests.integration.test_alembic_migrations import (
    _alembic_at_test_db,
    _get_alembic_config,
    _reset_alembic_state,
    _sync_engine,
)

E29_REVISION = "e29_619_memories_ws_ctx_idx"
PRIOR_HEAD = "e29_658_drop_graph_cache_cols"
INDEX_NAME = "idx_memories_ws_ctx"

_WS_ID = "a0619000-0000-0000-0000-000000000001"
_CTX_ID = "b0619000-0000-0000-0000-000000000002"


def _seed_memories(conn: Connection, n: int = 5) -> None:
    """Insert n minimal active memory rows (no FK-constrained columns).

    Mirrors the e26 seed pattern: workspace_id and context_id are left NULL
    so no FK prerequisites are needed, but the rows give the planner enough
    statistics to prefer the compound index over a bitmap merge when
    enable_seqscan is off.
    """
    for _ in range(n):
        conn.execute(
            text(
                "INSERT INTO memories "
                "(id, user_id, summary, content, type, embedding_status, "
                " importance, confidence, scope, long_term, use_count, "
                " access_count, client, source, created_at) "
                "VALUES (gen_random_uuid(), 'tester-619', 'scope scan seed', "
                "'body', 'note', 'success', 0.5, 1.0, 'working', false, "
                "0, 0, 'test', 'mcp_remember', now())"
            )
        )
    conn.execute(text("ANALYZE memories"))


def _leave_db_at_head() -> None:
    """Convention: integration suite expects the test DB at head after each test."""
    with _alembic_at_test_db():
        command.upgrade(_get_alembic_config(), "head")


def _index_def(conn: Connection) -> str | None:
    """Return the ``pg_indexes`` definition for the compound index, or None."""
    return conn.execute(
        text("SELECT indexdef FROM pg_indexes WHERE indexname = :n"),
        {"n": INDEX_NAME},
    ).scalar_one_or_none()


def test_e29_upgrade_creates_compound_partial_index() -> None:
    """Upgrade builds idx_memories_ws_ctx and the planner uses it for the scope predicate."""
    _reset_alembic_state()
    with _alembic_at_test_db():
        command.upgrade(_get_alembic_config(), PRIOR_HEAD)

    engine = _sync_engine()
    try:
        with engine.connect() as conn:
            assert _index_def(conn) is None, f"{INDEX_NAME} should not exist before upgrade"

        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), E29_REVISION)

        with engine.connect() as conn:
            indexdef = _index_def(conn)
            assert indexdef is not None, f"{INDEX_NAME} not created by upgrade"
            assert "workspace_id" in indexdef
            assert "context_id" in indexdef
            assert "deleted_at IS NULL" in indexdef

        # Seed rows and ANALYZE so the planner has statistics to work with.
        # Without data the planner may bitmap-merge the two single-column
        # indexes instead of using the compound one (Copilot loop 1 lesson).
        with engine.begin() as conn:
            _seed_memories(conn)

        # Planner check: with seqscan disabled the compound partial index must
        # back the exact three-column predicate the callers emit.
        with engine.connect() as conn:
            conn.execute(text("SET enable_seqscan = off"))
            plan = "\n".join(
                conn.execute(
                    text(
                        "EXPLAIN SELECT id FROM memories "
                        "WHERE workspace_id = CAST(:ws AS uuid) "
                        "AND context_id = CAST(:ctx AS uuid) "
                        "AND deleted_at IS NULL"
                    ),
                    {"ws": _WS_ID, "ctx": _CTX_ID},
                )
                .scalars()
                .all()
            )
            assert INDEX_NAME in plan, f"compound index not used; plan was:\n{plan}"
    finally:
        engine.dispose()
        _leave_db_at_head()


def test_e29_downgrade_drops_index() -> None:
    """Downgrade removes idx_memories_ws_ctx; single-column indexes are unaffected."""
    _reset_alembic_state()
    with _alembic_at_test_db():
        command.upgrade(_get_alembic_config(), E29_REVISION)
        command.downgrade(_get_alembic_config(), PRIOR_HEAD)

    engine = _sync_engine()
    try:
        with engine.connect() as conn:
            assert _index_def(conn) is None, "index should be dropped on downgrade"
            # Single-column indexes created by the baseline must survive the downgrade.
            surviving = (
                conn.execute(
                    text(
                        "SELECT indexname FROM pg_indexes "
                        "WHERE tablename = 'memories' "
                        "AND indexname IN ('ix_memories_workspace_id', 'ix_memories_context_id')"
                    )
                )
                .scalars()
                .all()
            )
            assert set(surviving) == {
                "ix_memories_workspace_id",
                "ix_memories_context_id",
            }, f"single-column baseline indexes missing after downgrade: {surviving}"
    finally:
        engine.dispose()
        _leave_db_at_head()
