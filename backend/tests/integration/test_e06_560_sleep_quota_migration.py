"""Integration tests for migration ``e06_560_sleep_quota_addon`` (Issue #560).

These tests cover the data-shape changes that ``TestAlembicMigrations`` does
not — specifically:

1. ``workspaces.addon_sleep_contexts_bonus`` is added with default 0.
2. ``workspace_addons.check_addon_type`` is extended with ``extra_sleep_contexts``.
3. The force-skip data migration scopes correctly:
   - FREE/BASIC contexts with ``sleep_mode != 'skip'`` are forced to ``'skip'``.
   - PRO contexts are NOT touched (grandfather).
   - Soft-deleted contexts (``deleted_at IS NOT NULL``) are NOT touched.
4. ``downgrade()`` drops the column and restores the prior CHECK constraint.

The pre-revision is ``e05_558_sleep_default_skip`` — seed at that revision so
the contexts table exists with the post-#558 default ``'skip'`` (we then
manually set non-skip values for the seed rows we want force-skipped).
"""

import uuid

from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from alembic import command
from tests.integration.test_alembic_migrations import (
    _alembic_at_test_db,
    _get_alembic_config,
    _reset_alembic_state,
    _sync_engine,
)

# Revision immediately before e06_560 — the state where the addon column is
# absent and the CHECK constraint excludes ``extra_sleep_contexts``. Pinning
# the target makes this test correct as more migrations land on top of e06_560.
PRE_E06_REV = "e05_558_sleep_default_skip"


def _seed_workspace(conn, plan_name: str, owner_user_id: str = "owner-test-560") -> str:
    """Insert a workspace with the given plan_name and return its UUID."""
    ws_id = str(uuid.uuid4())
    conn.execute(
        text(
            "INSERT INTO workspaces (id, name, owner_user_id, plan_name) "
            "VALUES (:id, :name, :owner, :plan)"
        ),
        {"id": ws_id, "name": f"ws-{ws_id[:8]}", "owner": owner_user_id, "plan": plan_name},
    )
    return ws_id


def _seed_context(
    conn,
    workspace_id: str,
    sleep_mode: str,
    soft_deleted: bool = False,
    owner_user_id: str = "owner-test-560",
) -> str:
    """Insert a context with explicit sleep_mode; return its UUID.

    ``sleep_mode`` is set explicitly via UPDATE rather than at INSERT time to
    avoid coupling to the column's request-schema absence (``Context.sleep_mode``
    is server_default-only per #558 — Pydantic does not accept it). The UPDATE
    after INSERT is the only way to seed a non-default value at the migration
    layer, since INSERT cannot bypass the server_default without explicit value.
    """
    ctx_id = str(uuid.uuid4())
    conn.execute(
        text(
            "INSERT INTO contexts (id, workspace_id, name, created_by, sleep_mode) "
            "VALUES (:id, :ws, :name, :owner, :sm)"
        ),
        {
            "id": ctx_id,
            "ws": workspace_id,
            "name": f"ctx-{ctx_id[:8]}",
            "owner": owner_user_id,
            "sm": sleep_mode,
        },
    )
    if soft_deleted:
        conn.execute(
            text("UPDATE contexts SET deleted_at = NOW() WHERE id = :id"),
            {"id": ctx_id},
        )
    return ctx_id


def _get_sleep_mode(conn, ctx_id: str) -> str:
    return conn.execute(
        text("SELECT sleep_mode FROM contexts WHERE id = :id"),
        {"id": ctx_id},
    ).scalar_one()


def _leave_db_at_head() -> None:
    """Convention: integration suite expects the test DB at head after each test."""
    with _alembic_at_test_db():
        command.upgrade(_get_alembic_config(), "head")


