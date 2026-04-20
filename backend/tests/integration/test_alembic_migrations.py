"""Alembic migration forward/rollback tests.

Issue #335: Verify all migrations can be applied and rolled back cleanly.
Requires a real PostgreSQL database (TEST_DATABASE_URL).
"""

import os
import uuid

from alembic.config import Config
from sqlalchemy import create_engine, text

from alembic import command

ALEMBIC_INI = "alembic.ini"


def _get_alembic_config() -> Config:
    """Create Alembic config pointing to test database."""
    config = Config(ALEMBIC_INI)
    # Override with test database URL if available
    test_url = os.getenv("TEST_DATABASE_URL")
    if test_url:
        # Alembic needs sync URL (not asyncpg)
        sync_url = test_url.replace("+asyncpg", "")
        config.set_main_option("sqlalchemy.url", sync_url)
    return config


def _sync_engine():
    """Sync SQLAlchemy engine pointing to the test database."""
    test_url = os.getenv(
        "TEST_DATABASE_URL",
        "postgresql+asyncpg://kagura:kagura_dev_password@localhost:5432/kagura_test",
    )
    sync_url = test_url.replace("+asyncpg", "")
    return create_engine(sync_url)


def _reset_alembic_state():
    """Drop alembic_version and all tables so upgrade starts clean.

    Needed when conftest.py create_all has already created tables
    in the same pytest session (session-scoped fixture conflict).

    Safety: refuses to run unless the DB name ends with '_test'.
    """
    from sqlalchemy import create_engine, text

    test_url = os.getenv(
        "TEST_DATABASE_URL",
        "postgresql+asyncpg://kagura:kagura_dev_password@localhost:5432/kagura_test",
    )
    # Safety guard: only allow DROP SCHEMA on databases ending with '_test'
    db_name = test_url.rsplit("/", 1)[-1].split("?")[0]
    if not db_name.endswith("_test"):
        raise RuntimeError(f"Refusing to reset non-test database: {db_name}")

    sync_url = test_url.replace("+asyncpg", "")
    engine = create_engine(sync_url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    engine.dispose()


class TestAlembicMigrations:
    """Test that all Alembic migrations apply and rollback cleanly."""

    def test_upgrade_to_head(self):
        """All migrations apply without error."""
        _reset_alembic_state()
        config = _get_alembic_config()
        command.upgrade(config, "head")

    def test_current_is_head(self):
        """After upgrade, current revision matches head."""
        config = _get_alembic_config()
        # This will raise if not at head
        command.ensure_version(config)

    def test_downgrade_one_step(self):
        """Most recent migration can be rolled back."""
        config = _get_alembic_config()
        command.upgrade(config, "head")
        command.downgrade(config, "-1")
        # Re-apply to leave DB in clean state
        command.upgrade(config, "head")

    def test_downgrade_to_base_and_upgrade(self):
        """Full rollback to baseline and re-apply works."""
        config = _get_alembic_config()
        # Downgrade to baseline (first revision: 157247e0df86)
        command.downgrade(config, "157247e0df86")
        # Re-upgrade to head
        command.upgrade(config, "head")


# Revision one step before b03_396 — the state where workspace_id / context_id
# on neural_memory_edges are still nullable, so we can seed pre-062 NULL rows
# and then upgrade over b03_396 to exercise the backfill + orphan-delete paths.
B02_REV = "b02_383_edges_ws_ctx_idx"


class TestB03NeuralEdgesBackfillMigration:
    """Seeded-state tests for b03_396_neural_edges_ws_ctx_not_null (#396 AC 5).

    The ``TestAlembicMigrations`` class above only verifies the *mechanics* of
    upgrade/downgrade. These tests instead exercise the data-path branches the
    b03_396 migration contains by seeding pre-062 NULL rows into shapes the
    migration is intended to handle:

      * matching endpoints     → backfilled (UPDATE ran, row survived)
      * both endpoints NULL    → orphan-deleted (neither side recoverable)
      * mismatched endpoints   → orphan-deleted (src.ctx ≠ dst.ctx, rejected by
                                 the narrowed backfill WHERE to avoid silently
                                 introducing the exact invariant violation the
                                 sibling write-path guard exists to prevent)
      * already-populated      → untouched (UPDATE's IS NULL guard)

    Each test drops the schema and re-upgrades to b02 so the seeded INSERT
    sees the old nullable columns.
    """

    @staticmethod
    def _reset_and_upgrade_to_b02():
        """Drop schema, upgrade to b02 (one step before b03), ready for seeding.

        Note on DATABASE_URL: backend/alembic/env.py unconditionally reads
        ``get_database_url()`` on import and calls ``config.set_main_option``
        on the alembic Config — so ``_get_alembic_config()``'s
        ``set_main_option("sqlalchemy.url", ...)`` is always clobbered by
        env.py. The only way to steer ``command.upgrade`` at the test DB is
        to set DATABASE_URL to the TEST_DATABASE_URL value for the duration
        of the call. We restore the original env afterwards so unrelated
        code outside the test is not affected.
        """
        # Safety guard — mirrors _reset_alembic_state's non-test-DB check so
        # a misconfigured TEST_DATABASE_URL cannot drop a dev/prod database.
        test_url = os.getenv(
            "TEST_DATABASE_URL",
            "postgresql+asyncpg://kagura:kagura_dev_password@localhost:5432/kagura_test",
        )
        db_name = test_url.rsplit("/", 1)[-1].split("?")[0]
        if not db_name.endswith("_test"):
            raise RuntimeError(f"Refusing to reset non-test database: {db_name}")

        engine = _sync_engine()
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
        engine.dispose()

        prev_db_url = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = test_url
        try:
            command.upgrade(_get_alembic_config(), B02_REV)
        finally:
            if prev_db_url is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = prev_db_url

    @staticmethod
    def _upgrade_head_with_test_db():
        """Run ``alembic upgrade head`` aimed at the test DB — see note above."""
        test_url = os.getenv(
            "TEST_DATABASE_URL",
            "postgresql+asyncpg://kagura:kagura_dev_password@localhost:5432/kagura_test",
        )
        prev_db_url = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = test_url
        try:
            command.upgrade(_get_alembic_config(), "head")
        finally:
            if prev_db_url is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = prev_db_url

    @staticmethod
    def _seed_workspace_and_context(conn, owner_id: str) -> tuple[str, str]:
        """Insert a workspace and a context; return their UUIDs as strings."""
        ws_id = str(uuid.uuid4())
        ctx_id = str(uuid.uuid4())
        conn.execute(
            text("INSERT INTO workspaces (id, name, owner_user_id) VALUES (:id, :name, :owner)"),
            {"id": ws_id, "name": f"ws-{ws_id[:8]}", "owner": owner_id},
        )
        conn.execute(
            text(
                "INSERT INTO contexts (id, workspace_id, name, created_by) "
                "VALUES (:id, :ws, :name, :owner)"
            ),
            {
                "id": ctx_id,
                "ws": ws_id,
                "name": f"ctx-{ctx_id[:8]}",
                "owner": owner_id,
            },
        )
        return ws_id, ctx_id

    @staticmethod
    def _seed_memory(
        conn,
        mem_id: str,
        owner_id: str,
        workspace_id: str | None,
        context_id: str | None,
    ) -> None:
        """Insert a memory with the given (workspace_id, context_id) — may be NULL."""
        conn.execute(
            text(
                "INSERT INTO memories "
                "(id, user_id, workspace_id, context_id, summary, content, type, "
                " embedding_status, importance, confidence, scope, long_term, "
                " use_count, access_count, client, source) "
                "VALUES (:id, :u, :ws, :ctx, 'm', 'x', 'note', 'pending', "
                " 0.5, 1.0, 'working', false, 0, 0, 'test', 'test')"
            ),
            {
                "id": mem_id,
                "u": owner_id,
                "ws": workspace_id,
                "ctx": context_id,
            },
        )

    @staticmethod
    def _seed_edge(
        conn,
        owner_id: str,
        src_id: str,
        dst_id: str,
        workspace_id: str | None = None,
        context_id: str | None = None,
    ) -> None:
        """Insert an edge with the given (workspace_id, context_id) — may be NULL."""
        conn.execute(
            text(
                "INSERT INTO neural_memory_edges "
                "(user_id, src_id, dst_id, edge_type, weight, confidence, "
                " workspace_id, context_id) "
                "VALUES (:u, :s, :d, 'neural_association', 1.0, 1.0, :ws, :ctx)"
            ),
            {
                "u": owner_id,
                "s": src_id,
                "d": dst_id,
                "ws": workspace_id,
                "ctx": context_id,
            },
        )

    def test_null_edge_with_matching_endpoints_is_backfilled(self):
        """Edge with NULL ws/ctx whose src and dst share the same ctx → backfilled."""
        self._reset_and_upgrade_to_b02()
        owner = "owner-match"
        mem_src = str(uuid.uuid4())
        mem_dst = str(uuid.uuid4())

        engine = _sync_engine()
        with engine.begin() as conn:
            ws_id, ctx_id = self._seed_workspace_and_context(conn, owner)
            self._seed_memory(conn, mem_src, owner, ws_id, ctx_id)
            self._seed_memory(conn, mem_dst, owner, ws_id, ctx_id)
            self._seed_edge(conn, owner, mem_src, mem_dst)  # NULL ws/ctx
        engine.dispose()

        self._upgrade_head_with_test_db()

        engine = _sync_engine()
        with engine.begin() as conn:
            rows = conn.execute(
                text("SELECT workspace_id, context_id FROM neural_memory_edges WHERE user_id = :u"),
                {"u": owner},
            ).fetchall()
            assert len(rows) == 1
            assert str(rows[0].workspace_id) == ws_id
            assert str(rows[0].context_id) == ctx_id
        engine.dispose()

    def test_null_edge_with_both_endpoints_null_is_orphan_deleted(self):
        """Edge with NULL ws/ctx whose endpoints also have NULL ws/ctx → deleted."""
        self._reset_and_upgrade_to_b02()
        owner = "owner-both-null"
        mem_src = str(uuid.uuid4())
        mem_dst = str(uuid.uuid4())

        engine = _sync_engine()
        with engine.begin() as conn:
            # Memories with NULL ws/ctx — simulates pre-063 state before
            # migration 063 backfilled the Memory isolation columns.
            self._seed_memory(conn, mem_src, owner, None, None)
            self._seed_memory(conn, mem_dst, owner, None, None)
            self._seed_edge(conn, owner, mem_src, mem_dst)  # NULL ws/ctx
        engine.dispose()

        self._upgrade_head_with_test_db()

        engine = _sync_engine()
        with engine.begin() as conn:
            count = conn.execute(
                text("SELECT COUNT(*) FROM neural_memory_edges WHERE user_id = :u"),
                {"u": owner},
            ).scalar()
            assert count == 0
        engine.dispose()

    def test_null_edge_with_mismatched_endpoint_contexts_is_orphan_deleted(self):
        """Edge with NULL ws/ctx whose endpoints live in different contexts → deleted.

        Pins the self-review catch (commit 7699eac): the original
        COALESCE(e, ms, md) backfill would have silently picked one side and
        introduced the exact invariant violation the write-path guard exists
        to prevent. The narrowed WHERE (requires ms.ctx == md.ctx) means
        mismatched rows stay NULL and fall through to orphan delete.
        """
        self._reset_and_upgrade_to_b02()
        owner = "owner-mismatch"
        mem_src = str(uuid.uuid4())
        mem_dst = str(uuid.uuid4())

        engine = _sync_engine()
        with engine.begin() as conn:
            ws_id, ctx_a = self._seed_workspace_and_context(conn, owner)
            ctx_b = str(uuid.uuid4())
            conn.execute(
                text(
                    "INSERT INTO contexts (id, workspace_id, name, created_by) "
                    "VALUES (:id, :ws, :name, :o)"
                ),
                {"id": ctx_b, "ws": ws_id, "name": f"ctx-b-{ctx_b[:8]}", "o": owner},
            )
            self._seed_memory(conn, mem_src, owner, ws_id, ctx_a)  # in ctx_a
            self._seed_memory(conn, mem_dst, owner, ws_id, ctx_b)  # in ctx_b
            self._seed_edge(conn, owner, mem_src, mem_dst)  # NULL ws/ctx
        engine.dispose()

        self._upgrade_head_with_test_db()

        engine = _sync_engine()
        with engine.begin() as conn:
            count = conn.execute(
                text("SELECT COUNT(*) FROM neural_memory_edges WHERE user_id = :u"),
                {"u": owner},
            ).scalar()
            assert count == 0
        engine.dispose()

    def test_populated_edge_is_not_touched(self):
        """Edge already populated with ws/ctx → untouched by UPDATE's IS NULL guard."""
        self._reset_and_upgrade_to_b02()
        owner = "owner-populated"
        mem_src = str(uuid.uuid4())
        mem_dst = str(uuid.uuid4())

        engine = _sync_engine()
        with engine.begin() as conn:
            ws_id, ctx_id = self._seed_workspace_and_context(conn, owner)
            self._seed_memory(conn, mem_src, owner, ws_id, ctx_id)
            self._seed_memory(conn, mem_dst, owner, ws_id, ctx_id)
            # Edge already has ws/ctx populated
            self._seed_edge(conn, owner, mem_src, mem_dst, ws_id, ctx_id)
        engine.dispose()

        self._upgrade_head_with_test_db()

        engine = _sync_engine()
        with engine.begin() as conn:
            rows = conn.execute(
                text("SELECT workspace_id, context_id FROM neural_memory_edges WHERE user_id = :u"),
                {"u": owner},
            ).fetchall()
            assert len(rows) == 1
            assert str(rows[0].workspace_id) == ws_id
            assert str(rows[0].context_id) == ctx_id
        engine.dispose()

    def test_partial_null_edge_with_mismatched_non_null_value_is_orphan_deleted(self):
        """Edge with e.ws non-NULL but != endpoint's ws, e.ctx NULL → orphan-deleted.

        Copilot loop 1 catch: without the ``e.ws = ms.ws`` guard in Step 1's
        WHERE, COALESCE would fill e.ctx from the endpoints while preserving
        the incorrect e.ws, producing a fully-non-NULL row that escapes the
        Step 2 orphan delete AND still violates the edge↔memory invariant.
        The narrowed WHERE leaves the partial-NULL row untouched so the
        orphan delete catches it.
        """
        self._reset_and_upgrade_to_b02()
        owner = "owner-partial-null-mismatch"
        mem_src = str(uuid.uuid4())
        mem_dst = str(uuid.uuid4())
        wrong_ws = str(uuid.uuid4())  # UUID that doesn't exist in workspaces

        engine = _sync_engine()
        with engine.begin() as conn:
            ws_id, ctx_id = self._seed_workspace_and_context(conn, owner)
            self._seed_memory(conn, mem_src, owner, ws_id, ctx_id)
            self._seed_memory(conn, mem_dst, owner, ws_id, ctx_id)
            # Insert edge with a *foreign* workspace_id (non-NULL but wrong)
            # and context_id=NULL. Bypass the FK on workspace_id temporarily
            # by dropping the constraint for this row — at b02 the edge's
            # workspace_id column has no FK to workspaces, only a plain UUID
            # column, so we can write any UUID. (If the schema ever adds an
            # FK on neural_memory_edges.workspace_id, this test will need to
            # be adjusted to seed the wrong_ws into workspaces first.)
            conn.execute(
                text(
                    "INSERT INTO neural_memory_edges "
                    "(user_id, src_id, dst_id, edge_type, weight, confidence, "
                    " workspace_id, context_id) "
                    "VALUES (:u, :s, :d, 'neural_association', 1.0, 1.0, "
                    " :ws, NULL)"
                ),
                {"u": owner, "s": mem_src, "d": mem_dst, "ws": wrong_ws},
            )
        engine.dispose()

        self._upgrade_head_with_test_db()

        engine = _sync_engine()
        with engine.begin() as conn:
            count = conn.execute(
                text("SELECT COUNT(*) FROM neural_memory_edges WHERE user_id = :u"),
                {"u": owner},
            ).scalar()
            assert count == 0, (
                "partial-NULL edge with wrong pre-existing ws must be orphan-deleted, "
                "not COALESCE-repaired into a fully-populated invariant violation"
            )
        engine.dispose()
