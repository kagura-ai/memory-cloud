"""Migration round-trip test for #1869 (e94_1869_summary_emb_idx).

Verifies (mirrors the e29_619 / e62_1245 pattern):
1. Upgrade builds ``idx_memories_summary_embedding_live``, a partial B-tree on
   ``memories (summary_embedding_id)`` for live rows that have a point.
2. The planner can use it for the shared-point check
   ``MemoryService._delete_memory_point`` runs once per forgotten row.
3. Downgrade drops it; the other ``memories`` indexes survive.

Complements the ORM-side declaration in ``Memory.__table_args__`` — without
this, a rename or typo in the migration would ship alembic-provisioned
databases without the index while the create_all-based suite stayed green.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection

from alembic import command
from models.memory import Memory
from tests.integration.test_alembic_migrations import (
    _alembic_at_test_db,
    _get_alembic_config,
    _reset_alembic_state,
    _sync_engine,
)

E94_REVISION = "e94_1869_summary_emb_idx"
PRIOR_HEAD = "e93_1852_embedding_attempted_at"
INDEX_NAME = "idx_memories_summary_embedding_live"

_POINT_ID = "c1869000-0000-0000-0000-000000000001"


def _seed_memories(conn: Connection, n: int = 5) -> None:
    """Insert n minimal live rows that each name a point (no FK columns)."""
    for _ in range(n):
        conn.execute(
            text(
                "INSERT INTO memories "
                "(id, user_id, summary, summary_embedding_id, content, type, "
                " embedding_status, importance, confidence, scope, long_term, "
                " access_count, client, source, created_at) "
                "VALUES (gen_random_uuid(), 'tester-1869', 'shared point seed', "
                "gen_random_uuid(), 'body', 'note', 'success', 0.5, 1.0, "
                "'working', false, 0, 'test', 'mcp_remember', now())"
            )
        )
    conn.execute(text("ANALYZE memories"))


def _leave_db_at_head() -> None:
    """Convention: integration suite expects the test DB at head after each test."""
    with _alembic_at_test_db():
        command.upgrade(_get_alembic_config(), "head")


def _index_def(conn: Connection) -> str | None:
    """Return the ``pg_indexes`` definition for the partial index, or None."""
    return conn.execute(
        text("SELECT indexdef FROM pg_indexes WHERE indexname = :n"),
        {"n": INDEX_NAME},
    ).scalar_one_or_none()


def test_e94_upgrade_creates_partial_index_the_shared_point_check_uses() -> None:
    """Upgrade builds the index and the planner uses it for the EXISTS check."""
    _reset_alembic_state()
    with _alembic_at_test_db():
        command.upgrade(_get_alembic_config(), PRIOR_HEAD)

    engine = _sync_engine()
    try:
        with engine.connect() as conn:
            assert _index_def(conn) is None, f"{INDEX_NAME} should not exist before upgrade"

        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), E94_REVISION)

        with engine.connect() as conn:
            indexdef = _index_def(conn)
            assert indexdef is not None, f"{INDEX_NAME} not created by upgrade"
            assert "(summary_embedding_id)" in indexdef
            assert "deleted_at IS NULL" in indexdef
            assert "summary_embedding_id IS NOT NULL" in indexdef
            valid = conn.execute(
                text(
                    "SELECT i.indisvalid FROM pg_class c "
                    "JOIN pg_index i ON c.oid = i.indexrelid WHERE c.relname = :n"
                ),
                {"n": INDEX_NAME},
            ).scalar_one()
            assert valid is True

        # Seed rows and ANALYZE so the planner has statistics to work with.
        with engine.begin() as conn:
            _seed_memories(conn)

        # Planner check: with seqscan disabled the partial index must back the
        # predicate _delete_memory_point emits (no context_id scope).
        with engine.connect() as conn:
            conn.execute(text("SET enable_seqscan = off"))
            plan = "\n".join(
                conn.execute(
                    text(
                        "EXPLAIN SELECT EXISTS (SELECT 1 FROM memories "
                        "WHERE summary_embedding_id = CAST(:point AS uuid) "
                        "AND id <> CAST(:self AS uuid) "
                        "AND deleted_at IS NULL)"
                    ),
                    {"point": _POINT_ID, "self": _POINT_ID},
                )
                .scalars()
                .all()
            )
            assert INDEX_NAME in plan, f"partial index not used; plan was:\n{plan}"
    finally:
        engine.dispose()
        _leave_db_at_head()


def test_e94_downgrade_drops_index() -> None:
    """Downgrade removes the index; other memories indexes are unaffected."""
    _reset_alembic_state()
    with _alembic_at_test_db():
        command.upgrade(_get_alembic_config(), E94_REVISION)
        command.downgrade(_get_alembic_config(), PRIOR_HEAD)

    engine = _sync_engine()
    try:
        with engine.connect() as conn:
            assert _index_def(conn) is None, "index should be dropped on downgrade"
            surviving = (
                conn.execute(
                    text(
                        "SELECT indexname FROM pg_indexes "
                        "WHERE tablename = 'memories' "
                        "AND indexname IN ('ix_memories_context_id', 'idx_memories_ws_ctx')"
                    )
                )
                .scalars()
                .all()
            )
            assert len(surviving) == 2, f"pre-existing indexes affected: {surviving}"
    finally:
        engine.dispose()
        _leave_db_at_head()


def test_e94_index_is_mirrored_in_the_model() -> None:
    """``Memory.__table_args__`` declares the same index, so a create_all
    schema (the unit suite) and an alembic one agree."""
    index = next(i for i in Memory.__table__.indexes if i.name == INDEX_NAME)

    assert [column.name for column in index.columns] == ["summary_embedding_id"]
    assert (
        str(index.dialect_options["postgresql"]["where"])
        == "deleted_at IS NULL AND summary_embedding_id IS NOT NULL"
    )
