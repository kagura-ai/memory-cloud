"""Alembic migration forward/rollback tests.

Issue #335: Verify all migrations can be applied and rolled back cleanly.
Requires a real PostgreSQL database (TEST_DATABASE_URL).
"""

import os
import uuid
from contextlib import contextmanager

from alembic.config import Config
from sqlalchemy import create_engine, text

from alembic import command

ALEMBIC_INI = "alembic.ini"

_DEFAULT_TEST_URL = "postgresql+asyncpg://kagura:kagura_dev_password@localhost:5432/kagura_test"


def _get_alembic_config() -> Config:
    """Create Alembic config pointing to test database.

    Note: ``backend/alembic/env.py`` reads ``get_database_url()`` at import
    time and overwrites ``sqlalchemy.url`` on the Config, so this
    ``set_main_option`` is always clobbered before migrations actually run.
    It is kept for cases where env.py might not be imported (e.g. direct
    Config introspection). To actually steer the migrations at the test DB,
    wrap the ``command.*`` call in ``_alembic_at_test_db()`` below.
    """
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
    test_url = os.getenv("TEST_DATABASE_URL", _DEFAULT_TEST_URL)
    sync_url = test_url.replace("+asyncpg", "")
    return create_engine(sync_url)


@contextmanager
def _alembic_at_test_db():
    """Temporarily point ``get_database_url()`` at ``TEST_DATABASE_URL``.

    ``backend/alembic/env.py`` reads ``get_database_url()`` on import and
    overwrites the Config's ``sqlalchemy.url``, so
    ``_get_alembic_config().set_main_option("sqlalchemy.url", ...)`` is
    always clobbered. The only reliable way to steer ``command.upgrade`` /
    ``command.downgrade`` / ``command.ensure_version`` at the test DB is
    to set ``DATABASE_URL`` in the environment for the duration of the
    call. Anything outside this context sees the prior value restored.

    This is the shared mechanism used by both ``TestAlembicMigrations``
    (mechanical forward/rollback) and ``TestB03NeuralEdgesBackfillMigration``
    (seeded data-path tests) — without it, a ``_reset_alembic_state()``
    drops the test schema but the migration then runs against the dev DB,
    silently stamping the dev alembic_version and producing bogus "passes".
    """
    test_url = os.getenv("TEST_DATABASE_URL", _DEFAULT_TEST_URL)
    prev = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = test_url
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prev


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
    """Test that all Alembic migrations apply and rollback cleanly.

    Every ``command.*`` call is wrapped in ``_alembic_at_test_db()`` so the
    migration runs against the test DB, not the dev DB — see that helper's
    docstring for the env.py override issue it works around.
    """

    def test_upgrade_to_head(self):
        """All migrations apply without error."""
        _reset_alembic_state()
        config = _get_alembic_config()
        with _alembic_at_test_db():
            command.upgrade(config, "head")

    def test_current_is_head(self):
        """After upgrade, current revision matches head."""
        config = _get_alembic_config()
        # This will raise if not at head
        with _alembic_at_test_db():
            command.ensure_version(config)

    def test_downgrade_one_step(self):
        """Most recent migration can be rolled back."""
        config = _get_alembic_config()
        with _alembic_at_test_db():
            command.upgrade(config, "head")
            command.downgrade(config, "-1")
            # Re-apply to leave DB in clean state
            command.upgrade(config, "head")

    def test_downgrade_to_base_and_upgrade(self):
        """Full rollback to baseline and re-apply works."""
        config = _get_alembic_config()
        with _alembic_at_test_db():
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

        Steers migrations at the test DB via ``_alembic_at_test_db()`` —
        without it, env.py's ``get_database_url()`` override would silently
        redirect the upgrade to the dev DB.
        """
        # Safety guard — mirrors _reset_alembic_state's non-test-DB check so
        # a misconfigured TEST_DATABASE_URL cannot drop a dev/prod database.
        test_url = os.getenv("TEST_DATABASE_URL", _DEFAULT_TEST_URL)
        db_name = test_url.rsplit("/", 1)[-1].split("?")[0]
        if not db_name.endswith("_test"):
            raise RuntimeError(f"Refusing to reset non-test database: {db_name}")

        engine = _sync_engine()
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
        engine.dispose()

        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), B02_REV)

    @staticmethod
    def _upgrade_head_with_test_db():
        """Run ``alembic upgrade head`` aimed at the test DB."""
        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), "head")

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


# Revision one step before e05_558 — the state where contexts.sleep_mode
# still defaults to 'full', so we can seed a row at the old default and
# then upgrade over e05_558 to verify existing rows are not rewritten.
E04_REV = "e04_552_gc_index"
E05_REV = "e05_558_sleep_default_skip"


class TestE05SleepModeDefaultMigration:
    """Seeded-state tests for e05_558_sleep_mode_default_skip (#558).

    ``ALTER COLUMN ... SET DEFAULT`` is a metadata-only change in PostgreSQL —
    it must not touch existing rows. These tests assert that contract on a
    real DB by seeding a context at the old default ``'full'``, running the
    e05 upgrade, and verifying:

      * the seeded row's ``sleep_mode`` is still ``'full'`` (existing rows untouched)
      * a new context inserted post-upgrade gets ``'skip'`` (new default fires)
      * downgrading reverts the default back to ``'full'``
    """

    @staticmethod
    def _reset_and_upgrade_to_e04():
        test_url = os.getenv("TEST_DATABASE_URL", _DEFAULT_TEST_URL)
        db_name = test_url.rsplit("/", 1)[-1].split("?")[0]
        if not db_name.endswith("_test"):
            raise RuntimeError(f"Refusing to reset non-test database: {db_name}")

        engine = _sync_engine()
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
        engine.dispose()

        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), E04_REV)

    @staticmethod
    def _seed_workspace_and_context(conn, name_suffix: str) -> tuple[str, str]:
        """Insert a workspace + context (no sleep_mode → server_default fires)."""
        owner_id = f"owner-{name_suffix}"
        ws_id = str(uuid.uuid4())
        ctx_id = str(uuid.uuid4())
        conn.execute(
            text("INSERT INTO workspaces (id, name, owner_user_id) VALUES (:id, :name, :owner)"),
            {"id": ws_id, "name": f"ws-{name_suffix}", "owner": owner_id},
        )
        conn.execute(
            text(
                "INSERT INTO contexts (id, workspace_id, name, created_by) "
                "VALUES (:id, :ws, :name, :owner)"
            ),
            {
                "id": ctx_id,
                "ws": ws_id,
                "name": f"ctx-{name_suffix}",
                "owner": owner_id,
            },
        )
        return ws_id, ctx_id

    def test_existing_rows_keep_full_after_upgrade(self):
        """Row inserted at e04 with default 'full' must NOT change after e05 upgrade."""
        self._reset_and_upgrade_to_e04()

        engine = _sync_engine()
        with engine.begin() as conn:
            _, ctx_id_pre = self._seed_workspace_and_context(conn, "pre-upgrade")
            row = conn.execute(
                text("SELECT sleep_mode FROM contexts WHERE id = :id"),
                {"id": ctx_id_pre},
            ).fetchone()
            assert row is not None and row[0] == "full", (
                f"pre-upgrade context should default to 'full' at e04, got {row}"
            )
        engine.dispose()

        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), E05_REV)

        engine = _sync_engine()
        with engine.begin() as conn:
            row = conn.execute(
                text("SELECT sleep_mode FROM contexts WHERE id = :id"),
                {"id": ctx_id_pre},
            ).fetchone()
            assert row is not None and row[0] == "full", (
                "existing context's sleep_mode must be unchanged after e05 upgrade"
            )
        engine.dispose()

    def test_new_row_after_upgrade_defaults_to_skip(self):
        """Row inserted post-e05 (no sleep_mode) must get 'skip' from new default."""
        self._reset_and_upgrade_to_e04()
        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), E05_REV)

        engine = _sync_engine()
        with engine.begin() as conn:
            _, ctx_id_post = self._seed_workspace_and_context(conn, "post-upgrade")
            row = conn.execute(
                text("SELECT sleep_mode FROM contexts WHERE id = :id"),
                {"id": ctx_id_post},
            ).fetchone()
            assert row is not None and row[0] == "skip", (
                f"new context post-e05 should default to 'skip', got {row}"
            )
        engine.dispose()

    def test_downgrade_reverts_default_to_full(self):
        """After downgrading e05 → e04, new INSERTs (no sleep_mode) get 'full' again."""
        self._reset_and_upgrade_to_e04()
        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), E05_REV)
            command.downgrade(_get_alembic_config(), E04_REV)

        engine = _sync_engine()
        with engine.begin() as conn:
            _, ctx_id = self._seed_workspace_and_context(conn, "post-downgrade")
            row = conn.execute(
                text("SELECT sleep_mode FROM contexts WHERE id = :id"),
                {"id": ctx_id},
            ).fetchone()
            assert row is not None and row[0] == "full", (
                f"after downgrade to e04, new context should default to 'full', got {row}"
            )
        engine.dispose()

        # Re-apply to leave DB at head for subsequent tests in the same session.
        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), "head")
