"""Migration round-trip test for #818 (e26_818_memory_summary_trgm_index).

Verifies:
1. Upgrade installs the ``pg_trgm`` extension and creates the
   ``idx_memories_summary_trgm`` GIN index with the ``gin_trgm_ops``
   operator class.
2. The planner uses that index for the #580 ``summary ILIKE '%q%'`` filter
   (the exact predicate the route emits) — proving the bare-column index is
   the right shape, not a ``lower(summary)`` expression index.
3. Downgrade drops the index but intentionally leaves ``pg_trgm`` installed.
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

E26_REVISION = "e26_818_summary_trgm_idx"
PRIOR_HEAD = "e25_782_widen_edge_type"
INDEX_NAME = "idx_memories_summary_trgm"


def _leave_db_at_head() -> None:
    """Convention: integration suite expects the test DB at head after each test."""
    with _alembic_at_test_db():
        command.upgrade(_get_alembic_config(), "head")


def _index_def(conn: Connection) -> str | None:
    """Return the ``pg_indexes`` definition for the trigram index, or None."""
    return conn.execute(
        text("SELECT indexdef FROM pg_indexes WHERE indexname = :n"),
        {"n": INDEX_NAME},
    ).scalar_one_or_none()


def _pg_trgm_installed(conn: Connection) -> bool:
    return (
        conn.execute(
            text("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'")
        ).scalar_one_or_none()
        is not None
    )


def _seed_memory(conn: Connection, summary: str) -> None:
    """Insert one minimal active memory row (only NOT NULL cols without defaults)."""
    # NOT NULL columns on `memories` carry Python-side ORM defaults, not server
    # defaults, so a raw INSERT must supply them all explicitly.
    conn.execute(
        text(
            "INSERT INTO memories "
            "(id, user_id, summary, content, type, embedding_status, importance, "
            " confidence, scope, long_term, use_count, access_count, client, "
            " source, created_at) "
            "VALUES (gen_random_uuid(), 'tester-818', :summary, 'body', 'note', "
            "'success', 0.5, 1.0, 'working', false, 0, 0, 'test', "
            "'mcp_remember', now())"
        ),
        {"summary": summary},
    )


def test_e26_upgrade_creates_trgm_index_and_extension() -> None:
    """Upgrade enables pg_trgm and builds a gin_trgm_ops index the ILIKE filter uses."""
    _reset_alembic_state()
    with _alembic_at_test_db():
        command.upgrade(_get_alembic_config(), PRIOR_HEAD)

    engine = _sync_engine()
    try:
        # Before the migration: neither extension nor index exists on a clean
        # (DROP SCHEMA-reset) test DB.
        with engine.connect() as conn:
            assert _index_def(conn) is None

        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), E26_REVISION)

        with engine.connect() as conn:
            assert _pg_trgm_installed(conn)

            indexdef = _index_def(conn)
            assert indexdef is not None, f"{INDEX_NAME} not created by upgrade"
            # Bare-column trigram GIN index — not lower(summary).
            assert "USING gin" in indexdef
            assert "gin_trgm_ops" in indexdef
            assert "lower(" not in indexdef.lower()

        # Planner check: with seqscan disabled the trigram index must back the
        # exact predicate the #580 route emits (summary ILIKE '%q%'). A
        # lower(summary) expression index would NOT be matched here.
        with engine.begin() as conn:
            for s in ("authentication flow", "auth token refresh", "unrelated topic"):
                _seed_memory(conn, s)

        with engine.connect() as conn:
            conn.execute(text("SET enable_seqscan = off"))
            plan = "\n".join(
                conn.execute(text("EXPLAIN SELECT id FROM memories WHERE summary ILIKE '%auth%'"))
                .scalars()
                .all()
            )
            assert INDEX_NAME in plan, f"trigram index not used; plan was:\n{plan}"
    finally:
        engine.dispose()
        _leave_db_at_head()


def test_e26_downgrade_drops_index_keeps_extension() -> None:
    """Downgrade removes the index but leaves pg_trgm installed (documented contract)."""
    _reset_alembic_state()
    with _alembic_at_test_db():
        command.upgrade(_get_alembic_config(), E26_REVISION)
        command.downgrade(_get_alembic_config(), PRIOR_HEAD)

    engine = _sync_engine()
    try:
        with engine.connect() as conn:
            assert _index_def(conn) is None, "index should be dropped on downgrade"
            # Extension is intentionally retained — dropping it is out of scope
            # for this migration (other objects may come to depend on it).
            assert _pg_trgm_installed(conn)
    finally:
        engine.dispose()
        _leave_db_at_head()
