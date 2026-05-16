"""Integration tests for migration ``e15_675_workspace_slot_bonus`` (#675).

These tests cover the data-shape and grandfather backfill that
``TestAlembicMigrations`` does not — specifically:

1. ``users.workspace_slot_bonus`` is added with default 0.
2. Grandfather backfill: a user with 5 owned (non-deleted) workspaces
   gets ``workspace_slot_bonus = 4`` after upgrade.
3. A user with 0 or 1 owned workspaces gets ``workspace_slot_bonus = 0``
   (GREATEST guard and outer WHERE filter respectively).
4. Soft-deleted workspaces are excluded from the count: a user with 1
   live + 4 deleted workspaces gets bonus = 0, not 4.
5. ``downgrade()`` drops the column.

Pre-revision is ``e14_655_allowlist_provider``.
"""

import uuid

from sqlalchemy import inspect, text

from alembic import command
from tests.integration.test_alembic_migrations import (
    _alembic_at_test_db,
    _get_alembic_config,
    _reset_alembic_state,
    _sync_engine,
)

# Pre-e15 revision: state where users.workspace_slot_bonus column does
# NOT yet exist. Pinning this target keeps the test correct as more
# migrations land on top of e15.
PRE_E15_REV = "e14_655_allowlist_provider"


def _seed_user(conn, user_id: str | None = None) -> str:
    """Insert a minimal user row; return the user_id (OAuth sub).

    ``users.timezone``, ``users.locale``, and ``users.is_initial_admin`` are
    NOT NULL columns without server defaults in the baseline schema
    (see ``157247e0df86_baseline_create_all_tables_from_models.py``). The
    raw-SQL INSERT here is below the ORM layer's Python-side defaults, so
    omitting them would raise a NOT NULL violation before the e15 upgrade
    runs. Supply explicit values matching the model's defaults
    (``timezone='UTC'``, ``locale='en'``, ``is_initial_admin=false``);
    ``auth_method`` is omitted because it has ``server_default='oauth'``.
    """
    uid = user_id or f"u-{uuid.uuid4().hex[:12]}"
    conn.execute(
        text(
            "INSERT INTO users "
            "(email, user_id, role, timezone, locale, is_initial_admin) "
            "VALUES (:email, :uid, 'user', 'UTC', 'en', false)"
        ),
        {"email": f"{uid}@test.example", "uid": uid},
    )
    return uid


def _seed_workspace(conn, owner_user_id: str, deleted: bool = False) -> str:
    """Insert a workspace owned by ``owner_user_id``; optionally soft-delete."""
    ws_id = str(uuid.uuid4())
    conn.execute(
        text("INSERT INTO workspaces (id, name, owner_user_id) VALUES (:id, :name, :owner)"),
        {"id": ws_id, "name": f"ws-{ws_id[:8]}", "owner": owner_user_id},
    )
    if deleted:
        conn.execute(
            text("UPDATE workspaces SET deleted_at = NOW() WHERE id = :id"),
            {"id": ws_id},
        )
    return ws_id


def _get_bonus(conn, user_id: str) -> int:
    return conn.execute(
        text("SELECT workspace_slot_bonus FROM users WHERE user_id = :uid"),
        {"uid": user_id},
    ).scalar_one()


def _leave_db_at_head() -> None:
    """Convention: integration suite expects the test DB at head after each test."""
    with _alembic_at_test_db():
        command.upgrade(_get_alembic_config(), "head")


class TestE15WorkspaceSlotBonusMigration:
    """Data-shape and grandfather-backfill checks for e15_675."""

    def test_upgrade_adds_column_with_default_zero(self):
        """Column exists with default 0 after upgrade; users with <=1 stay 0."""
        _reset_alembic_state()
        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), PRE_E15_REV)

        engine = _sync_engine()
        try:
            with engine.begin() as conn:
                uid_none = _seed_user(conn)  # 0 owned workspaces
                uid_one = _seed_user(conn)
                _seed_workspace(conn, uid_one)  # 1 owned workspace

            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), "head")

            inspector = inspect(engine)
            cols = {c["name"] for c in inspector.get_columns("users")}
            assert "workspace_slot_bonus" in cols

            with engine.begin() as conn:
                assert _get_bonus(conn, uid_none) == 0
                assert _get_bonus(conn, uid_one) == 0
        finally:
            engine.dispose()

    def test_grandfather_backfill_five_workspaces_gives_bonus_four(self):
        """User with 5 owned workspaces gets bonus=4 (effective cap 1+4=5)."""
        _reset_alembic_state()
        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), PRE_E15_REV)

        engine = _sync_engine()
        try:
            with engine.begin() as conn:
                uid = _seed_user(conn)
                for _ in range(5):
                    _seed_workspace(conn, uid)

            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), "head")

            with engine.begin() as conn:
                assert _get_bonus(conn, uid) == 4
        finally:
            engine.dispose()

    def test_soft_deleted_workspaces_excluded_from_owned_count(self):
        """Soft-deleted workspaces do NOT contribute to the grandfather bonus."""
        _reset_alembic_state()
        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), PRE_E15_REV)

        engine = _sync_engine()
        try:
            with engine.begin() as conn:
                uid = _seed_user(conn)
                # 1 live + 4 soft-deleted → effective owned_count = 1 → bonus = 0
                _seed_workspace(conn, uid)
                for _ in range(4):
                    _seed_workspace(conn, uid, deleted=True)

            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), "head")

            with engine.begin() as conn:
                assert _get_bonus(conn, uid) == 0
        finally:
            engine.dispose()

    def test_downgrade_drops_column(self):
        """Downgrade removes workspace_slot_bonus from users."""
        _reset_alembic_state()
        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), "head")
            command.downgrade(_get_alembic_config(), PRE_E15_REV)

        engine = _sync_engine()
        try:
            inspector = inspect(engine)
            cols = {c["name"] for c in inspector.get_columns("users")}
            assert "workspace_slot_bonus" not in cols
        finally:
            engine.dispose()
            _leave_db_at_head()