class TestE06SleepQuotaAddonMigration:
    """Data-shape and force-skip checks for ``e06_560_sleep_quota_addon``."""

    def test_upgrade_adds_addon_column_with_default_zero(self):
        """``workspaces.addon_sleep_contexts_bonus`` exists with default 0."""
        _reset_alembic_state()
        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), PRE_E06_REV)

        engine = _sync_engine()
        try:
            with engine.begin() as conn:
                ws_id = _seed_workspace(conn, plan_name="free")

            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), "head")

            inspector = inspect(engine)
            cols = {c["name"] for c in inspector.get_columns("workspaces")}
            assert "addon_sleep_contexts_bonus" in cols

            with engine.begin() as conn:
                bonus = conn.execute(
                    text("SELECT addon_sleep_contexts_bonus FROM workspaces WHERE id = :id"),
                    {"id": ws_id},
                ).scalar_one()
            assert bonus == 0
        finally:
            engine.dispose()

    def test_upgrade_force_skips_free_basic_active_contexts(self):
        """FREE/BASIC contexts with sleep_mode != 'skip' are forced to 'skip'."""
        _reset_alembic_state()
        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), PRE_E06_REV)

        engine = _sync_engine()
        try:
            with engine.begin() as conn:
                free_ws = _seed_workspace(conn, plan_name="free")
                basic_ws = _seed_workspace(conn, plan_name="basic")
                pro_ws = _seed_workspace(conn, plan_name="pro")

                # Active rows that SHOULD be force-skipped:
                free_full = _seed_context(conn, free_ws, sleep_mode="full")
                basic_full = _seed_context(conn, basic_ws, sleep_mode="full")
                basic_edges = _seed_context(conn, basic_ws, sleep_mode="edges_only")

                # PRO grandfather — must NOT be touched:
                pro_full_1 = _seed_context(conn, pro_ws, sleep_mode="full")
                pro_full_2 = _seed_context(conn, pro_ws, sleep_mode="edges_only")

                # Soft-deleted FREE row — must NOT be touched (deleted_at filter):
                free_deleted = _seed_context(conn, free_ws, sleep_mode="full", soft_deleted=True)

            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), "head")

            with engine.begin() as conn:
                # FREE / BASIC active rows: forced to skip
                assert _get_sleep_mode(conn, free_full) == "skip"
                assert _get_sleep_mode(conn, basic_full) == "skip"
                assert _get_sleep_mode(conn, basic_edges) == "skip"

                # PRO rows: untouched (grandfather)
                assert _get_sleep_mode(conn, pro_full_1) == "full"
                assert _get_sleep_mode(conn, pro_full_2) == "edges_only"

                # Soft-deleted row: untouched (deleted_at filter)
                assert _get_sleep_mode(conn, free_deleted) == "full"
        finally:
            engine.dispose()

    def test_upgrade_extends_addon_type_check_constraint(self):
        """``workspace_addons.check_addon_type`` accepts ``extra_sleep_contexts``."""
        _reset_alembic_state()
        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), "head")

        engine = _sync_engine()
        try:
            with engine.begin() as conn:
                ws_id = _seed_workspace(conn, plan_name="pro")
                # Should succeed — the new addon type is in the extended enum.
                conn.execute(
                    text(
                        "INSERT INTO workspace_addons "
                        "(workspace_id, addon_type, quantity, active_from, created_by) "
                        "VALUES (:ws, 'extra_sleep_contexts', 1, NOW(), 'test-560')"
                    ),
                    {"ws": ws_id},
                )

            # Bogus addon type still rejected by the CHECK.
            with engine.begin() as conn:
                try:
                    conn.execute(
                        text(
                            "INSERT INTO workspace_addons "
                            "(workspace_id, addon_type, quantity, active_from, created_by) "
                            "VALUES (:ws, 'extra_garbage_type', 1, NOW(), 'test-560')"
                        ),
                        {"ws": ws_id},
                    )
                    raise AssertionError("Expected IntegrityError on bogus addon_type")
                except IntegrityError:
                    pass
        finally:
            engine.dispose()

    def test_downgrade_drops_column_and_restores_prior_constraint(self):
        """Downgrade reverses the column add and restores the pre-#560 CHECK."""
        _reset_alembic_state()
        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), "head")
            command.downgrade(_get_alembic_config(), PRE_E06_REV)

        engine = _sync_engine()
        try:
            inspector = inspect(engine)
            cols = {c["name"] for c in inspector.get_columns("workspaces")}
            assert "addon_sleep_contexts_bonus" not in cols

            # The old CHECK constraint should reject the new addon type.
            with engine.begin() as conn:
                ws_id = _seed_workspace(conn, plan_name="pro")
                try:
                    conn.execute(
                        text(
                            "INSERT INTO workspace_addons "
                            "(workspace_id, addon_type, quantity, active_from) "
                            "VALUES (:ws, 'extra_sleep_contexts', 1, NOW())"
                        ),
                        {"ws": ws_id},
                    )
                    raise AssertionError(
                        "Expected IntegrityError after downgrade — "
                        "extra_sleep_contexts should not be in the pre-#560 enum"
                    )
                except IntegrityError:
                    pass
        finally:
            engine.dispose()
            _leave_db_at_head()
